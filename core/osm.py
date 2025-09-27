import requests


OVERPASS = "https://overpass-api.de/api/interpreter"


# 公園・遊歩道などを中心に抽出（MVP向け簡易版）
QUERY_TMPL = """
[out:json][timeout:25];
(
node["leisure"="park"](around:{radius},{lat},{lon});
way["leisure"="park"](around:{radius},{lat},{lon});
way["highway"="footway"](around:{radius},{lat},{lon});
way["highway"="path"](around:{radius},{lat},{lon});
);
out center 20;
"""


def get_pois(lat: float, lon: float, radius_m: int = 800):
    q = QUERY_TMPL.format(radius=radius_m, lat=lat, lon=lon)
    r = requests.post(OVERPASS, data={"data": q}, timeout=30)
    if r.status_code != 200:
        return []
    js = r.json()
    pois = []
    for el in js.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name") or tags.get("ref") or "POI"
        if "center" in el:
            lat2, lon2 = el["center"]["lat"], el["center"]["lon"]
        else:
            # nodeの場合
            lat2, lon2 = el.get("lat"), el.get("lon")
        if lat2 and lon2:
            pois.append({
                "name": name,
                "lat": lat2,
                "lon": lon2,
                "kind": tags.get("leisure") or tags.get("highway")
            })
    # 単純に距離でソート（近い順）
    pois.sort(key=lambda x: (x["lat"]-lat)**2 + (x["lon"]-lon)**2)
    return pois

