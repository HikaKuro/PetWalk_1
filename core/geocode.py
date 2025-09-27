import requests


NOMINATIM = "https://nominatim.openstreetmap.org/search"


headers = {"User-Agent": "petwalk-mvp/1.0"}


def geocode_address(address: str):
    params = {"q": address, "format": "json", "limit": 1}
    r = requests.get(NOMINATIM, params=params, headers=headers, timeout=20)
    if r.status_code != 200:
        return None
    js = r.json()
    if not js:
        return None
    return {"lat": float(js[0]["lat"]), "lon": float(js[0]["lon"]), "name": js[0].get("display_name")}