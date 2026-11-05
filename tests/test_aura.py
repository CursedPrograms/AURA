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
    saved = str(tmp_path / "location.json")
    monkeypatch.setattr(aura, "SAVED_LOCATION", saved)
    monkeypatch.setattr(aura, "COAST_JSON", str(tmp_path / "coast.json"))
    geo = lambda url, **kw: Resp({"results": [{"latitude": -33.9, "longitude": 18.4, "name": "Cape Town", "country": "South Africa"}]})
    loc = aura.resolve_location({"Location": {"City": "Cape Town"}}, get=geo)
    assert loc["name"] == "Cape Town, South Africa" and loc["from"] == "config"
    fixed = aura.resolve_location({"Location": {"Latitude": 1, "Longitude": 2, "Name": "Home"}})
    assert fixed["name"] == "Home"
    aura._detected.clear()
    net = lambda url, **kw: Resp({"latitude": 10, "longitude": 20, "city": "Somewhere", "country_name": "Land"})
    assert aura.resolve_location({}, get=net)["from"] == "network"
    assert not os.path.exists(saved)                                     # guessing never writes location.json
    assert aura.save_location("Cape Town", get=geo)["name"] == "Cape Town, South Africa"
    home = aura.resolve_location({"Location": {"Latitude": 1, "Longitude": 2}}, get=net)
    assert home["from"] == "saved" and home["name"] == "Cape Town, South Africa"   # the saved city wins
    aura.forget_location()
    assert not os.path.exists(saved) and aura.resolve_location({}, get=net)["from"] == "network"
    loc = {"lat": -33.9, "lon": 18.4}
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


def test_moon():
    full = aura.moon(aura.NEW_MOON_TS + aura.SYNODIC_DAYS / 2 * 86400)
    assert full["phase"] == "full" and full["lit"] == 100
    new = aura.moon(aura.NEW_MOON_TS + 3 * aura.SYNODIC_DAYS * 86400)
    assert new["phase"] == "new" and new["lit"] == 0
    assert aura.moon(aura.NEW_MOON_TS + 7.4 * 86400)["phase"] == "first quarter"


def test_weather_word_and_phrasebook():
    assert aura.weather_word({"code": 95, "temp": 20}) == "stormy"
    assert aura.weather_word({"code": 63, "temp": 20}) == "rainy"
    assert aura.weather_word({"code": 0, "temp": 33}) == "hot"
    assert aura.weather_word({"code": 3, "temp": 5}) == "cold"
    assert aura.weather_word({"code": 2, "temp": 20}) == "cloudy"
    book = aura.phrasebook()                       # RIFT's talk_bf.json, next to this repo
    assert book[17]["text"] == "wx?" and book[aura.WX_ANSWERS["rainy"]]["text"] == "rain"


def test_answer(monkeypatch):
    monkeypatch.setitem(aura.STATE, "weather", {"temp": 12.0, "words": "light rain", "code": 61, "rain_chance": 10,
                                                "sunrise": "2026-11-05T05:20", "sunset": "2026-11-05T18:41"})
    monkeypatch.setattr(aura, "snapshot", lambda max_age_s=600: {"MILA": {"temp": 24.0, "hum": 40.0, "ts": 0}})
    a = aura.answer("how warm is it at mila?")
    assert a["about"] == "MILA" and "MILA reads 24°" in a["answer"] and "light rain" in a["answer"]
    assert a["bf"] == {"u": 20, "text": "rain"}
    assert "rises at 05:20" in aura.answer("when is sunrise")["answer"]
    assert "moon is" in aura.answer("what's the moon doing")["answer"]
    assert "12° and light rain outside" in aura.answer("what's the weather")["answer"]
    c = aura.app.test_client()
    assert c.get("/chirp?u=99").status_code == 400
    assert c.get("/ask?q=weather").get_json()["bf"]["text"] == "rain"


def test_coming_and_air():
    hourly = {"time": [f"2026-11-05T{h:02d}:00" for h in range(13, 20)],
              "precipitation_probability": [10, 20, 30, 70, 80, 40, 10],
              "weather_code": [1, 2, 3, 61, 95, 3, 1]}
    assert aura.coming(hourly) == {"rain_at": "16:00", "rain_chance": 70.0, "storm_at": "17:00"}
    assert aura.coming({}) == {"rain_at": None, "rain_chance": None, "storm_at": None}
    air = {"current": {"us_aqi": 130, "pm2_5": 40, "grass_pollen": 80, "birch_pollen": None, "ragweed_pollen": 2}}
    a = aura.fetch_air({"lat": 1, "lon": 2}, get=lambda url, **kw: Resp(air))
    assert a["words"] == "unhealthy for sensitive people" and a["pollen_high"] == ["grass"]


