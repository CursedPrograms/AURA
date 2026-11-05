"""AURA's tests: readings from any robot, history, her words, location and weather (no network)."""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import aura  # noqa: E402


class Resp:
    def __init__(self, data, ok=True):
        self.data, self.ok, self.status_code = data, ok, 200 if ok else 404

    def json(self):
        return self.data

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError("bad")


def test_climate_from_fleet_formats():
    assert aura.climate_from({"temp": 22.5, "hum": 41.0}) == (22.5, 41.0)              # MILA /status
    assert aura.climate_from({"F": 30, "temp": 19.0, "hum": 55.5}) == (19.0, 55.5)     # NORA /sensors
    assert aura.climate_from({"temp": -1, "hum": -1}) == (None, None)                  # NORA, no AHT10
    assert aura.climate_from({"sensors": {"temperature": 18, "humidity": 60}}) == (18.0, 60.0)
    assert aura.climate_from({"mode": "idle"}) == (None, None)
    assert aura.climate_from({"temp": 400, "hum": 150}) == (None, None)


def test_history_samples_and_prunes():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "h.csv")
        h = aura.History(path, days=1, sample_s=60)
        now = time.time()
        assert h.add("AURA", 21.0, 40.0, now - 120)
        assert not h.add("AURA", 21.5, 40.0, now - 100)       # within the sample time
        assert h.add("AURA", 22.0, 41.0, now)
        assert h.add("MILA", 25.0, None, now)
        assert [r[1] for r in h.series(1)["AURA"]] == [21.0, 22.0]
        h.rows["OLD"].append((now - 3 * 86400, 1.0, 1.0))
        h.prune()
        assert "OLD" not in h.series(100)
        again = aura.History(path, days=1, sample_s=60)       # read back from the file
        assert [r[1] for r in again.series(1)["AURA"]] == [21.0, 22.0]


def test_read_robot_tries_pages_and_remembers():
    asked = []

    def get(url, timeout=None):
        asked.append(url)
        if url == "http://10.0.0.5:5002/sensors":
            return Resp({"temp": 20.0, "hum": 50.0})
        return Resp({}, ok=False)

    robot = {"name": "NORA", "ip": "10.0.0.5", "capabilities": ["web:5002"]}
    assert aura.read_robot(robot, get=get)[:2] == (20.0, 50.0)
    asked.clear()
    aura.read_robot(robot, get=get)
    assert asked[0] == "http://10.0.0.5:5002/sensors"          # the page that worked, first


def test_says():
    w = {"temp": 12.0, "words": "light rain", "rain_chance": 80}
    now = {"AURA": {"temp": 22.0, "hum": 75.0}, "MILA": {"temp": 24.0}, "NORA": {"temp": 19.0}}
    line = aura.says(now, w)
    assert "12° and light rain outside" in line and "22° in here" in line
    assert "umbrella" in line and "muggy" in line and "MILA has the warmest spot" in line
    assert aura.says({}, None).startswith("I'm still gathering")


def test_location_and_weather(monkeypatch, tmp_path):
    monkeypatch.setattr(aura, "LOCATION_JSON", str(tmp_path / "loc.json"))
    monkeypatch.setattr(aura, "DATA", str(tmp_path))
    cfg = {"Location": {"City": "Cape Town"}}
    get = lambda url, **kw: Resp({"results": [{"latitude": -33.9, "longitude": 18.4, "name": "Cape Town", "country": "South Africa"}]})
    loc = aura.resolve_location(cfg, get=get)
    assert loc["name"] == "Cape Town, South Africa" and loc["from"] == "city"
    fixed = aura.resolve_location({"Location": {"Latitude": 1, "Longitude": 2, "Name": "Home"}})
    assert fixed["name"] == "Home"
    meteo = {"current": {"temperature_2m": 17.2, "relative_humidity_2m": 63, "apparent_temperature": 16,
                         "weather_code": 3, "wind_speed_10m": 12, "precipitation": 0, "is_day": 1},
             "daily": {"temperature_2m_max": [20], "temperature_2m_min": [11], "precipitation_probability_max": [10]}}
    w = aura.fetch_weather(loc, get=lambda url, **kw: Resp(meteo))
    assert w["words"] == "overcast" and w["temp"] == 17.2 and w["high"] == 20


def test_api(monkeypatch, tmp_path):
    monkeypatch.setattr(aura, "HISTORY", aura.History(str(tmp_path / "h.csv"), 7, 60))
    c = aura.app.test_client()
    assert c.post("/report", json={"name": "KIDA01", "temp": 23.1, "hum": 45}).get_json()["ok"]
    assert c.post("/report", json={"name": "KIDA01"}).status_code == 400
    assert c.post("/report", json={"name": "AURA", "temp": 1}).status_code == 400
    now = c.get("/api/now").get_json()
    assert now["inside"]["KIDA01"]["temp"] == 23.1
    assert "KIDA01" in c.get("/api/history?hours=1").get_json()["series"]
    assert c.get("/status").get_json()["name"] == "AURA"
    assert c.get("/").status_code == 200
