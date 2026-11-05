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
STATE = {"weather": None, "air": None, "weather_error": None, "location": None, "board": None, "board_error": None,
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


CURRENT = ("temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,wind_direction_10m,"
           "wind_gusts_10m,precipitation,rain,showers,snowfall,is_day,pressure_msl,surface_pressure,cloud_cover,"
           "cloud_cover_low,cloud_cover_mid,cloud_cover_high,dew_point_2m,visibility,cape,lifted_index,"
           "freezing_level_height,snow_depth,soil_temperature_0cm,soil_moisture_0_to_1cm,evapotranspiration,"
           "shortwave_radiation,direct_radiation,diffuse_radiation,uv_index,uv_index_clear_sky,sunshine_duration")
HOURLY = ("precipitation_probability,weather_code,pressure_msl,cape,wind_gusts_10m,cloud_cover,lightning_potential,"
          "soil_temperature_6cm,soil_moisture_1_to_3cm")
DAILY = ("temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset,uv_index_max,"
         "uv_index_clear_sky_max,precipitation_sum,rain_sum,showers_sum,snowfall_sum,precipitation_hours,"
         "wind_speed_10m_max,wind_gusts_10m_max,wind_direction_10m_dominant,sunshine_duration,daylight_duration,"
         "et0_fao_evapotranspiration,shortwave_radiation_sum")
PAST_HOURS = 3                   # the hourly list starts 3 h ago: the pressure trend


def compass(deg):
    if deg is None:
        return None
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((deg % 360) / 45 + 0.5) % 8]


def pressure_trend(change):
    """hPa over 3 hours, in words: a fast fall is the barometer's "storm coming"."""
    if change is None:
        return None
    return ("falling fast" if change <= -3 else "falling" if change <= -1 else "rising fast" if change >= 3
            else "rising" if change >= 1 else "steady")


def muggy(dew):
    """How the air feels, from the dew point (humidity alone misleads: 90 % on a cold night is dry air)."""
    if dew is None:
        return None
    return ("dry" if dew < 10 else "comfortable" if dew < 16 else "a bit humid" if dew < 18
            else "muggy" if dew < 21 else "oppressive")


def storm_potential(cape, lifted):
    """From CAPE (the energy the air has for storms, J/kg) and the lifted index (below 0: unstable)."""
    if cape is None:
        return None
    if cape >= 1000 and (lifted is None or lifted <= -2):
        return "high"
    if cape >= 500 or (lifted is not None and lifted < 0):
        return "some"
    return "low"


