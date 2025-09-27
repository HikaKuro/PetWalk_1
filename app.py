import os
from datetime import datetime, timedelta
import streamlit as st
import pydeck as pdk
from streamlit_geolocation import streamlit_geolocation

from core.weather import get_hourly_weather
from core.geocode import geocode_address
from core.osm import get_pois
from core.routing import route_walking
from core.recommend import recommend_time_windows, score_route
from core.coupon import issue_coupon_qr
from core.db import DB
from math import isfinite

st.set_page_config(page_title="PetWalk+ MVP", layout="wide")

# --- Sidebar: 犬プロフィール & 位置入力 ---
with st.sidebar:
    st.header("プロフィール")
    dog_size = st.selectbox("犬種サイズ", ["小型", "中型", "大型"])
    breed = st.text_input("犬種（任意）")
    age_years = st.number_input("年齢(年)", 0.0, 30.0, 5.0, 0.5)
    weight_kg = st.number_input("体重(kg)", 0.0, 100.0, 8.0, 0.5)
    # st.divider()
    # st.caption("位置情報が使えない場合は住所入力でフォールバック")
    # address_txt = st.text_input("住所（任意）")

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
                from streamlit_geolocation import streamlit_geolocation
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

    # # 現在の基準位置のミニ表示
    # if ss.latlon:
    #     st.caption(f"現在の基準位置：{ss.latlon[0]:.5f}, {ss.latlon[1]:.5f}")
    # else:
    #     st.warning("現在地許諾 or 住所を入力してください。")
    # # 取得済みなら小さな確認表示（None保護も）
    # if st.session_state.get("latlon"):
    #     lat, lon = st.session_state.latlon
    #     st.caption(f"現在の基準位置：{lat:.5f}, {lon:.5f}")

    colA, colB = st.columns([2, 1])
    with colA:
        go = st.button("おすすめ開始", type="primary", use_container_width=True)
    with colB:
        radius = st.slider("探索半径(m)", 300, 2000, 800, 100)

    # --- 計算（ボタン押下時のみ再計算） ---
    if go and (lat is not None) and (lon is not None):
        # 1) 天気
        wx = get_hourly_weather(lat, lon, hours=24)

        # 2) 時間帯レコメンド
        windows = recommend_time_windows(wx, size=dog_size, age=age_years, weight=weight_kg)

        # 2.1) ウィンドウの「時間帯・天気・スコア」行を生成（表示は後段で行う）
        from core.recommend import SIZE_THRESH
        threshold = SIZE_THRESH.get(dog_size, 26.0)
        if age_years >= 8:
            threshold -= 1.0

        def hourly_score(h):
            hour = int(h["time"].split("T")[1][:2]) if "T" in h["time"] else 0
            t_surf = h["temp"] + (4.0 if 9 <= hour <= 16 else 0.0)
            s = 50 + int((threshold - t_surf) * 6)
            if h["wind"] < 0.5: s -= 5
            elif h["wind"] > 6: s -= 3
            if h["rh"] > 75: s -= int((h["rh"] - 75) / 2)
            return max(0, min(100, s))

        def as_dt(s: str):
            return datetime.fromisoformat(s.replace("T", " "))

        tw_rows = []
        for w in windows:
            sdt, edt = as_dt(w["start"]), as_dt(w["end"])
            hrs = [h for h in wx if sdt <= as_dt(h["time"]) < edt]
            if not hrs:
                continue
            scores = [hourly_score(h) for h in hrs]
            temps  = [h["temp"] for h in hrs]
            rhs    = [h["rh"]   for h in hrs]
            winds  = [h["wind"] for h in hrs]
            tw_rows.append({
                "時間帯": f"{sdt.strftime('%H:%M')}–{edt.strftime('%H:%M')}",
                "天気":  f"{min(temps):.0f}–{max(temps):.0f}℃ / 平均湿度{(sum(rhs)/len(rhs)):.0f}% / 風{min(winds):.1f}–{max(winds):.1f}m/s",
                "スコア": int(round(sum(scores) / len(scores)))
            })
        tw_rows = sorted(tw_rows, key=lambda r: r["スコア"], reverse=True)

        # 3) 目的地候補 → 4) ルート生成 & スコア
        pois = get_pois(lat, lon, radius_m=radius)
        routes = []
        for poi in pois[:3]:
            r = route_walking((lat, lon), (poi["lat"], poi["lon"]))
            if r:
                r["poi"] = poi
                r["score"] = score_route(r, pois)
                routes.append(r)
        routes = sorted(routes, key=lambda x: x.get("score", 0), reverse=True)

        # 計算結果をセッションに保存（←ここが肝）
        ss.latlon = (lat, lon)
        ss.windows = windows
        ss.tw_rows = tw_rows
        ss.routes = routes
        ss.selected_route_idx = 0

    # --- ここからは「セッションの値」を常に使って描画（タブ切替の再実行でも消えない） ---
    routes = ss.routes
    selected_idx = ss.selected_route_idx
    latlon = ss.latlon

    # 時間帯レコメンド表
    if ss.tw_rows:
        st.markdown("**時間帯レコメンド**")
        st.dataframe(ss.tw_rows, use_container_width=True)

    # 5) ルート切り替えUI（セッションに選択状態を保持）
    if routes:
        labels = [f"候補{i+1}: {r['poi']['name']} / {r.get('distance_m',0)/1000:.1f}km / スコア{r['score']}"
                  for i, r in enumerate(routes)]
        sel = st.selectbox(
            "強調表示するルート",
            labels,
            index=min(selected_idx, len(labels)-1),
            key="route_select"
        )
        selected_idx = labels.index(sel)
        ss.selected_route_idx = selected_idx

        # 候補タブ（切替で再実行してもセッションから再描画）
        tabs = st.tabs([f"候補{i+1}" for i in range(len(routes))])
        for i, t in enumerate(tabs):
            with t:
                r = routes[i]
                st.markdown(f"**{r['poi']['name']}** 距離: {r.get('distance_m','?')} m / スコア: {r['score']}")
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

    # 6) 地図表示：セッションの routes/selected_idx/latlon を使用
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
        # 出発・目的地マーカー
        layers.append(pdk.Layer(
            "ScatterplotLayer",
            data=[{"lon": latlon[1], "lat": latlon[0]}],
            get_position="[lon, lat]",
            get_fill_color=[0, 180, 80],
            get_line_color=[255, 255, 255],
            line_width_min_pixels=1,
            radius_min_pixels=6,
        ))
        dst = routes[selected_idx]["poi"] if routes else None
        if dst:
            layers.append(pdk.Layer(
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
