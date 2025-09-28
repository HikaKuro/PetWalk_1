import os
from datetime import datetime, timedelta
import json, sqlite3, uuid, time
import streamlit as st
from streamlit_cookies_manager import EncryptedCookieManager
import pydeck as pdk
from streamlit_geolocation import streamlit_geolocation
from core.store import (
    load_user_settings, save_user_settings,
    add_location, list_locations,
    add_reco, list_recos, get_reco
)

from core.weather import get_hourly_weather
from core.geocode import geocode_address
from core.osm import get_pois
from core.routing import route_walking
from core.recommend import recommend_time_windows, score_route
from core.coupon import issue_coupon_qr
from core.db import DB
from core.recommend import SIZE_THRESH  # ファイル冒頭でのimportにまとめてもOK

# --- Cookie 管理（パスワードは Secrets へ） ---
COOKIES = EncryptedCookieManager(
    prefix="petwalk_",
    password=st.secrets.get("COOKIES_PASSWORD", "dev-only-override")  # 本番は Secrets に入れてね
)
if not COOKIES.ready():  # 初回だけ Cookie 初期化で1回止まる
    st.stop()

def get_user_id() -> str:
    if "uid" in COOKIES:
        return COOKIES["uid"]
    uid = str(uuid.uuid4())
    COOKIES["uid"] = uid
    COOKIES.save()
    return uid

def _poi_display_name(poi: dict | None) -> str:
    """OSMのPOIにユーザー向けの表示名を与える"""
    if not poi:
        return "目的地（名称なし）"
    name = poi.get("name")
    if name and name != "POI":
        return str(name)

    # 種別→日本語ラベルのフォールバック
    kind = poi.get("kind") or poi.get("leisure") or poi.get("highway")
    kind_map = {
        "park": "公園",
        "dog_park": "ドッグラン",
        "footway": "遊歩道",
        "path": "小道",
        "pedestrian": "歩行者道路",
    }
    base = kind_map.get(kind, "目的地")
    return f"{base}（名称なし）"

@st.cache_data(ttl=600, show_spinner=False)
def _cached_weather(lat: float, lon: float, hours: int = 48):
    # 失敗時でも扱いやすいように空配列を返す
    return get_hourly_weather(lat, lon, hours=hours) or []

@st.cache_data(ttl=600, show_spinner=False)
def _cached_pois(lat: float, lon: float, radius_m: int):
    return get_pois(lat, lon, radius_m=radius_m) or []

# ★ 追加: WMO weathercode を日本語の簡易カテゴリへ
def _wmo_to_label_icon(code: int):
    try:
        c = int(code)
    except Exception:
        return ("不明", "❓")
    if c in (0, 1, 2):                  # 快晴〜晴れ
        return ("晴れ", "☀️")
    if c == 3:                           # くもり
        return ("曇り", "☁️")
    if c in (45, 48):                    # 霧
        return ("霧", "🌫️")
    if (51 <= c <= 67) or (80 <= c <= 82):  # 霧雨/雨/にわか雨
        return ("雨", "🌧️")
    if (71 <= c <= 77) or (85 <= c <= 86):  # 雪/にわか雪
        return ("雪", "🌨️")
    if 95 <= c <= 99:                    # 雷雨/ひょう
        return ("雷雨", "⛈️")
    return ("不明", "❓")


st.set_page_config(page_title="PetWalk+ MVP", layout="wide")

# --- Sidebar: 犬プロフィール & 位置入力 ---
with st.sidebar:
    st.header("プロフィール")
        
    uid = get_user_id()
    prefs = load_user_settings(uid)
    # 事前に session_state へ既定値を流し込む（key ベースで初期選択される）
    for k, v in prefs.items():
        st.session_state.setdefault(k, v)

    # ▼ 既存ウィジェット（例）— key を必ず付ける！
    dog_size = st.sidebar.selectbox("犬のサイズ", ["小型", "中型", "大型"], key="dog_size")
    dog_breed = st.sidebar.text_input("犬種（任意）", key="dog_breed")
    age_years = st.sidebar.number_input("年齢（歳）", min_value=0, max_value=30, step=1, key="age_years")
    weight_kg = st.sidebar.number_input("体重（kg）", min_value=0.0, max_value=100.0, step=0.5, key="weight_kg")
    address_txt = st.sidebar.text_input("住所（任意）", key="address_txt")
    # ほかにもサイドバー項目があれば同様に key を付ける

    if st.sidebar.button("設定を保存する", use_container_width=True):
        payload = {
            "dog_size": st.session_state.get("dog_size"),
            "dog_breed": st.session_state.get("dog_breed"),
            "age_years": st.session_state.get("age_years"),
            "weight_kg": st.session_state.get("weight_kg"),
            "address_txt": st.session_state.get("address_txt"),
        }
        save_user_settings(uid, payload)
        st.sidebar.success("設定を保存しました")