def fetch_weather(loc, get=requests.get):
    """Everything Open-Meteo's forecast says about now, today and the next 6 hours."""
    r = get("https://api.open-meteo.com/v1/forecast", timeout=10, headers=UA, params={
        "latitude": loc["lat"], "longitude": loc["lon"], "timezone": "auto",
        "current": CURRENT, "hourly": HOURLY, "daily": DAILY,
        "past_hours": PAST_HOURS, "forecast_hours": 7, "past_days": 1, "forecast_days": 1})
    r.raise_for_status()
    d = r.json()
    c, day, hourly = d.get("current", {}), d.get("daily", {}), d.get("hourly", {})
    today = len(day.get("time") or [None]) - 1          # [yesterday, today]
    at = lambda k, i=today: (day.get(k) or [None] * (i + 1))[i] if len(day.get(k) or []) > i else None
    n = lambda k: _num(c.get(k))
    pressures = [_num(x) for x in hourly.get("pressure_msl") or []]
    change = (round(pressures[PAST_HOURS] - pressures[0], 1)
              if len(pressures) > PAST_HOURS and None not in (pressures[0], pressures[PAST_HOURS]) else None)
    daylight, daylight_before = _num(at("daylight_duration")), _num(at("daylight_duration", today - 1)) if today else None
    gusts_ahead = [_num(x) for x in (hourly.get("wind_gusts_10m") or [])[PAST_HOURS + 1:]]
    lightning = [_num(x) for x in (hourly.get("lightning_potential") or [])[PAST_HOURS:] if _num(x) is not None]
    later = {k: v[PAST_HOURS:] for k, v in hourly.items()}   # this hour on, for coming()
    vis = n("visibility")
    return {
        "temp": n("temperature_2m"), "hum": n("relative_humidity_2m"), "feels": n("apparent_temperature"),
        "code": c.get("weather_code"), "words": describe(c.get("weather_code")), "day": bool(c.get("is_day", 1)),
        "wind_kmh": n("wind_speed_10m"), "gusts_kmh": n("wind_gusts_10m"), "wind_dir": compass(n("wind_direction_10m")),
        "rain_mm": n("precipitation"),
        "precip": {"rain_mm": n("rain"), "showers_mm": n("showers"), "snowfall_cm": n("snowfall"), "snow_depth_m": n("snow_depth")},
        "pressure_hpa": n("pressure_msl"), "surface_pressure_hpa": n("surface_pressure"),
        "pressure_change_3h": change, "pressure_trend": pressure_trend(change),
        "clouds": {"total": n("cloud_cover"), "low": n("cloud_cover_low"), "mid": n("cloud_cover_mid"), "high": n("cloud_cover_high")},
        "visibility_km": round(vis / 1000, 1) if vis is not None else None, "fog": vis is not None and vis < 1000,
        "dew_point": n("dew_point_2m"), "feel_of_air": muggy(n("dew_point_2m")),
        "cape": n("cape"), "lifted_index": n("lifted_index"), "storm_potential": storm_potential(n("cape"), n("lifted_index")),
        "lightning": max(lightning) if lightning else None,
        "freezing_level_m": n("freezing_level_height"),
        "sun": {"radiation_wm2": n("shortwave_radiation"), "direct_wm2": n("direct_radiation"), "diffuse_wm2": n("diffuse_radiation"),
                "uv": n("uv_index"), "uv_clear_sky": n("uv_index_clear_sky"),
                "sunshine_h_today": round(_num(at("sunshine_duration")) / 3600, 1) if _num(at("sunshine_duration")) is not None else None,
                "daylight_h": round(daylight / 3600, 2) if daylight is not None else None,
                "daylight_change_min": round((daylight - daylight_before) / 60, 1) if None not in (daylight, daylight_before) else None,
                "radiation_today_mj": _num(at("shortwave_radiation_sum"))},
        "ground": {"soil_temp_surface": n("soil_temperature_0cm"),
                   "soil_temp_6cm": _num((hourly.get("soil_temperature_6cm") or [None] * (PAST_HOURS + 1))[PAST_HOURS]) if len(hourly.get("soil_temperature_6cm") or []) > PAST_HOURS else None,
                   "soil_moisture_top": n("soil_moisture_0_to_1cm"),
                   "soil_moisture_1_3cm": _num((hourly.get("soil_moisture_1_to_3cm") or [None] * (PAST_HOURS + 1))[PAST_HOURS]) if len(hourly.get("soil_moisture_1_to_3cm") or []) > PAST_HOURS else None,
                   "evapotranspiration_mm": n("evapotranspiration"), "et0_today_mm": _num(at("et0_fao_evapotranspiration"))},
        "today_summary": {"precip_mm": _num(at("precipitation_sum")), "rain_mm": _num(at("rain_sum")),
                          "showers_mm": _num(at("showers_sum")), "snowfall_cm": _num(at("snowfall_sum")),
                          "precip_hours": _num(at("precipitation_hours")), "wind_max_kmh": _num(at("wind_speed_10m_max")),
                          "gust_max_kmh": _num(at("wind_gusts_10m_max")), "wind_dir": compass(_num(at("wind_direction_10m_dominant")))},
        "high": _num(at("temperature_2m_max")), "low": _num(at("temperature_2m_min")),
        "rain_chance": _num(at("precipitation_probability_max")),
        "sunrise": at("sunrise"), "sunset": at("sunset"), "uv": _num(at("uv_index_max")),
        "soon": dict(coming(later), gusty_at=next((later["time"][i + 1][-5:] for i, g in enumerate(gusts_ahead)
                                                   if g is not None and g >= 50 and i + 1 < len(later.get("time", []))), None)),
        "ts": time.time()}


