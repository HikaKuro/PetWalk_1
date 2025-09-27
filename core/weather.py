import requests
from datetime import datetime, timedelta


BASE = "https://api.open-meteo.com/v1/forecast"


def get_hourly_weather(lat: float, lon: float, hours: int = 24, tz: str = "Asia/Tokyo"):
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ["temperature_2m", "relative_humidity_2m", "wind_speed_10m"],
        "timezone": tz,
    }
    r = requests.get(BASE, params=params, timeout=20)
    r.raise_for_status()
    js = r.json()
    out = []
    for i, t in enumerate(js["hourly"]["time"]):
        out.append({
            "time": t,
            "temp": js["hourly"]["temperature_2m"][i],
            "rh": js["hourly"]["relative_humidity_2m"][i],
            "wind": js["hourly"]["wind_speed_10m"][i],
        })
    return out[:hours]