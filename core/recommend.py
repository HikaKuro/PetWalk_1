from datetime import datetime


# 温度閾値（超簡易・安全側）。後でRAG/論文で精緻化。
SIZE_THRESH = {
"小型": 25.0,
"中型": 27.0,
"大型": 28.0,
}




def recommend_time_windows(hourly, size: str, age: float, weight: float):
    threshold = SIZE_THRESH.get(size, 26.0)
    # 老犬・短頭種などは厳しめに（MVPは年齢のみで補正）
    if age >= 8:
        threshold -= 1.0
    windows, cur = [], None
    for h in hourly:
        # 路面温度近似: 日中は +4℃
        hour = int(h["time"].split("T")[1][:2]) if "T" in h["time"] else 0
        t_surf = h["temp"] + (4.0 if 9 <= hour <= 16 else 0.0)
        safe = (t_surf <= threshold) and (h["wind"] >= 1.0)
        ts = h["time"].replace("T", " ")
        if safe and cur is None:
            cur = {"start": ts}
        if (not safe) and cur is not None:
            cur["end"] = ts
            windows.append(cur)
            cur = None
    if cur is not None:
        cur["end"] = hourly[-1]["time"].replace("T", " ")
        windows.append(cur)
    return windows




def score_route(route: dict, pois: list) -> int:
    # 超簡易: 経路終点の種別で加点、公園なら+30、footwayなら+15
    poi = route.get("poi", {})
    kind = poi.get("kind", "")
    base = 0
    if kind == "park":
        base += 30
    if kind in ("footway", "path"):
        base += 15
    # 距離が短いほど高得点（片道1.5kmを上限に線形減点）
    dist = max(1, route.get("distance_m", 1000))
    dist_score = max(0, int(40 * (1.5e3 - min(dist, 1.5e3)) / 1.5e3))
    return base + dist_score