def test_says_mentions_whats_coming(monkeypatch):
    monkeypatch.setitem(aura.STATE, "air", {"aqi": 160, "words": "unhealthy", "pollen_high": []})
    w = {"temp": 22.0, "words": "partly cloudy", "rain_chance": 30, "soon": {"rain_at": "16:00", "storm_at": None}}
    line = aura.says({}, w)
    assert "rain around 16:00" in line and "the air is unhealthy today" in line


def test_reading_the_numbers():
    assert aura.pressure_trend(-3.4) == "falling fast" and aura.pressure_trend(0.3) == "steady"
    assert aura.pressure_trend(1.2) == "rising" and aura.pressure_trend(None) is None
    assert aura.muggy(13.3) == "comfortable" and aura.muggy(19) == "muggy" and aura.muggy(23) == "oppressive"
    assert aura.storm_potential(1890, -3) == "high" and aura.storm_potential(600, 1) == "some"
    assert aura.storm_potential(100, 2) == "low"
    assert aura.compass(336) == "NW" and aura.compass(0) == "N" and aura.compass(170) == "S"


def test_fetch_weather_everything():
    times = [f"2026-11-05T{h:02d}:00" for h in range(10, 20)]
    meteo = {
        "current": {"temperature_2m": 27.8, "relative_humidity_2m": 41, "weather_code": 1, "wind_speed_10m": 17,
                    "wind_direction_10m": 336, "wind_gusts_10m": 45, "pressure_msl": 1014, "cloud_cover": 34,
                    "dew_point_2m": 13.3, "visibility": 17480, "cape": 1890, "lifted_index": -3,
                    "soil_temperature_0cm": 35.8, "soil_moisture_0_to_1cm": 0.1, "is_day": 1},
        "hourly": {"time": times, "pressure_msl": [1017.4, 1016.4, 1015.2, 1014.0, 1012.5, 1011.6, 1011.8, 1012.1, 1013.4, 1013.9],
                   "precipitation_probability": [0, 0, 0, 10, 30, 70, 40, 10, 0, 0], "weather_code": [1] * 10,
                   "wind_gusts_10m": [30, 30, 30, 40, 45, 55, 40, 30, 20, 20]},
        "daily": {"time": ["2026-11-04", "2026-11-05"], "daylight_duration": [47409.67, 47489.56],
                  "temperature_2m_max": [26, 28.1], "sunset": ["2026-11-04T18:26", "2026-11-05T18:27"],
                  "et0_fao_evapotranspiration": [5.9, 6.06]},
    }
    w = aura.fetch_weather({"lat": 1, "lon": 2}, get=lambda url, **kw: Resp(meteo))
    assert w["pressure_change_3h"] == -3.4 and w["pressure_trend"] == "falling fast"
    assert w["high"] == 28.1 and w["sunset"] == "2026-11-05T18:27"           # today, not yesterday
    assert w["sun"]["daylight_change_min"] == 1.3 and w["wind_dir"] == "NW"
    assert w["storm_potential"] == "high" and w["feel_of_air"] == "comfortable" and not w["fog"]
    assert w["soon"]["rain_at"] == "15:00" and w["soon"]["gusty_at"] == "15:00"
    assert w["ground"]["et0_today_mm"] == 6.06
    line = aura.says({}, w)
    assert "pressure's dropping fast" in line


def test_answers_about_the_details(monkeypatch):
    w = {"temp": 28.0, "words": "mostly clear", "pressure_hpa": 1014, "pressure_change_3h": -3.4, "pressure_trend": "falling fast",
         "sun": {"daylight_h": 13.19, "daylight_change_min": 1.3, "uv": 9.4, "sunshine_h_today": 12},
         "freezing_level_m": 4960, "precip": {}, "today_summary": {}, "uv": 10}
    monkeypatch.setitem(aura.STATE, "weather", w)
    monkeypatch.setattr(aura, "snapshot", lambda max_age_s=600: {})
    assert "storm's on its way" in aura.answer("what's the barometer doing")["answer"]
    assert "The days are getting longer" in aura.answer("are the days getting longer?")["answer"]
    assert aura.answer("will it snow")["answer"].startswith("No snow")
    assert "Wear sunscreen" in aura.answer("do I need sunscreen")["answer"]