def coming(hourly):
    """The next 6 hours: when rain looks likely (60 % or more, or it's already in the forecast code),
    and when a thunderstorm is forecast. {"rain_at": "16:00", "rain_chance": 70, "storm_at": None}"""
    out = {"rain_at": None, "rain_chance": None, "storm_at": None}
    times = hourly.get("time") or []
    probs = hourly.get("precipitation_probability") or []
    codes = hourly.get("weather_code") or []
    for i, t in enumerate(times[1:7], start=1):           # [0] is this hour: that's "now", not "coming"
        p = _num(probs[i]) if i < len(probs) else None
        c = codes[i] if i < len(codes) else None
        rainy = c is not None and (51 <= c <= 67 or 80 <= c <= 82 or c >= 95)
        if out["rain_at"] is None and ((p is not None and p >= 60) or rainy):
            out["rain_at"], out["rain_chance"] = t[-5:], p
        if out["storm_at"] is None and c is not None and c >= 95:
            out["storm_at"] = t[-5:]
    return out


def fetch_air(loc, get=requests.get):
    """Air quality (US AQI, PM2.5) and pollen from Open-Meteo's air-quality service. Pollen is only
    measured in Europe: elsewhere it stays None."""
    r = get("https://air-quality-api.open-meteo.com/v1/air-quality", timeout=10, headers=UA, params={
        "latitude": loc["lat"], "longitude": loc["lon"], "timezone": "auto",
        "current": "us_aqi,pm2_5,grass_pollen,birch_pollen,ragweed_pollen"})
    r.raise_for_status()
    c = r.json().get("current", {})
    pollen = {k: _num(c.get(k + "_pollen")) for k in ("grass", "birch", "ragweed")}
    pollen = {k: v for k, v in pollen.items() if v is not None}
    aqi = _num(c.get("us_aqi"))
    return {"aqi": aqi, "pm2_5": _num(c.get("pm2_5")), "pollen": pollen or None,
            "words": air_words(aqi), "pollen_high": [k for k, v in pollen.items() if v >= 50], "ts": time.time()}


def air_words(aqi):
    if aqi is None:
        return None
    return ("good" if aqi <= 50 else "moderate" if aqi <= 100 else "unhealthy for sensitive people" if aqi <= 150
            else "unhealthy" if aqi <= 200 else "very unhealthy")


def weather_loop():
    while True:
        try:
            if not STATE["location"]:
                STATE["location"] = resolve_location()
            STATE["weather"] = fetch_weather(STATE["location"])
            STATE["weather_error"] = None
            if time.time() - (STATE.get("air") or {}).get("ts", 0) > 1800:
                try:
                    STATE["air"] = fetch_air(STATE["location"])
                except Exception:
                    pass            # the weather matters more: no air quality this round
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
                "name": NAME, "type": "weather", "capabilities": f"weather,climate,web:{PORT},talk:{PORT},ask:{PORT}"})
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
    soon = (weather or {}).get("soon") or {}
    if soon.get("storm_at"):
        bits.append(f"a storm is coming around {soon['storm_at']}")
    elif soon.get("rain_at") and not (weather or {}).get("words", "").endswith("rain"):
        bits.append(f"rain around {soon['rain_at']}")
    bits += extras(weather)[:2]
    air = STATE.get("air") or {}
    if air.get("aqi") is not None and air["aqi"] > 100:
        bits.append(f"the air is {air['words']} today")
    if air.get("pollen_high"):
        bits.append(f"lots of {' and '.join(air['pollen_high'])} pollen about")
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


