import os
import requests
import polyline as pl


ORS_KEY = os.getenv("ORS_API_KEY") or os.environ.get("STREAMLIT_SECRETS_ORG", None)


# ORS: https://api.openrouteservice.org/v2/directions/foot-walking
# OSRM demo: https://router.project-osrm.org/route/v1/foot/{lon},{lat};{lon},{lat}?overview=full&geometries=polyline




def _route_ors(origin, dest):
    url = "https://api.openrouteservice.org/v2/directions/foot-walking"
    headers = {"Authorization": ORS_KEY, "Content-Type": "application/json"}
    body = {"coordinates": [[origin[1], origin[0]], [dest[1], dest[0]]]} # lon,lat 順
    r = requests.post(url, headers=headers, json=body, timeout=30)
    if r.status_code != 200:
        return None
    js = r.json()
    feat = js["features"][0]
    geom = feat["geometry"]["coordinates"] # lon,lat
    coords = [(c[1], c[0]) for c in geom]
    dist = feat["properties"]["segments"][0]["distance"]
    return {"geometry": coords, "distance_m": int(dist), "polyline": ""}




def _route_osrm(origin, dest):
    url = f"https://router.project-osrm.org/route/v1/foot/{origin[1]},{origin[0]};{dest[1]},{dest[0]}?overview=full&geometries=polyline"
    r = requests.get(url, timeout=30)
    if r.status_code != 200:
        return None
    js = r.json()
    if not js.get("routes"):
        return None
    geom = js["routes"][0]["geometry"]
    coords = pl.decode(geom) # (lat, lon)
    dist = js["routes"][0]["distance"]
    return {"geometry": coords, "distance_m": int(dist), "polyline": geom}


def route_walking(origin, dest):
    if ORS_KEY:
        try:
            got = _route_ors(origin, dest)
            if got:
                return got
        except Exception:
            pass
    return _route_osrm(origin, dest)