# --- Tabs ---
TAB1, TAB2, TAB3 = st.tabs(["散歩おすすめ", "散歩ナビ", "実績"])

# --- DB 初期化（最初に一度だけ） ---
db = DB("sqlite:///petwalk_mvp.db")

# --- Session defaults ---
ss = st.session_state
ss.setdefault("latlon", None)            # (lat, lon)
ss.setdefault("routes", [])              # ルート候補リスト
ss.setdefault("windows", [])             # 時間帯ウィンドウ
ss.setdefault("tw_rows", [])             # 表示用の「時間帯・天気・スコア」行
ss.setdefault("selected_route_idx", 0)   # 選択中ルート

# 状態保持
if "last_plan" not in st.session_state:
    st.session_state.last_plan = None  

with TAB1:
    st.subheader("散歩の時間帯 & ルートをおすすめします") 
    # --- Session 初期化 ---
    ss = st.session_state
    ss.setdefault("latlon", None)         # (lat, lon)
    ss.setdefault("getloc_mode", False)   # 現在地取得モード（ボタン押下後だけ有効）
    # 以降で使う安全な参照
    lat, lon = ss.latlon if ss.latlon else (None, None)
# === Step 1｜位置を決める ===
    with st.container(border=True):
        mode = st.radio("位置の取得方法", ["📍 現在地を使う", "🧭 住所を入力"], horizontal=True)

        if mode == "📍 現在地を使う":
            # 1) ボタンで取得モードに入る（次回以降の再実行でもコンポーネントを継続表示）
            if st.button("📍 現在地を取得", type="primary", use_container_width=True,
                         help="ブラウザの位置情報アクセスを『許可』してください"):
                ss.getloc_mode = True

            # 2) 取得モード中は geolocation コンポーネントを表示し続ける
            if ss.getloc_mode:
                loc = streamlit_geolocation()
                if loc and (loc.get("latitude") is not None) and (loc.get("longitude") is not None):
                    lat = float(loc["latitude"])
                    lon = float(loc["longitude"])
                    ss.latlon = (lat, lon)
                    ss.getloc_mode = False  # 取得できたらモード終了
                    st.success(f"現在地をセットしました: {lat:.5f}, {lon:.5f}")
                else:
                    st.info("位置情報の許可を与えるか、取得が完了するまでお待ちください。")

        else:
            # 住所入力ルート（address_txt は使わず、この場で完結）
            addr = st.text_input("住所・ランドマーク・駅名を入力")
            set_by_addr = st.button("🔎 住所から位置を設定", use_container_width=True, disabled=(not addr))
            if set_by_addr and addr:
                ge = geocode_address(addr)  # 戻り: {"lat": .., "lon": ..} を想定
                if ge:
                    lat, lon = ge["lat"], ge["lon"]
                    ss.latlon = (lat, lon)
                    st.success(f"位置をセットしました: {lat:.5f}, {lon:.5f}")
                else:
                    st.error("住所から位置を取得できませんでした。表記を変えて再試行してください。")

    colA, colB = st.columns([2, 1])
    with colA:
        go = st.button("おすすめ開始", type="primary", use_container_width=True)
    with colB:
        radius = st.slider("探索半径(m)", 300, 2000, 800, 100)

    # --- 計算（ボタン押下時のみ再計算） ---
    if go and (lat is not None) and (lon is not None):
        with st.spinner("おすすめを算出中…"):
            try:
                # 1) 天気（48h）
                wx = _cached_weather(lat, lon, hours=48)
                if not wx:
                    st.warning("天気データを取得できませんでした。少し時間をおいて再試行してください。")
                    ss.wx = None
                    ss.windows = []
                    ss.routes = []
                else:
                    ss.wx = wx

                    # 2) 時間帯ウィンドウ
                    windows = recommend_time_windows(wx, size=dog_size, age=age_years, weight=weight_kg)
                    ss.windows = windows


                    # 3) POI → 経路 → スコア
                    pois = _cached_pois(lat, lon, radius_m=radius)
                    MAX_ROUTES = 3            # ★ 追加：上限3件
                    routes = []

                    for p in pois:
                        r = route_walking((lat, lon), (p["lat"], p["lon"]))  # dict: {"geometry": [...], "distance_m": int, "polyline": str}
                        if not r or not r.get("geometry"):
                            continue

                        # 距離[m]を概算してUIに出す
                        geom = r["geometry"]  # [(lat, lon), ...]
                        dist_m = r.get("distance_m")
                        if dist_m is None:
                            def _haversine_m(a, b):
                                from math import radians, sin, cos, asin, sqrt
                                lat1, lon1 = a; lat2, lon2 = b
                                R = 6371000.0
                                dlat = radians(lat2 - lat1); dlon = radians(lon2 - lon1)
                                x = sin(dlat/2)**2 + cos(radians(lat1))*cos(radians(lat2))*sin(dlon/2)**2
                                return 2 * R * asin(sqrt(x))
                            dist_m = int(sum(_haversine_m(geom[i], geom[i+1]) for i in range(len(geom)-1)))

                        route_dict = {**r, "poi": p, "distance_m": int(dist_m)}
                        rscore = score_route(route_dict, [])   # 第2引数は未使用
                        routes.append({**route_dict, "score": int(rscore)})
                        # ★ 追加：3件溜まったら打ち止め（余計なルーティングAPIを叩かない）
                        if len(routes) >= MAX_ROUTES:
                            break

                    routes.sort(key=lambda r: (-(r["score"]), r.get("distance_m", 10**12)))
                    routes = routes[:MAX_ROUTES]               # ★ 追加
                    ss.routes = routes
                    ss.selected_route_idx = 0
                    ss.__just_recommended = True   # ★ このフラグを立てる


            except Exception as e:
                st.error(f"おすすめ計算中にエラー: {e}")
                ss.wx = None
                ss.windows = []
                ss.routes = []

    if go and (lat is not None) and (lon is not None):
        add_location(
            uid, lat, lon,
            address=st.session_state.get("address_txt"),
            accuracy=st.session_state.get("geo_accuracy"),
            source="geolocation" if st.session_state.get("getloc_mode") else "geocode"
        )


    # 5) ルート切り替えUI（セッションに選択状態を保持）
    routes = ss.get("routes", [])
    selected_idx = ss.get("selected_route_idx", 0)
    latlon = ss.get("latlon", None)

    if routes:
        labels = [
            f"候補{i+1}: {_poi_display_name(r['poi'])} / {r.get('distance_m',0)/1000:.1f}km / スコア{r['score']}"
            for i, r in enumerate(routes)
        ]
        sel = st.selectbox(
            "おすすめルート",
            labels,
            help="スコア＝行き先（公園・遊歩道など）と距離の短さを合わせた簡易評価。高いほど散歩向きです。",
            index=min(selected_idx, len(labels)-1),
            key="route_select",       
        )
        selected_idx = labels.index(sel)
        ss.selected_route_idx = selected_idx

        tabs = st.tabs([f"候補{i+1}" for i in range(len(routes))])
        for i, t in enumerate(tabs):
            with t:
                r = routes[i]
                st.markdown(f"**{_poi_display_name(r['poi'])}** / スコア: {r['score']}")
                if latlon:
                    gmaps_url = f"https://www.google.com/maps/dir/?api=1&origin={latlon[0]},{latlon[1]}&destination={r['poi']['lat']},{r['poi']['lon']}&travelmode=walking"
                    st.link_button("Googleマップでナビ", gmaps_url, use_container_width=True)
                if st.button("このルートをプランに保存", key=f"save_plan_{i}"):
                    plan_id = db.save_plan(
                        origin_lat=latlon[0], origin_lon=latlon[1],
                        dest_lat=r['poi']['lat'], dest_lon=r['poi']['lon'],
                        polyline=r.get('polyline', ''), windows=ss.windows, score=r['score']
                    )
                    st.session_state.last_plan = {
                        "id": plan_id,
                        "origin": (latlon[0], latlon[1]),
                        "dest": (r['poi']['lat'], r['poi']['lon'])
                    }
                    st.success("保存しました。Tab2でクーポン発行まで進められます。")

    # 6) 地図表示
    layers = []
    if routes and latlon:
        SELECTED_COLOR = [0, 153, 255]
        OTHER_COLOR = [170, 170, 170]
        for i, r in enumerate(routes):
            color = SELECTED_COLOR if i == selected_idx else OTHER_COLOR
            width = 7 if i == selected_idx else 3
            lonlat_path = [[pt[1], pt[0]] for pt in r["geometry"]]
            layers.append(pdk.Layer(
                "PathLayer",
                data=[{"path": lonlat_path, "color": color}],
                get_path="path",
                get_color="color",
                width_scale=1,
                width_min_pixels=width,
            ))
        layers.append(pdk.Layer(  # 出発
            "ScatterplotLayer",
            data=[{"lon": latlon[1], "lat": latlon[0]}],
            get_position="[lon, lat]",
            get_fill_color=[0, 180, 80],
            get_line_color=[255, 255, 255],
            line_width_min_pixels=1,
            radius_min_pixels=6,
        ))
        dst = routes[selected_idx]["poi"]
        layers.append(pdk.Layer(  # 目的地
            "ScatterplotLayer",
            data=[{"lon": dst["lon"], "lat": dst["lat"]}],
            get_position="[lon, lat]",
            get_fill_color=[230, 57, 70],
            get_line_color=[255, 255, 255],
            line_width_min_pixels=1,
            radius_min_pixels=6,
        ))

    if (lat is not None) and (lon is not None):
        view_state = pdk.ViewState(latitude=lat, longitude=lon, zoom=14)
        st.pydeck_chart(pdk.Deck(map_style=None, initial_view_state=view_state, layers=layers))


    # --- ここから：当日／翌日の時間帯テーブル ---

    if ss.get("wx"):
        wx = ss.wx
        if not wx:
            st.info("時間帯テーブルを表示できる天気データがありません。")
        else:
            def _as_dt(s: str) -> datetime:
                return datetime.fromisoformat(s.replace("T", " "))

            base_date = _as_dt(wx[0]["time"]).date()
            today = base_date
            tomorrow = base_date + timedelta(days=1)

            hourly_today = [h for h in wx if _as_dt(h["time"]).date() == today]
            hourly_tomorrow = [h for h in wx if _as_dt(h["time"]).date() == tomorrow]

            threshold = SIZE_THRESH.get(dog_size, 26.0)
            if age_years >= 8:
                threshold -= 1.0

            def _hourly_score(h):
                hour = int(h["time"].split("T")[1][:2]) if "T" in h["time"] else 0
                t_surf = h["temp"] + (4.0 if 9 <= hour <= 16 else 0.0)
                delta = threshold - t_surf
                s = 50 + int(delta * 6)
                if h["wind"] < 0.5: s -= 5
                elif h["wind"] > 6: s -= 3
                if h["rh"] > 75: s -= int((h["rh"] - 75) / 2)
                return max(0, min(100, s))

            def _build_rows(hourlies):
                wins = recommend_time_windows(hourlies, size=dog_size, age=age_years, weight=weight_kg)
                rows = []
 
                for w in wins:
                    sdt = _as_dt(w["start"]); edt = _as_dt(w["end"])
                    hrs = [h for h in hourlies if sdt <= _as_dt(h["time"]) < edt]
                    if not hrs:
                        continue
                    scores = [_hourly_score(h) for h in hrs]

                    # --- 天気（★ weathercode の多数決 → 上位1〜2語を “／” で表示）---
                    label_icon_list = [_wmo_to_label_icon(h.get("code")) for h in hrs]
                    agg = {}
                    for label, icon in label_icon_list:
                        if label in agg:
                            agg[label] = (agg[label][0] + 1, agg[label][1])
                        else:
                            agg[label] = (1, icon)
                    total = sum(v[0] for v in agg.values())
                    top = sorted(agg.items(), key=lambda kv: kv[1][0], reverse=True)
                    if len(top) >= 2 and (top[1][1][0] / total) >= 0.30:
                        (l1, (c1, i1)), (l2, (c2, i2)) = top[0], top[1]
                        weather_str = f"{i1}/{i2} {l1}／{l2}"
                    else:
                        l1, (c1, i1) = top[0]
                        weather_str = f"{i1} {l1}"

                    # --- 気温/湿度（平均値を表示）---
                    avg_temp = sum(h["temp"] for h in hrs if h.get("temp") is not None) / len(hrs)
                    avg_rh   = sum(h["rh"]   for h in hrs if h.get("rh")   is not None) / len(hrs)

                    temps = [h["temp"] for h in hrs if h.get("temp") is not None]
                    rhs   = [h["rh"]   for h in hrs if h.get("rh")   is not None]
                    avg_temp = (sum(temps) / len(temps)) if temps else None
                    avg_rh   = (sum(rhs)   / len(rhs))   if rhs   else None

                    rows.append({
                        "時間帯": f"{sdt.strftime('%H:%M')}–{edt.strftime('%H:%M')}",
                        "天気":   weather_str,
                        "気温":   f"{avg_temp:.1f}℃" if avg_temp is not None else "—",
                        "湿度":   f"{avg_rh:.0f}%"   if avg_rh   is not None else "—",
                        "スコア": int(round(sum(scores) / len(scores))),
                    })

                # スコア高い順に並べ替え
                rows = sorted(rows, key=lambda r: r["スコア"], reverse=True)
                return rows

            st.caption("スコア＝路面温度（気温＋日中補正）・風・湿度から算出した快適度（0〜100）。高いほど安全に歩けます。")
            st.markdown(f"### おすすめ時間帯（当日: {today.strftime('%Y-%m-%d')}）")
            rows_today = _build_rows(hourly_today)
            rows_today_view = [dict(r) for r in (rows_today or [])]
            if rows_today_view:
                rows_today_view[0]["時間帯"] = "◎ " + rows_today[0]["時間帯"]
                st.dataframe(rows_today_view, use_container_width=True)
            else:
                st.info("当日に安全な時間帯は見つかりませんでした。")

            st.markdown(f"### おすすめ時間帯（翌日: {tomorrow.strftime('%Y-%m-%d')}）")
            rows_tomorrow = _build_rows(hourly_tomorrow)
            rows_tomorrow_view = [dict(r) for r in (rows_tomorrow or [])]   # ★ コピー
            if rows_tomorrow_view:
                rows_tomorrow_view[0]["時間帯"] = "◎ " + rows_tomorrow[0]["時間帯"]
                st.dataframe(rows_tomorrow_view, use_container_width=True)
            else:
                st.info("翌日に安全な時間帯は見つかりませんでした。")

            params = {
                "dog_size": st.session_state.get("dog_size"),
                "dog_breed": st.session_state.get("dog_breed"),
                "age_years": st.session_state.get("age_years"),
                "weight_kg": st.session_state.get("weight_kg"),
                "weather_source": "open-meteo",
            }
            # dayタグを付けて保存用に統合
            results_log = []
            results_log += [{"day": "今日", "rank": i+1, **r} for i, r in enumerate(rows_today or [])]
            results_log += [{"day": "明日", "rank": i+1, **r} for i, r in enumerate(rows_tomorrow or [])]

            # 位置が決まっていて何かしら結果があるときだけ記録
            if ss.pop("__just_recommended", False) and (lat is not None) and (lon is not None) and results_log:
                add_reco(
                    uid,
                    origin=(lat, lon),
                    params=params,
                    results=results_log,
                    routes=ss.routes,
                    model_version="timewin_v1.2"
                )
    else:
        st.info("まずは『おすすめ開始』で天気とルートを取得してください。")




with TAB2:
    st.subheader("Googleマップでナビ → 到着でクーポン")
    plan = st.session_state.last_plan
    if not plan:
        st.info("まずはTab1でルートを作成してください。")
    else:
        o = plan["origin"]; d = plan["dest"]
        gmaps_url = f"https://www.google.com/maps/dir/?api=1&origin={o[0]},{o[1]}&destination={d[0]},{d[1]}&travelmode=walking"
        st.link_button("Googleマップを開く", gmaps_url)

        # プランがある時だけ発行ボタンを表示
        if st.button("到着判定 → クーポン発行"):
            token, img_path = issue_coupon_qr(session_id=plan["id"])
            st.image(img_path, caption=f"クーポンQR（token: {token[:8]}…）")
            st.success("発行しました。店頭で読み取ってください。")


with TAB3:
    st.subheader("実績（ダミー→徐々に本実装）")
    stats = db.get_stats()
    st.write(stats)