def extras(w, hour=None):
    """What else is worth a word, most important first: the barometer, storm energy, gusts, fog,
    how the air feels, frost, and a clear night for the stars."""
    if not w:
        return []
    hour = time.localtime().tm_hour if hour is None else hour
    out = []
    if w.get("pressure_trend") == "falling fast":
        out.append("the pressure's dropping fast")
    if w.get("storm_potential") == "high" and not (w.get("soon") or {}).get("storm_at"):
        out.append("the air has the energy for a thunderstorm later")
    gust = w.get("gusts_kmh")
    if gust is not None and gust >= 50:
        out.append(f"gusts up to {gust:.0f} km/h")
    elif (w.get("soon") or {}).get("gusty_at"):
        out.append(f"gusty from about {w['soon']['gusty_at']}")
    if w.get("fog"):
        out.append(f"foggy, only {w.get('visibility_km')} km of visibility")
    if w.get("feel_of_air") in ("muggy", "oppressive"):
        out.append(f"it feels {w['feel_of_air']}")
    if w.get("temp") is not None and w["temp"] <= 2 or ((w.get("ground") or {}).get("soil_temp_surface") or 99) <= 0:
        out.append("frost is possible")
    clouds = (w.get("clouds") or {}).get("total")
    m = moon()
    if (hour >= 18 or hour < 4) and clouds is not None and clouds < 25:
        out.append("a clear night for the stars" + (" and a full moon" if m["phase"] == "full" else ""))
    return out


def snapshot(max_age_s=600):
    with NOW_LOCK:
        cut = time.time() - max_age_s
        return {k: dict(v) for k, v in NOW.items() if v["ts"] >= cut}


# ── Europe only: pollen, ammonia, lightning (Open-Meteo has them from European models) ────────
POLLENS = ("alder", "birch", "grass", "mugwort", "olive", "ragweed")
EU_AQI = ("european_aqi", "european_aqi_pm2_5", "european_aqi_pm10", "european_aqi_nitrogen_dioxide",
          "european_aqi_ozone", "european_aqi_sulphur_dioxide")
_europe_cache = {}               # (lat, lon) -> (when, answer)
EUROPE_CACHE_S = 1800


def pollen_level(grains):
    """Grains per m³ in words (a rough scale across the six kinds)."""
    if grains is None:
        return None
    return ("none" if grains < 1 else "low" if grains < 10 else "moderate" if grains < 50
            else "high" if grains < 200 else "very high")


def lightning_level(lpi):
    """The lightning potential index (J/kg): 0 is none."""
    if lpi is None:
        return None
    return "none" if lpi <= 0.05 else "possible" if lpi < 2 else "likely"


def eu_aqi_words(v):
    if v is None:
        return None
    return ("good" if v <= 20 else "fair" if v <= 40 else "moderate" if v <= 60 else "poor" if v <= 80
            else "very poor" if v <= 100 else "extremely poor")


