"""aura.py - AURA, the DREAM Robotics fleet's weather station.

Atmospheric Utility & Reporting Automaton. She knows the weather three ways:
  * outside: the current weather for her location from Open-Meteo (open source, free, no API key)
  * her own:  an AHT10 on her Arduino over USB (scripts/aura_sensor), "TEMP:21.4" / "HUM:48.2"
  * the fleet: the temperature and humidity every robot already reports (MILA's /status, NORA's
    /sensors, anything with "temp" / "hum"), found through RIFT's fleet list, read over WiFi or
    Bluetooth (fleet_bt), or pushed to her with POST /report
and keeps them all in data/history.csv, so her page can plot inside against outside.

    python aura.py                 her page on http://127.0.0.1:5017/
"""
import csv
import json
import math
import os
import re
import threading
import time
from collections import defaultdict, deque

import requests
from flask import Flask, jsonify, render_template, request

import fleet_bt
from fleet_ladder import FleetLadder

NAME = "AURA"
HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
HISTORY_CSV = os.path.join(DATA, "history.csv")
LOCATION_JSON = os.path.join(DATA, "location.json")

DEFAULTS = {
    "Port": 5017,
    "RiftHost": "127.0.0.1",
    "RiftPort": 5000,
    "Location": {"Latitude": None, "Longitude": None, "City": "", "Name": ""},
    "Units": "C",
    "WeatherMinutes": 10,
    "FleetSeconds": 60,
    "BoardPort": "auto",
    "BoardBaud": 9600,
    "HistoryDays": 7,
    "SampleSeconds": 60,
    "Bluetooth": {},
    "BluetoothServer": True,
    "FleetLadder": False,
}


def load_config(path=os.path.join(HERE, "config.json")):
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(path, encoding="utf-8") as f:
            cfg.update(json.load(f).get("Config", {}).get(NAME, {}))
    except (OSError, ValueError):
        pass
    return cfg


CFG = load_config()
PORT = int(CFG["Port"])
STARTED = time.time()
UA = {"User-Agent": "AURA-weather-station/1.0 (DREAM Robotics)"}   # some services refuse Python's default

# ── What the weather codes mean (WMO, as Open-Meteo sends them) ─────────────────────────────
WMO = {0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy",
       51: "drizzling", 53: "drizzling", 55: "drizzling", 56: "freezing drizzle", 57: "freezing drizzle",
       61: "light rain", 63: "raining", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
       71: "light snow", 73: "snowing", 75: "heavy snow", 77: "snow grains", 80: "showers", 81: "showers",
       82: "violent showers", 85: "snow showers", 86: "snow showers", 95: "a thunderstorm",
       96: "a thunderstorm with hail", 99: "a thunderstorm with hail"}


def describe(code):
    return WMO.get(code, "unknown weather")


# ── Readings from anywhere: a robot's status JSON, a pushed report, her own board ─────────────
TEMP_KEYS = ("temp", "temperature", "temp_c", "tempC", "t")
HUM_KEYS = ("hum", "humidity", "rh", "h")


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def climate_from(d):
    """(temp C, humidity %) from any robot's JSON, looking inside "sensors" / "env" too. A robot
    without the sensor sends nothing, 0 or NORA's -1: those count as no reading."""
    if not isinstance(d, dict):
        return None, None
    for inner in ("sensors", "env", "climate"):
        if isinstance(d.get(inner), dict):
            t, h = climate_from(d[inner])
            if t is not None or h is not None:
                return t, h
    t = next((_num(d[k]) for k in TEMP_KEYS if k in d), None)
    h = next((_num(d[k]) for k in HUM_KEYS if k in d), None)
    if t is not None and not -40 <= t <= 85:
        t = None
    if h is not None and not 0 < h <= 100:
        h = None
    if t == -1 and h is None:      # NORA without her AHT10: -1 / -1
        t = None
    return t, h


