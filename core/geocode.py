import re, unicodedata, time
import requests


NOMINATIM = "https://nominatim.openstreetmap.org/search"

HEADERS = {
    "User-Agent": "petwalk-mvp/1.0",
    "Accept-Language": "ja",
}

def _normalize_jp(addr: str) -> str:
    s = unicodedata.normalize("NFKC", addr or "")
    # 括弧内・カッコ内の注記除去
    s = re.sub(r"（.*?）|\(.*?\)", "", s)
    # 建物名っぽい語＋以降を削る（ざっくり）
    s = re.sub(r"(マンション|アパート|ハイツ|コーポ|ビル|タワー|レジデンス|メゾン)[^、,／/]*", "", s)
    # 号室表現を削る
    s = re.sub(r"[0-9０-９]+号室", "", s)
    # ハイフン類を半角ハイフンに統一
    s = re.sub(r"[‐-‒–—―ー−－〜~]", "-", s)
    # 丁目/番地/番/号 → ハイフン化（号は削除）
    s = s.replace("丁目", "-").replace("番地", "-").replace("番", "-").replace("号", "")
    # 連続ハイフンや余分な空白を整理
    s = re.sub(r"-{2,}", "-", s)
    s = " ".join(s.split())
    # 日本の限定を助ける
    if "日本" not in s and "Japan" not in s:
        s += " 日本"
    return s

def _query(q: str):
    params = {
        "q": q,
        "format": "json",
        "limit": 1,
        "countrycodes": "jp",
        "addressdetails": 1,
    }
    r = requests.get(NOMINATIM, params=params, headers=HEADERS, timeout=20)
    if r.status_code != 200:
        return None
    js = r.json()
    if not js:
        return None
    return {
        "lat": float(js[0]["lat"]),
        "lon": float(js[0]["lon"]),
        "name": js[0].get("display_name"),
    }

def geocode_address(address: str):
    # 1) そのまま
    for q in (address, _normalize_jp(address)):
        hit = _query(q)
        if hit:
            return hit
        time.sleep(1)  # Nominatimの礼儀（レート制限対策）

    # 2) 末尾を段階的に切り落として再検索（例: 1-2-3 → 1-2 まで）
    s = _normalize_jp(address)
    if "-" in s:
        parts = s.split("-")
        for cut in range(len(parts) - 1, 1, -1):
            q2 = "-".join(parts[:cut])
            hit = _query(q2)
            if hit:
                return hit
            time.sleep(1)

    return None