def fetch_europe(lat, lon, get=requests.get):
    """Pollen (now and the next 24 h), ammonia and the European AQI from the air-quality service, and the
    lightning potential for the next 12 h from the forecast. Outside Europe the European-only values are None,
    and "in_europe" says so."""
    key = (round(lat, 2), round(lon, 2))
    hit = _europe_cache.get(key)
    if hit and time.time() - hit[0] < EUROPE_CACHE_S:
        return hit[1]
    aq = get("https://air-quality-api.open-meteo.com/v1/air-quality", timeout=15, headers=UA, params={
        "latitude": lat, "longitude": lon, "timezone": "auto",
        "current": ",".join([f"{k}_pollen" for k in POLLENS] + list(EU_AQI) + ["ammonia", "dust"]),
        "hourly": ",".join(f"{k}_pollen" for k in POLLENS), "forecast_hours": 24})
    aq.raise_for_status()
    a = aq.json()
    cur, hourly = a.get("current", {}), a.get("hourly", {})
    fc = get("https://api.open-meteo.com/v1/forecast", timeout=15, headers=UA, params={
        "latitude": lat, "longitude": lon, "timezone": "auto", "hourly": "lightning_potential", "forecast_hours": 12})
    fc.raise_for_status()
    f = fc.json().get("hourly", {})
    lpi = [_num(x) for x in f.get("lightning_potential") or []]
    pollen = {}
    for k in POLLENS:
        now = _num(cur.get(f"{k}_pollen"))
        ahead = [_num(x) for x in hourly.get(f"{k}_pollen") or []]
        peak = max((x for x in ahead if x is not None), default=None)
        pollen[k] = {"now": now, "level": pollen_level(now), "peak_24h": peak, "peak_level": pollen_level(peak),
                     "hours": [[t[-5:], v] for t, v in zip(hourly.get("time") or [], ahead)]}
    lpi_known = [x for x in lpi if x is not None]
    answer = {
        "in_europe": any(p["now"] is not None for p in pollen.values()) or bool(lpi_known),
        "pollen": pollen,
        "ammonia_ugm3": _num(cur.get("ammonia")), "dust_ugm3": _num(cur.get("dust")),
        "european_aqi": {k.replace("european_aqi_", "").replace("european_aqi", "overall"): _num(cur.get(k)) for k in EU_AQI},
        "european_aqi_words": eu_aqi_words(_num(cur.get("european_aqi"))),
        "lightning": {"now": lpi_known[0] if lpi_known else None, "max_12h": max(lpi_known) if lpi_known else None,
                      "level": lightning_level(max(lpi_known)) if lpi_known else None,
                      "hours": [[t[-5:], v] for t, v in zip(f.get("time") or [], lpi)]},
        "ts": time.time()}
    _europe_cache[key] = (time.time(), answer)
    return answer


def geocode(city, get=requests.get):
    r = get("https://geocoding-api.open-meteo.com/v1/search", params={"name": city, "count": 1}, timeout=10, headers=UA)
    hit = (r.json().get("results") or [None])[0]
    if not hit:
        return None
    return {"lat": hit["latitude"], "lon": hit["longitude"],
            "name": ", ".join(x for x in (hit.get("name"), hit.get("country")) if x)}


# ── The moon (no internet needed: days since a known new moon) ─────────────────────────────────
SYNODIC_DAYS = 29.530588853
NEW_MOON_TS = 947182440          # 2000-01-06 18:14 UTC
MOON_NAMES = ["new", "waxing crescent", "first quarter", "waxing gibbous",
              "full", "waning gibbous", "last quarter", "waning crescent"]
MOON_ICONS = ["\U0001F311", "\U0001F312", "\U0001F313", "\U0001F314",
              "\U0001F315", "\U0001F316", "\U0001F317", "\U0001F318"]


def moon(ts=None):
    """Phase name, how lit (0-100 %), its age in days, the days to the next full moon, and an icon."""
    age = ((ts or time.time()) - NEW_MOON_TS) / 86400 % SYNODIC_DAYS
    f = age / SYNODIC_DAYS
    i = int(f * 8 + 0.5) % 8
    lit = (1 - math.cos(2 * math.pi * f)) / 2 * 100
    to_full = (SYNODIC_DAYS / 2 - age) % SYNODIC_DAYS
    return {"phase": MOON_NAMES[i], "icon": MOON_ICONS[i], "lit": round(lit), "age_days": round(age, 1),
            "days_to_full": round(to_full, 1)}