# ── History: one row per source per SampleSeconds, kept HistoryDays ───────────────────────────
class History:
    def __init__(self, path, days, sample_s):
        self.path, self.days, self.sample_s = path, days, sample_s
        self.lock = threading.Lock()
        self.rows = defaultdict(deque)          # source -> deque[(ts, temp, hum)]
        self.last_saved = {}
        self._load()

    def _load(self):
        try:
            with open(self.path, newline="", encoding="utf-8") as f:
                for r in csv.DictReader(f):
                    self.rows[r["source"]].append((float(r["ts"]), _num(r["temp"]), _num(r["hum"])))
        except (OSError, KeyError, ValueError):
            pass
        for src, rows in self.rows.items():
            if rows:
                self.last_saved[src] = rows[-1][0]
        self.prune(rewrite=True)

    def add(self, source, temp, hum, now=None):
        """Keeps it if this source hasn't been saved for SampleSeconds. True if it was kept."""
        now = now or time.time()
        if temp is None and hum is None:
            return False
        with self.lock:
            if now - self.last_saved.get(source, 0) < self.sample_s:
                return False
            self.last_saved[source] = now
            self.rows[source].append((now, temp, hum))
            new = not os.path.exists(self.path)
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["ts", "source", "temp", "hum"])
                w.writerow([round(now), source, "" if temp is None else round(temp, 1), "" if hum is None else round(hum, 1)])
        return True

    def prune(self, rewrite=False):
        cut = time.time() - self.days * 86400
        with self.lock:
            dropped = 0
            for rows in self.rows.values():
                while rows and rows[0][0] < cut:
                    rows.popleft()
                    dropped += 1
            if (dropped or rewrite) and self.rows:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                tmp = self.path + ".tmp"
                with open(tmp, "w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow(["ts", "source", "temp", "hum"])
                    for src, rows in self.rows.items():
                        for ts, t, h in rows:
                            w.writerow([round(ts), src, "" if t is None else t, "" if h is None else h])
                os.replace(tmp, self.path)

    def series(self, hours):
        cut = time.time() - hours * 3600
        with self.lock:
            return {src: [[round(ts), t, h] for ts, t, h in rows if ts >= cut]
                    for src, rows in self.rows.items() if any(ts >= cut for ts, _, _ in rows)}


HISTORY = History(HISTORY_CSV, CFG["HistoryDays"], CFG["SampleSeconds"])
NOW = {}                          # source -> {"temp", "hum", "ts", "via"}
NOW_LOCK = threading.Lock()
STATE = {"weather": None, "weather_error": None, "location": None, "board": None, "board_error": None,
         "registered": False, "fleet_error": None}


def note_reading(source, temp, hum, via):
    if temp is None and hum is None:
        return
    with NOW_LOCK:
        NOW[source] = {"temp": temp, "hum": hum, "ts": time.time(), "via": via}
    HISTORY.add(source, temp, hum)


# ── Where she is ───────────────────────────────────────────────────────────────────────────────
def resolve_location(cfg=CFG, get=requests.get):
    """Latitude/Longitude from config.json, else its City (Open-Meteo's geocoding), else a guess
    from this network's public address. Remembered in data/location.json."""
    loc = cfg.get("Location") or {}
    lat, lon = _num(loc.get("Latitude")), _num(loc.get("Longitude"))
    if lat is not None and lon is not None:
        return {"lat": lat, "lon": lon, "name": loc.get("Name") or loc.get("City") or f"{lat:.2f}, {lon:.2f}", "from": "config"}
    city = (loc.get("City") or "").strip()
    try:
        with open(LOCATION_JSON, encoding="utf-8") as f:
            saved = json.load(f)
        if saved.get("city", "") == city:
            return saved
    except (OSError, ValueError):
        pass
    found = None
    if city:
        r = get("https://geocoding-api.open-meteo.com/v1/search", params={"name": city, "count": 1}, timeout=10, headers=UA)
        hit = (r.json().get("results") or [None])[0]
        if hit:
            found = {"lat": hit["latitude"], "lon": hit["longitude"],
                     "name": ", ".join(x for x in (hit.get("name"), hit.get("country")) if x), "from": "city"}
    # No city: a guess from this network's public address (two free services, in case one says no)
    for url, country in (("https://ipapi.co/json/", "country_name"), ("https://ipwho.is/", "country")):
        if found:
            break
        try:
            d = get(url, timeout=10, headers=UA).json()
        except (requests.RequestException, ValueError):
            continue
        if _num(d.get("latitude")) is not None:
            found = {"lat": d["latitude"], "lon": d["longitude"],
                     "name": ", ".join(x for x in (d.get("city"), d.get(country)) if x), "from": "network"}
    if not found:
        raise RuntimeError("couldn't work out where this is: set Location in config.json")
    found["city"] = city
    os.makedirs(DATA, exist_ok=True)
    with open(LOCATION_JSON, "w", encoding="utf-8") as f:
        json.dump(found, f)
    return found


def fetch_weather(loc, get=requests.get):
    r = get("https://api.open-meteo.com/v1/forecast", timeout=10, headers=UA, params={
        "latitude": loc["lat"], "longitude": loc["lon"], "timezone": "auto",
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,"
                   "wind_speed_10m,precipitation,is_day",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max", "forecast_days": 1})
    r.raise_for_status()
    d = r.json()
    c, day = d.get("current", {}), d.get("daily", {})
    first = lambda k: (day.get(k) or [None])[0]
    return {"temp": _num(c.get("temperature_2m")), "hum": _num(c.get("relative_humidity_2m")),
            "feels": _num(c.get("apparent_temperature")), "code": c.get("weather_code"),
            "words": describe(c.get("weather_code")), "wind_kmh": _num(c.get("wind_speed_10m")),
            "rain_mm": _num(c.get("precipitation")), "day": bool(c.get("is_day", 1)),
            "high": _num(first("temperature_2m_max")), "low": _num(first("temperature_2m_min")),
            "rain_chance": _num(first("precipitation_probability_max")), "ts": time.time()}


def weather_loop():
    while True:
        try:
            if not STATE["location"]:
                STATE["location"] = resolve_location()
            STATE["weather"] = fetch_weather(STATE["location"])
            STATE["weather_error"] = None
            w = STATE["weather"]
            note_reading("Outside", w["temp"], w["hum"], "open-meteo")
            time.sleep(CFG["WeatherMinutes"] * 60)
        except Exception as e:      # no internet yet, a timeout: try again soon
            STATE["weather_error"] = f"{type(e).__name__}: {e}"[:160]
            time.sleep(60)


# ── Her own sensor: the Arduino ────────────────────────────────────────────────────────────────
def find_board(baud):
    """Explicit BoardPort, else whichever serial port answers WHO with "I am AURA"."""
    import serial
    from serial.tools import list_ports
    if str(CFG["BoardPort"]).lower() != "auto":
        return CFG["BoardPort"]
    for p in list_ports.comports():
        try:
            with serial.Serial(p.device, baud, timeout=0.3) as s:
                time.sleep(2.0)                 # an UNO resets when its port opens
                s.reset_input_buffer()
                s.write(b"WHO\n")
                end = time.time() + 2.0
                while time.time() < end:
                    if b"I am AURA" in s.readline():
                        return p.device
        except (OSError, serial.SerialException):
            continue
    return None


def board_loop():
    try:
        import serial
    except ImportError:
        STATE["board_error"] = "pyserial isn't installed"
        return
    temp = hum = None
    while True:
        port = find_board(CFG["BoardBaud"])
        if not port:
            STATE["board"], STATE["board_error"] = None, "Her Arduino isn't plugged in."
            time.sleep(30)
            continue
        try:
            with serial.Serial(port, CFG["BoardBaud"], timeout=2) as s:
                STATE["board"], STATE["board_error"] = port, None
                while True:
                    line = s.readline().decode(errors="ignore").strip()
                    if line.startswith("TEMP:"):
                        temp = _num(line[5:])
                    elif line.startswith("HUM:"):
                        hum = _num(line[4:])
                        note_reading(NAME, temp, hum, port)
                    elif line == "AHT_ERROR":
                        STATE["board_error"] = "Her AHT10 isn't answering: check SDA (A4) and SCL (A5)."
        except (OSError, serial.SerialException) as e:
            STATE["board"], STATE["board_error"] = None, f"{port} went away ({type(e).__name__})"
            time.sleep(5)


# ── The fleet: everyone who reports temperature or humidity ────────────────────────────────────
try:   # the WiFi ladder (fleet_ladder.py): AURA joins once she has a rung on it
    LADDER = FleetLadder(NAME, enabled=CFG.get("FleetLadder", False))
except ValueError:
    LADDER = None
_found_urls = {}                   # robot -> the URL that gave a reading last time


def registry():
    host = (LADDER and LADDER.registry_host()) or CFG["RiftHost"]
    return f"http://{host}:{CFG['RiftPort']}"


def candidate_urls(robot):
    ip = robot.get("ip")
    if not ip:
        return []
    ports = [c.split(":", 1)[1] for c in robot.get("capabilities", []) if str(c).startswith("web:")]
    urls = []
    for port in ports + ["80"]:
        base = f"http://{ip}" + ("" if port == "80" else f":{port}")
        urls += [base + "/sensors", base + "/status"]
    known = _found_urls.get(robot.get("name"))
    return ([known] if known else []) + [u for u in urls if u != known]


def read_robot(robot, get=requests.get):
    """(temp, hum, url) from the first of the robot's pages that reports them."""
    for url in candidate_urls(robot):
        try:
            r = get(url, timeout=2)
            if not r.ok:
                continue
            t, h = climate_from(r.json())
        except (requests.RequestException, ValueError):
            continue
        if t is not None or h is not None:
            _found_urls[robot["name"]] = url
            return t, h, url
    return None, None, None


def read_bluetooth(name, target):
    """A robot paired over Bluetooth (config.json Bluetooth: {"NORA": "COM7"}): its own pages,
    asked over the fleet's Bluetooth link."""
    link = fleet_bt.BtHttp(target)
    try:
        for path in ("/sensors", "/status"):
            code, text = link.get(path)
            if code == 200:
                t, h = climate_from(json.loads(text))
                if t is not None or h is not None:
                    return t, h
    finally:
        link.close()
    return None, None


def fleet_loop():
    while True:
        try:
            robots = requests.get(registry() + "/robots", timeout=3).json().get("robots", [])
            STATE["fleet_error"] = None
        except (requests.RequestException, ValueError) as e:
            robots = []
            STATE["fleet_error"] = f"RIFT's fleet list isn't answering ({type(e).__name__})"
        for robot in robots:
            if robot.get("name") in (NAME, None) or robot.get("name") in CFG["Bluetooth"]:
                continue
            t, h, url = read_robot(robot)
            note_reading(robot["name"], t, h, url)
        for name, target in (CFG.get("Bluetooth") or {}).items():
            try:
                t, h = read_bluetooth(name, target)
                note_reading(name, t, h, f"bluetooth {target}")
            except Exception:
                pass
        HISTORY.prune()
        time.sleep(CFG["FleetSeconds"])


def heartbeat():
    while True:
        try:
            r = requests.post(registry() + "/register", timeout=2, data={
                "name": NAME, "type": "weather", "capabilities": f"weather,climate,web:{PORT}"})
            STATE["registered"] = r.ok
        except requests.RequestException:
            STATE["registered"] = False
        time.sleep(10)


# ── What she'd say about it ────────────────────────────────────────────────────────────────────
def fmt(t):
    if t is None:
        return "?"
    return f"{t * 9 / 5 + 32:.0f}°F" if CFG.get("Units") == "F" else f"{t:.0f}°"


def says(now=None, weather=None):
    """A line in her own words: outside, in here, and who's warmest."""
    now = now if now is not None else snapshot()
    weather = weather if weather is not None else STATE["weather"]
    bits = []
    if weather and weather.get("temp") is not None:
        bits.append(f"It's {fmt(weather['temp'])} and {weather['words']} outside")
        if weather.get("rain_chance") and weather["rain_chance"] >= 60:
            bits.append(f"a {weather['rain_chance']:.0f}% chance of rain today, so take an umbrella")
    mine = now.get(NAME)
    if mine and mine.get("temp") is not None:
        bits.append(f"{fmt(mine['temp'])} in here")
        if weather and weather.get("temp") is not None:
            gap = mine["temp"] - weather["temp"]
            if gap >= 8:
                bits.append("cosy compared to out there")
            elif gap <= -3:
                bits.append("warmer outside than in here: open a window")
        if mine.get("hum") is not None and mine["hum"] >= 70:
            bits.append("and it's muggy")
        elif mine.get("hum") is not None and mine["hum"] <= 25:
            bits.append("and the air is dry")
    robots = {n: r for n, r in now.items() if n not in (NAME, "Outside") and r.get("temp") is not None}
    if len(robots) >= 2:
        warm = max(robots, key=lambda n: robots[n]["temp"])
        bits.append(f"{warm} has the warmest spot at {fmt(robots[warm]['temp'])}")
    if not bits:
        return "I'm still gathering the weather. Give me a minute."
    line = bits[0] + "".join(", " + b for b in bits[1:])
    return re.sub(r",\s*and", " and", line) + "."


def snapshot(max_age_s=600):
    with NOW_LOCK:
        cut = time.time() - max_age_s
        return {k: dict(v) for k, v in NOW.items() if v["ts"] >= cut}


# ── Her web API and page ───────────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.route("/")
def page():
    return render_template("index.html", name=NAME, port=PORT)


@app.route("/ping")
def ping():
    return f"{NAME} alive", 200, {"Content-Type": "text/plain"}


@app.route("/api/now")
def api_now():
    now = snapshot()
    now.pop("Outside", None)
    return jsonify({"outside": STATE["weather"], "location": STATE["location"], "inside": now,
                    "says": says(), "units": CFG.get("Units", "C"),
                    "problems": [p for p in (STATE["weather_error"] and f"Weather: {STATE['weather_error']}",
                                             STATE["board_error"], STATE["fleet_error"]) if p]})


@app.route("/api/history")
def api_history():
    hours = max(1.0, min(float(request.args.get("hours", 24)), CFG["HistoryDays"] * 24))
    return jsonify({"hours": hours, "series": HISTORY.series(hours)})


@app.route("/status")
def status():
    """Like any robot's /status, so the fleet (NINA, RITA, TINA) can read her too."""
    mine = snapshot().get(NAME, {})
    w = STATE["weather"] or {}
    return jsonify({"name": NAME, "mode": "weather", "temp": mine.get("temp"), "hum": mine.get("hum"),
                    "outside_temp": w.get("temp"), "outside_hum": w.get("hum"), "weather": w.get("words"),
                    "board": STATE["board"], "registered": STATE["registered"],
                    "uptime_s": round(time.time() - STARTED), "says": says()})


@app.route("/report", methods=["POST"])
def report():
    """A robot pushing its reading: {"name": "MILA", "temp": 22.5, "hum": 41} (or in "sensors")."""
    d = request.get_json(silent=True) or request.form.to_dict()
    name = str(d.get("name") or "").strip()[:24]
    if not name or name in (NAME, "Outside"):
        return jsonify({"ok": False, "error": "say who you are: name"}), 400
    t, h = climate_from(d)
    if t is None and h is None:
        return jsonify({"ok": False, "error": "no temp or hum in that"}), 400
    note_reading(name, t, h, "report")
    return jsonify({"ok": True})


def main():
    for fn in (weather_loop, board_loop, fleet_loop, heartbeat):
        threading.Thread(target=fn, daemon=True, name=fn.__name__).start()
    if LADDER:
        LADDER.start()
    if CFG.get("BluetoothServer", True):
        fleet_bt.serve_rfcomm(PORT)          # her whole API over Bluetooth serial too
    print(f"[{NAME}] her page: http://127.0.0.1:{PORT}/")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