def test_europe(monkeypatch):
    aura._europe_cache.clear()
    aq = {"current": {"birch_pollen": 60, "grass_pollen": 4, "alder_pollen": 0, "mugwort_pollen": 0, "olive_pollen": 0,
                      "ragweed_pollen": 0, "european_aqi": 31, "european_aqi_pm2_5": 31, "ammonia": 1.6},
          "hourly": {"time": ["2026-11-05T13:00", "2026-11-05T14:00"], "birch_pollen": [60, 250], "grass_pollen": [4, 3]}}
    fc = {"hourly": {"time": ["2026-11-05T13:00", "2026-11-05T14:00"], "lightning_potential": [0.0, 3.1]}}
    get = lambda url, **kw: Resp(aq if "air-quality" in url else fc)
    d = aura.fetch_europe(52.5, 13.4, get=get)
    assert d["in_europe"] and d["pollen"]["birch"]["level"] == "high" and d["pollen"]["birch"]["peak_level"] == "very high"
    assert d["pollen"]["grass"]["level"] == "low" and d["lightning"]["level"] == "likely"
    assert d["european_aqi"]["overall"] == 31 and d["european_aqi_words"] == "fair" and d["ammonia_ugm3"] == 1.6
    aura._europe_cache.clear()
    none = {"current": {"european_aqi": 71}, "hourly": {}}
    far = aura.fetch_europe(-26.2, 28.0, get=lambda url, **kw: Resp(none if "air-quality" in url else {"hourly": {}}))
    assert not far["in_europe"] and far["pollen"]["birch"]["now"] is None
    assert aura.app.test_client().get("/europe").status_code == 200


def test_colours_are_the_robots_own():
    assert aura.COLOURS["NORA"] == "#F7BF28" and aura.COLOURS["MILA"] == "#1FD4DE"
    assert aura.COLOURS["AURA"] == "#5EC8F2" and aura.COLOURS["Outside"] == "#F2A65E"


def test_rain_outlook_more_sensors_more_sure():
    now = 1_800_000_000
    rising = lambda start: [(now - 7200 + i * 600, 25.0, start + i * 1.0) for i in range(13)]   # +6 %/h
    w = {"rain_chance": 30, "soon": {"rain_chance": 40}, "pressure_trend": "falling fast", "storm_potential": "high"}
    one = aura.rain_outlook(w, {"AURA": rising(60)}, {"AURA": {"temp": 21.0, "hum": 72.0}}, now=now)
    many = aura.rain_outlook(w, {n: rising(60) for n in ("AURA", "MILA", "NORA", "KIDA01")},
                             {n: {"temp": 21.0, "hum": 72.0} for n in ("AURA", "MILA", "NORA", "KIDA01")}, now=now)
    assert one["confidence"] == "low" and many["confidence"] == "high" and many["sensors"] == 4
    assert many["chance"] > 40 and any("barometer" in r for r in many["reasons"])
    dry = aura.rain_outlook({"rain_chance": 10, "pressure_trend": "rising"}, {}, {"AURA": {"temp": 30.0, "hum": 15.0}}, now=now)
    assert dry["chance"] < 10
    assert round(aura.dew_point(20, 50), 1) == 9.3


def test_tides_and_surf():
    times = [f"2026-11-05T{h:02d}:00" for h in range(0, 13)]
    levels = [0.0, 0.5, 0.9, 1.0, 0.8, 0.3, -0.3, -0.8, -1.0, -0.7, -0.2, 0.4, 0.8]
    t = aura.tides(times, levels)
    assert [x["kind"] for x in t] == ["high", "low"] and t[0]["at"] == "02:50" and t[1]["at"] == "07:54"   # between the hours
    assert aura.surf_words(0.2, 5) == "flat" and aura.surf_words(1.4, 12) == "fun-sized, clean, long-period"


def test_find_coast_rings(monkeypatch, tmp_path):
    monkeypatch.setattr(aura, "COAST_JSON", str(tmp_path / "coast.json"))
    calls = []

    def get(url, **kw):
        if "marine" in url:
            lats = kw["params"]["latitude"].split(",")
            calls.append(len(lats))
            # sea only to the east, from 100 km out
            out = [{"current": {"wave_height": 1.0, "sea_surface_temperature": 20, "sea_level_height_msl": 0.2}
                    if float(lo) > 20.9 else {"wave_height": None}} for lo in kw["params"]["longitude"].split(",")]
            return Resp(out if len(out) > 1 else out[0])
        return Resp({"address": {"city": "Seaside"}})
    c = aura.find_coast({"lat": 0.0, "lon": 20.0}, get=get)
    assert c["name"] == "Seaside" and c["direction"] == "E" and 90 < c["km"] < 160
    here = aura.find_coast({"lat": 0.0, "lon": 21.0}, city="", get=get)   # cached for the old home: a new key
    assert here["direction"] == "here"