# ── Brainfuck, like the rest of the fleet: RIFT's phrasebook (Fleet/talk_bf.json) ──────────────
# 17 is "wx?" (DREAM asking for the weather), 18-25 AURA's answers: sun, cloud, rain, fog, snow, storm, hot, cold.
TALK_JSON = os.path.join(HERE, "..", "RIFT", "Fleet", "talk_bf.json")
WX_ANSWERS = {"sunny": 18, "cloudy": 19, "rainy": 20, "foggy": 21, "snowy": 22, "stormy": 23, "hot": 24, "cold": 25}
TONES = {"+": 1568, "-": 784, ">": 1319, "<": 1047, "[": 880, "]": 1175, ".": 2093, ",": 523}
VOICE_PITCH = 1.25               # hers is light and airy (NINA's is 0.85, DREAM's deep)


def phrasebook():
    try:
        with open(TALK_JSON, encoding="utf-8") as f:
            return {u["index"]: u for u in json.load(f)}
    except (OSError, ValueError, KeyError):
        return {}


def weather_word(w):
    """The sky in one Brainfuck answer: storm, snow, rain or fog first, then hot or cold, else sun or cloud."""
    if not w:
        return None
    code, t = w.get("code") or 0, w.get("temp")
    if code >= 95:
        return "stormy"
    if 71 <= code <= 77 or code in (85, 86):
        return "snowy"
    if 51 <= code <= 67 or 80 <= code <= 82:
        return "rainy"
    if code in (45, 48):
        return "foggy"
    if t is not None and t >= 30:
        return "hot"
    if t is not None and t <= 8:
        return "cold"
    return "sunny" if code <= 1 else "cloudy"


def chirp(u):
    """Beeps utterance u's Brainfuck program, one tone per symbol, in her own pitch (Windows)."""
    entry = phrasebook().get(u)
    if not entry or os.name != "nt":
        return False
    import winsound
    for ch in entry["bf"]:
        winsound.Beep(max(37, min(32767, int(TONES.get(ch, 1000) * VOICE_PITCH))), 80 if ch == "." else 28)
    return True


# ── Asking her: the weather, a robot's readings, the moon, a story ─────────────────────────────
def _robot_in(q, names):
    flat = re.sub(r"[^a-z0-9]", "", q.lower())
    return next((n for n in sorted(names, key=len, reverse=True) if re.sub(r"[^a-z0-9]", "", n.lower()) in flat), None)


def answer(q):
    """Her answer in words, and her Brainfuck reply (the sky in one word). Whatever you ask, she
    finds a way back to the weather."""
    q = (q or "").strip()
    w = STATE["weather"]
    now = snapshot()
    word = weather_word(w)
    reply = {"u": WX_ANSWERS[word], "text": phrasebook().get(WX_ANSWERS[word], {}).get("text", word)} if word else None
    sky = f" Outside it's {fmt(w['temp'])} and {w['words']}, by the way." if w and w.get("temp") is not None else ""
    low = q.lower()
    who = _robot_in(q, [n for n in now if n != "Outside"])
    if who:
        r = now[who]
        bits = [fmt(r["temp"]) if r.get("temp") is not None else None,
                f"{r['hum']:.0f}% humidity" if r.get("hum") is not None else None]
        text = f"{'In here it reads' if who == NAME else who + ' reads'} {' and '.join(b for b in bits if b)}." + sky
    elif "moon" in low:
        m = moon()
        text = f"The moon is {m['phase']}, {m['lit']}% lit." + (
            " It's full tonight!" if m["phase"] == "full" else f" Full moon in {m['days_to_full']:.0f} days.") + sky
    elif "sun" in low and ("rise" in low or "set" in low) and w and w.get("sunrise"):
        text = f"The sun rises at {w['sunrise'][-5:]} and sets at {w['sunset'][-5:]}." + sky
    elif w and re.search(r"pressure|barometer", low):
        text = (f"The pressure is {w.get('pressure_hpa'):.0f} hPa and {w.get('pressure_trend')}"
                f" ({w.get('pressure_change_3h'):+.1f} in 3 hours)." if w.get("pressure_hpa") is not None else "I don't know the pressure yet.")
        if w.get("pressure_trend") == "falling fast":
            text += " That usually means a storm's on its way."
    elif w and re.search(r"\b(wind|windy|gust|gusts|gusty)\b", low):
        t = w.get("today_summary") or {}
        text = (f"Wind {w.get('wind_kmh') or 0:.0f} km/h from the {w.get('wind_dir') or '?'}, gusting to {w.get('gusts_kmh') or 0:.0f}."
                f" Today's strongest gust: {t.get('gust_max_kmh') or 0:.0f} km/h.") + sky
    elif w and re.search(r"cloud|stars|stargaz", low):
        c = w.get("clouds") or {}
        text = (f"Right now clouds cover {c.get('total') or 0:.0f}% of the sky: {c.get('low') or 0:.0f}% low, {c.get('mid') or 0:.0f}% middle, "
                f"{c.get('high') or 0:.0f}% high.")
        if "star" in low:
            text += " Good for stargazing tonight." if (c.get("total") or 100) < 25 else " Not a great night for the stars."
            m = moon()
            text += f" The moon is {m['phase']}, {m['lit']}% lit."
    elif w and re.search(r"muggy|dew|sticky|humid", low):
        text = (f"The dew point is {w.get('dew_point'):.0f}°, so the air feels {w.get('feel_of_air')}"
                f" ({w.get('hum'):.0f}% humidity)." if w.get("dew_point") is not None else says())
    elif w and re.search(r"fog|visib", low):
        text = f"You can see about {w.get('visibility_km')} km." + (" It's foggy." if w.get("fog") else "")
    elif w and re.search(r"thunder|lightning|storm", low):
        text = (f"Storm potential is {w.get('storm_potential')} (CAPE {w.get('cape') or 0:.0f} J/kg, lifted index "
                f"{w.get('lifted_index') if w.get('lifted_index') is not None else '?'}), and the pressure is {w.get('pressure_trend')}.")
        if (w.get("soon") or {}).get("storm_at"):
            text += f" A storm is forecast around {w['soon']['storm_at']}."
    elif w and re.search(r"soil|garden|plants|water the|evapo", low):
        g = w.get("ground") or {}
        text = (f"The soil is {g.get('soil_temp_surface') or 0:.0f}° on top and {g.get('soil_temp_6cm') or 0:.0f}° 6 cm down. "
                f"Moisture {(g.get('soil_moisture_top') or 0) * 100:.0f}% at the surface, and plants will lose about "
                f"{g.get('et0_today_mm') or 0:.1f} mm of water today.")
        if (g.get("soil_moisture_top") or 1) < 0.15:
            text += " The ground's dry: water them."
    elif w and re.search(r"daylight|days? (are |is )?(getting )?(longer|shorter)", low):
        sun = w.get("sun") or {}
        ch = sun.get("daylight_change_min")
        text = f"{sun.get('daylight_h') or 0:.1f} hours of daylight today" + (
            f", {abs(ch):.1f} minutes {'longer' if ch > 0 else 'shorter'} than yesterday." if ch else ".")
        if ch:
            text += " The days are getting longer." if ch > 0 else " The days are getting shorter."
    elif w and re.search(r"\buv\b|sunscreen|sunburn|sunshine", low):
        sun = w.get("sun") or {}
        text = (f"UV is {sun.get('uv') or 0:.0f} now, up to {w.get('uv') or 0:.0f} today. {sun.get('sunshine_h_today') or 0:.0f} hours "
                f"of sunshine today and {sun.get('daylight_h') or 0:.1f} hours of daylight")
        ch = sun.get("daylight_change_min")
        text += (f", {abs(ch):.1f} minutes {'longer' if ch > 0 else 'shorter'} than yesterday." if ch else ".")
        if (sun.get("uv") or 0) >= 6:
            text += " Wear sunscreen."
    elif w and re.search(r"snow|freez|frost", low):
        pr = w.get("precip") or {}
        snowing = (pr.get("snow_depth_m") or 0) > 0 or ((w.get("today_summary") or {}).get("snowfall_cm") or 0) > 0
        if not snowing and (w.get("temp") or 0) > 3:
            text = f"No snow: it's {fmt(w.get('temp'))}, and it only freezes {w.get('freezing_level_m') or 0:.0f} m up."
        else:
            text = (f"The freezing level is {w.get('freezing_level_m') or 0:.0f} m up. Snow depth {pr.get('snow_depth_m') or 0:.2f} m, "
                    f"snowfall today {((w.get('today_summary') or {}).get('snowfall_cm') or 0):.1f} cm.")
    elif "story" in low:
        text = weather_story_now()
    else:
        text = says()
    return {"answer": text, "bf": reply, "about": who or ("moon" if "moon" in low else "weather")}


def weather_story_now(seed=None):
    import story
    w = STATE["weather"] or {}
    t = w.get("temp")
    feel = "hot" if t is not None and t >= 28 else "cold" if t is not None and t <= 10 else "nice"
    m = moon()
    return story.weather_story(w.get("words", "sunny weather"), feel, moon=m["phase"] if m["lit"] > 0 else None, seed=seed)


# ── Her web API and page ───────────────────────────────────────────────────────────────────────
app = Flask(__name__)


@app.route("/")
def page():
    return render_template("index.html", name=NAME, port=PORT)


@app.route("/europe")
def europe_page():
    return render_template("europe.html", name=NAME)


@app.route("/api/europe")
def api_europe():
    """GET /api/europe (her own location) or ?city=Berlin: pollen, ammonia, lightning, the European AQI."""
    city = (request.args.get("city") or "").strip()[:60]
    try:
        loc = geocode(city) if city else (STATE["location"] or resolve_location())
        if not loc:
            return jsonify({"error": f"I couldn't find {city}."}), 404
        return jsonify(dict(fetch_europe(loc["lat"], loc["lon"]), where=loc.get("name"), mine=not city))
    except Exception as e:
        return jsonify({"error": f"Open-Meteo didn't answer ({type(e).__name__})."}), 502


@app.route("/ping")
def ping():
    return f"{NAME} alive", 200, {"Content-Type": "text/plain"}


@app.route("/api/now")
def api_now():
    now = snapshot()
    now.pop("Outside", None)
    return jsonify({"outside": STATE["weather"], "location": STATE["location"], "inside": now,
                    "says": says(), "units": CFG.get("Units", "C"), "moon": moon(), "air": STATE.get("air"),
                    "sky_word": weather_word(STATE["weather"]),
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
                    "moon": moon()["phase"], "uptime_s": round(time.time() - STARTED), "says": says()})


@app.route("/ask")
def ask_route():
    """GET /ask?q=what's the weather / how warm is MILA / the moon / tell me a story. DREAM asks her
    this way; with chirp=1 she also beeps her Brainfuck answer (the sky in one word)."""
    a = answer(request.args.get("q", ""))
    if request.args.get("chirp") == "1" and a["bf"]:
        threading.Thread(target=chirp, args=(a["bf"]["u"],), daemon=True).start()
    return jsonify(a)


@app.route("/story")
def story_route():
    """A weather story from the TinyStories model (NINA's storyteller), opened with the real sky."""
    try:
        return jsonify({"story": weather_story_now()})
    except Exception as e:
        return jsonify({"error": f"the storyteller couldn't start: {e}"}), 503


@app.route("/chirp")
def chirp_route():
    """RIFT's conversations (talk:): /chirp?u=<n> beeps that utterance, in her voice. 0-16 like every
    robot, 17-25 the weather."""
    try:
        u = int(request.args.get("u", ""))
    except ValueError:
        u = -1
    if u not in phrasebook():
        return jsonify({"ok": False, "error": "use /chirp?u= an utterance from RIFT's talk_bf.json"}), 400
    threading.Thread(target=chirp, args=(u,), daemon=True).start()
    return jsonify({"ok": True})


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
