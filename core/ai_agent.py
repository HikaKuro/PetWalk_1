# ai_agent.py
from __future__ import annotations
import json
from typing import List, Optional, Tuple
from pydantic import BaseModel, Field

# 既存モジュールの import（core 配下/直下の両対応）
try:
    from core.weather import get_hourly_weather
    from core.geocode import geocode_address
    from core.osm import get_pois
    from core.routing import route_walking
    from core.recommend import SIZE_THRESH
except Exception:
    from weather import get_hourly_weather
    from geocode import geocode_address
    from osm import get_pois
    from routing import route_walking
    from recommend import SIZE_THRESH

# ---- LangChain
from langchain.tools import StructuredTool
from langchain_openai import ChatOpenAI
from langchain.agents import initialize_agent, AgentType
from langchain.agents import create_openai_tools_agent, AgentExecutor
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import SystemMessage, HumanMessage
import requests
OVERPASS = "https://overpass-api.de/api/interpreter"



# =========================
# Pydantic Schemas
# =========================
# ファイル先頭の import 群の下あたりに追加
def _to_float(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

def _to_int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default
# 既存の _to_float/_to_int のすぐ下に追加
def _geq(a, b) -> bool:
    """a >= b を安全に。どちらかが数値化できなければ False を返す。"""
    af = _to_float(a)
    bf = _to_float(b)
    return (af is not None) and (bf is not None) and (af >= bf)

def _get_llm(model="gpt-4o-mini", temperature=0.2):
    # ここで JSON モードを固定
    return ChatOpenAI(
        model=model,
        temperature=temperature,
        timeout=60,
        model_kwargs={"response_format": {"type": "json_object"}}
    )

def _invoke_json(llm, system_prompt: str, payload: dict) -> dict:
    from langchain_core.messages import SystemMessage, HumanMessage
    msgs = [SystemMessage(content=system_prompt),
            HumanMessage(content=f"以下を解析してJSONで返してください。\n\n```json\n{json.dumps(payload, ensure_ascii=False)}\n```")]
    out = llm.invoke(msgs).content
    # JSONだけを取り出す（```json ... ```に包まれてもOK）
    import re, json as pyjson
    m = re.search(r"\{.*\}", out, re.S)
    return pyjson.loads(m.group(0)) if m else {}

TIME_WINDOW_SELECTOR_SYSTEM = """
    あなたは獣医監修の『犬の散歩時間』プランナーです。
    与えられた犬プロフィールと48時間の天気(日本時間)から、散歩に適した上位時間帯を 2〜4 件、重要度順に提案してください。

    出力要件（厳守）：
    - JSONオブジェクトのみを返す（説明文は出さない）。
    - フィールド: {"windows":[{"start":"YYYY-MM-DD HH:00","end":"YYYY-MM-DD HH:00","label":"短い説明","score":0-100,"reason_ja":"120字以内"}, ...]}
    - scoreは0〜100の整数。90-100=非常に安全/快適, 70-89=安全寄り, 50-69=可, 30-49=非推奨, 0-29=危険。
    - 日中は路面温度が気温より高い前提を考慮（おおむね+3〜6℃）。小型・高齢は暑熱に弱い。
    - 風通し(風速)と湿度も必ず反映。朝夕の優位性、夜間の明暗も考慮。
    - 候補は重複・包含しないように（同じ時間帯や大きく重なる時間帯は避ける）。
    """

ROUTE_SELECTOR_SYSTEM = """
    あなたは『犬の散歩目的地選定』のプランナーです。
    与えられた犬プロフィール、当日の暑さヒント、出発地点周辺のPOI一覧（最大60件）から、
    散歩に適した目的地を上位K件（K=3推奨）選び、短い理由を付けてください。

    出力要件（厳守）：
    - JSONのみ。{"selections":[{"poi_index":<int>,"label":"短い説明","reason_ja":"120字以内"}, ...]}
    - 暑い/蒸す日は日陰・芝・水辺・緑地・遊歩道を優先。幹線道路沿いは減点。
    - 小型・高齢は長距離を避ける。超近距離（片道200m未満）は運動不足も留意。
    - OSMタグ dogs=yes / pets=yes / dogs=leashed / pets=permissive 等の『ペット同伴可』は高評価。テラス席可も加点。
    - poi_index は入力配列のインデックス（順序を変えずに指す）。
    """

ROUTE_SCORER_SYSTEM = """
    あなたは『犬の散歩ルート評価者』です。
    選定済みの目的地ごとに、実測の徒歩距離・往復時間・環境ヒントを踏まえて
    0〜100の整数スコアと短い理由(日本語120字以内)を返してください。

    出力要件（厳守）：
    - JSONのみ。{"scores":[{"score":0-100,"reason_ja":"..."}, ...] } （入力順を保持）
    - 安全第一：公園/遊歩道/水辺/緑地 > 車道沿い。暑熱時は日陰・芝・水辺を加点、長距離は減点。
    - 『ペット同伴可（dogs=yes / pets=yes など）』の店舗は条件が合えば優先的に高得点。
    - 犬サイズ・年齢で許容距離を調整（小型・高齢は短め）。
    """

class RecommendInput(BaseModel):
    lat: float = Field(..., description="Origin latitude in WGS84")
    lon: float = Field(..., description="Origin longitude in WGS84")
    radius_m: int = Field(800, ge=100, le=3000, description="Search radius around origin in meters")
    dog_size: str = Field(..., description="犬のサイズ: 小型/中型/大型")
    age_years: float = Field(..., description="年齢（歳）")
    weight_kg: float = Field(..., description="体重（kg）")
    max_routes: int = Field(3, ge=1, le=5, description="返すルート候補の最大件数")

class GeocodeInput(BaseModel):
    address: str

class RecommendResult(BaseModel):
    # 表で使う時間帯サマリ
    time_windows: List[dict] = Field(
        ...,
        description="e.g. [{'時間帯':'18:00–20:00','天気':'☀️ 晴れ','気温':'22.4℃','湿度':'60%','スコア':88}, ...]（スコア降順）]"
    )
    # ルート候補（UI でタブ切り替え）
    routes: List[dict] = Field(
        ...,
        description="各要素: {'poi':{'name','lat','lon','kind'}, 'geometry':[(lat,lon),...], 'distance_m':int, 'score':int, 'polyline':str?}"
    )
    # LLM による簡潔な説明（UI の見出しに）
    summary: str


# =========================
# LangChain Tools
# =========================

def _tool_geocode(address: str) -> str:
    """住所→(lat,lon)"""
    g = geocode_address(address)
    return json.dumps(g or {} , ensure_ascii=False)

geocode_tool = StructuredTool.from_function(
    name="geocode_address",
    description="住所やランドマーク名から緯度経度を取得する。正確な位置が必要な時に使う。",
    func=_tool_geocode,
    args_schema=GeocodeInput,
    return_direct=False,
)

def _gpt_select_timewindows(llm, dog_profile: dict, hourly: list, top_k: int = 3) -> list:
    """
    hourly: [{"time":"YYYY-MM-DD HH:00","temp": float,"rh": float,"wind": float,"is_day": bool}, ...] (JST 48h)
    return: [{"start": "...", "end":"...", "label":"...", "score":int, "reason_ja":"..."}]
    """
    payload = {
        "dog": dog_profile,          # {"size":"小型|中型|大型","age":float,"weight":float}
        "hourly": hourly[:48],       # 送信は48本まで
        "request": {"top_k": top_k}
    }
    res = _invoke_json(llm, TIME_WINDOW_SELECTOR_SYSTEM, payload) or {}
    time_windows = res.get("windows") or []
    # バリデーション＆丸め
    out = []
    for w in time_windows[:top_k]:
        s = int(max(0, min(100, int(w.get("score", 0)))))
        st = str(w.get("start",""))[:16]; en = str(w.get("end",""))[:16]
        if len(st) == 16 and len(en) == 16 and st < en:
            out.append({"start": st, "end": en, "label": w.get("label") or "おすすめ時間帯",
                        "score": s, "reason": w.get("reason_ja") or ""})
    return out

def _gpt_select_routes(llm, dog_profile: dict, poi_items: list, hot_hint: bool, top_k: int = 3) -> list:
    """
    poi_items: [{"name":str,"kind":"park|footway|path|other","lat":float,"lon":float,"approx_m":int,"env":["shade?","grass?","water?","traffic?"]}, ...]
    return: [{"poi_index": int, "label": str, "reason_ja": str}]
    """
    payload = {
        "dog": dog_profile,
        "hot_day": bool(hot_hint),
        "pois": poi_items[:60],      # LLMに渡す最大件数を制限
        "request": {"top_k": top_k}
    }
    res = _invoke_json(llm, ROUTE_SELECTOR_SYSTEM, payload) or {}
    sels = res.get("selections") or []
    picks = []
    for s in sels[:top_k]:
        idx = s.get("poi_index")
        if isinstance(idx, int) and 0 <= idx < len(poi_items):
            picks.append({"poi_index": idx, "label": s.get("label") or "候補", "reason_sel": s.get("reason_ja") or ""})
    return picks

def _gpt_score_routes(llm, dog_profile: dict, routes_for_eval: list) -> list:
    """
    routes_for_eval: [{"distance_m":int,"est_minutes_oneway":int,"poi_kind":str,"environment":[...]}] in the chosen order
    return: [{"score":int,"reason_ja":str}, ...]
    """
    payload = {"dog": dog_profile, "routes": routes_for_eval}
    res = _invoke_json(llm, ROUTE_SCORER_SYSTEM, payload) or {}
    arr = res.get("scores") or []
    out = []
    for a in arr[:len(routes_for_eval)]:
        out.append({
            "score": int(max(0, min(100, int(a.get("score", 0))))),
            "reason": a.get("reason_ja") or ""
        })
    return out

def _tool_recommend(payload: dict) -> RecommendResult:
    try:
        """天気→安全時間帯→POI→経路→採点 までワンショットで実施。"""
        lat = _to_float(payload.get("lat"))
        lon = _to_float(payload.get("lon"))
        radius_m = _to_int(payload.get("radius_m"), 800) or 800
        dog_size = str(payload.get("dog_size") or "").strip() or "中型"
        age = _to_float(payload.get("age_years"), 5.0) or 5.0
        weight = _to_float(payload.get("weight_kg"), 10.0) or 10.0
        k = _to_int(payload.get("max_routes"), 3) or 3
        k = max(1, min(5, k))  # 1..5 にクリップ

        llm = _get_llm()  # ← 先に用意（後段でも使う）


        # 1) 天気（48h）
        hourly_raw = get_hourly_weather(lat, lon) or [] # 1h刻み、temp/rh/wind/is_day付きの配列を想定
        hourly = []
        for h in hourly_raw[:48]:
            hourly.append({
                "time": (h.get("time_jst") or h.get("time") or "")[:16],  # "YYYY-MM-DD HH:00"
                "temp": _to_float(h.get("temp")),
                "rh": _to_float(h.get("rh")),
                "wind": _to_float(h.get("wind")),
                "is_day": bool(h.get("is_day", False)),
                "code": _to_int(h.get("code")),
            })

        # --- LLMで上位時間帯を直接生成（スコア&理由込み） ---
        llm = _get_llm()  # 既存
        dog = {"size": dog_size, "age": age, "weight": weight}
        time_windows = _gpt_select_timewindows(llm, dog, hourly, top_k=3)

        # --- フェイルセーフ：LLMが空なら簡易固定（朝/夕） ---
        if not time_windows:
            from datetime import datetime, timezone, timedelta
            JST = timezone(timedelta(hours=9))
            today = datetime.now(JST).strftime("%Y-%m-%d")
            time_windows = [
                {"start": f"{today} 06:00", "end": f"{today} 08:00", "label": "朝の涼しい時間", "score": 75, "reason": "涼しく風も出やすい"},
                {"start": f"{today} 18:00", "end": f"{today} 20:00", "label": "夕方の日射弱め", "score": 70, "reason": "日射が弱まり路面温度が低下"},
            ]

        # 時間帯の表示テーブル（LLMスコア＆理由をそのまま利用）
        
        senior = _geq(age, 8)   # age が str でも安全
        threshold = SIZE_THRESH.get(dog_size, 26.0) - (1.0 if senior else 0.0)
    
        def _as_dt(s): 
            from datetime import datetime
            return datetime.fromisoformat(s.replace("T"," "))

        rows: list[dict] = []

        for w in time_windows:
            sdt = _as_dt(w["start"]); edt = _as_dt(w["end"])
            hrs = [h for h in hourly if sdt <= _as_dt(h["time"]) < edt]
            if not hrs:
                continue

            temps = [h["temp"] for h in hrs if h["temp"] is not None]
            rhs   = [h["rh"]   for h in hrs if h["rh"]   is not None]
            winds = [h["wind"] for h in hrs if h["wind"] is not None]
            avg_temp = (sum(temps)/len(temps)) if temps else None
            avg_rh   = (sum(rhs)/len(rhs))     if rhs   else None
            avg_wind = (sum(winds)/len(winds)) if winds else None

            # LLM生成のスコア＆理由をそのまま表示へ
            delta_air = (threshold - avg_temp) if (avg_temp is not None) else None
            temp_note = (f"気温{avg_temp:.1f}℃" + (f"（しきい値より{delta_air:.1f}℃低め）" if delta_air is not None else "")) if avg_temp is not None else "気温データ不足"
            rh_note   = f"湿度{avg_rh:.0f}%" if avg_rh is not None else "湿度—"
            wind_note = f"風{avg_wind:.1f}m/s" if avg_wind is not None else "風—"
            reason_tw = w.get("reason") or f"{temp_note} / {rh_note} / {wind_note}"
            
            rows.append({
                "時間帯": f"{sdt:%H:%M}–{edt:%H:%M}",
                "天気": "—",
                "気温": f"{avg_temp:.1f}℃" if avg_temp is not None else "—",
                "湿度": f"{avg_rh:.0f}%"   if avg_rh   is not None else "—",
                "スコア": int(w.get("score", 0)),
                "理由": reason_tw,
            })
        rows.sort(key=lambda r: r["スコア"], reverse=True)

        # ベスト時間帯（LLMスコア最大）を採用して気象条件を代表値化
        best_row = rows[0] if rows else None
        best_avg_temp = _to_float(best_row.get("気温","").replace("℃","")) if best_row else None
        best_avg_rh   = _to_float(best_row.get("湿度","").replace("%","")) if best_row else None
        # 速度（目安）：小型3.5km/h, 中型4.0, 大型4.5
        spd_kmh = 3.5 if dog_size == "小型" else (4.0 if dog_size == "中型" else 4.5)
        # 片道の基準分数：小型12/中型18/大型22。高齢(>=8歳)は20%短縮
        base_oneway_min = 12 if dog_size=="小型" else (18 if dog_size=="中型" else 22)
        if _geq(age, 8): base_oneway_min = int(round(base_oneway_min * 0.8))
        # 暑さ補正：しきい値より高いほど短縮、低いほど微増（最大 +25%）
        senior = _geq(age, 8)
        threshold = SIZE_THRESH.get(dog_size, 26.0) - (1.0 if senior else 0.0)
        if (best_avg_temp is not None):
             delta = threshold - best_avg_temp
             if delta < 0:      # 暑い：最大 -50%
                 factor = max(0.5, 1.0 + (delta/6.0))  # 6℃上回ると 1-1=0 → 50%に下限
             else:               # 涼しい：最大 +25%
                 factor = min(1.25, 1.0 + min(delta,5)/20.0)
        else:
            factor = 1.0
        oneway_min = max(8, int(round(base_oneway_min * factor)))
        total_min  = max(15, min(60, oneway_min * 2))
        # 提案半径：片道距離×1.1（目的地の少し外側まで）。300〜2000mにクリップ
        oneway_m = int(round(spd_kmh * 1000 * (oneway_min/60.0)))
        radius_suggest_m = max(300, min(2000, int(round(oneway_m * 1.1))))
        # ユーザー指定より“提案”を優先（プロダクト方針：自動でちょうど良く探す）
        radius_m_eff = radius_suggest_m


        # --- 生のPOI候補を広めに取得（上限を持たせる） ---
        # --- 生のPOI候補を広めに取得（上限を持たせる） ---
        pois_raw = get_pois(lat, lon, radius_m=radius_m_eff)  # park/footway/path/other
        # 追加：『ペット同伴可』の飲食/物販（OSM: dogs/pets タグ）
        def _fetch_pet_friendly(lat, lon, radius):
            q = f"""
            [out:json][timeout:25];
            (
              node(around:{radius},{lat},{lon})["amenity"~"cafe|restaurant|pub|bar|fast_food"]["dogs"~"yes|permissive|leashed"];
              way(around:{radius},{lat},{lon})["amenity"~"cafe|restaurant|pub|bar|fast_food"]["dogs"~"yes|permissive|leashed"];
              node(around:{radius},{lat},{lon})["amenity"~"cafe|restaurant|pub|bar|fast_food"]["pets"~"yes|permissive"];
              way(around:{radius},{lat},{lon})["amenity"~"cafe|restaurant|pub|bar|fast_food"]["pets"~"yes|permissive"];
              node(around:{radius},{lat},{lon})["shop"]["dogs"~"yes|permissive|leashed"];
              way(around:{radius},{lat},{lon})["shop"]["dogs"~"yes|permissive|leashed"];
            );
            out center tags 100;
            """
            try:
                r = requests.post(OVERPASS, data={"data": q}, timeout=12)
                r.raise_for_status()
                js = r.json()
                out = []
                for el in js.get("elements", []):
                    tags = el.get("tags", {}) or {}
                    name = tags.get("name") or "ペット同伴可スポット"
                    kind = tags.get("amenity") or tags.get("shop") or "pet_friendly"
                    if "center" in el:
                        latc, lonc = el["center"]["lat"], el["center"]["lon"]
                    else:
                        latc, lonc = el.get("lat"), el.get("lon")
                    if latc is None or lonc is None: 
                        continue
                    out.append({
                        "name": name, "kind": f"pet_{kind}",
                        "lat": latc, "lon": lonc,
                        "tags": tags, "pet_friendly": True
                    })
                return out
            except Exception:
                return []

        extra_pet = _fetch_pet_friendly(lat, lon, radius_m_eff)

        # park/footway/path + pet_friendly を結合（重複は座標&名前で雑に除去）
        merged = (pois_raw or []) + (extra_pet or [])
        seen = set()
        pois = []
        for p in merged:
            if len(pois) >= 80: break  # トークン対策
            name = p.get("name") or "目的地"
            latp = p.get("lat"); lonp = p.get("lon")
            sig = (name, round(latp or 0, 5), round(lonp or 0, 5))
            if sig in seen or (latp is None or lonp is None):
                continue
            seen.add(sig)        
            kind = p.get("kind") or p.get("type") or "other"
            approx = int(p.get("approx_m") or p.get("distance_m") or 0)  # なければ後述の簡易距離で補う
            env = []
            if kind in ("park","leisure_park"): env += ["grass","shade"]
            if "water" in (p.get("tags") or {}): env += ["water"]
            pet_ok = bool(p.get("pet_friendly")) or any(str((p.get("tags") or {}).get(k,"")).lower() in ("yes","permissive","leashed")
                        for k in ("dogs","pets","dog","pet"))
            if pet_ok: env.append("pet_friendly")
            pois.append({
                "name": name,
                "kind": ("park" if "park" in kind else
                         ("footway" if ("foot" in kind or "path" in kind) else
                         ("pet_friendly" if pet_ok else "other"))),
                "lat": float(latp), "lon": float(lonp),
                "approx_m": approx,
                "env": list(sorted(set(env))) or ["unknown"],
                "pet_friendly": pet_ok,
            })

        # --- 暑熱ヒント（時間帯スコアの最大が低いなど） ---
        hot_day = max([w["score"] for w in time_windows]) < 70 if time_windows else False

        # --- LLMが上位K件の目的地を選ぶ（理由つき） ---
        picks = _gpt_select_routes(llm, dog, pois, hot_hint=hot_day, top_k=k)

        # ルート線を並列取得
        from concurrent.futures import ThreadPoolExecutor, as_completed
        chosen_routes = []
        def _route_job(poi):
            return poi, route_walking(origin=(lat, lon), dest=(poi["lat"], poi["lon"]))
        with ThreadPoolExecutor(max_workers=min(4, len(picks))) as ex:
            futures = [ex.submit(_route_job, pois[sel["poi_index"]]) for sel in picks]
            for f in as_completed(futures):
                p, rt = f.result()
                if not rt: 
                    continue
                dist_m = int(rt.get("distance_m") or 0)
                one_way_min = max(1, int(round(dist_m / 1000 * 60 / 4.0)))
                chosen_routes.append({
                    "poi": p,
                    "geometry": rt.get("geometry"),
                    "polyline": rt.get("polyline"),
                    "distance_m": dist_m,
                    "est_minutes_oneway": one_way_min,
                    "poi_kind": p["kind"],
                    "environment": p["env"],
                    "sel_reason": next((s["reason_sel"] for s in picks if pois[s["poi_index"]] is p), ""),
                    "label": next((s["label"] for s in picks if pois[s["poi_index"]] is p), p["name"]),
                })

        # --- LLMで最終スコア＆理由（距離・環境込みで再評価） ---
        routes_for_eval = [{
            "distance_m": r["distance_m"],
            "est_minutes_oneway": r["est_minutes_oneway"],
            "poi_kind": r["poi_kind"],
            "environment": (r["environment"] + (["pet_friendly"] if r["poi"].get("pet_friendly") else [])),
        } for r in chosen_routes]

        scores = _gpt_score_routes(llm, dog, routes_for_eval)

        # --- マージ＆ソート（LLMスコアのみで並び替え） ---
        final_routes = []
        for r, s in zip(chosen_routes, scores):
            final_routes.append({
                **r,
                "score": s["score"],
                "reason": f"{r['sel_reason']} / {s['reason']}".strip(" / "),
            })
        final_routes.sort(key=lambda x: (-x["score"], x["distance_m"]))

        routes = final_routes[:k]

        summary = (
            f"48時間の天気からGPTが候補時間帯を生成・採点。"
            f"周辺POIは広めに収集し、GPTが目的地を選定→経路距離確定→GPTが最終採点。"
        )
        return RecommendResult(time_windows=rows, routes=routes, summary=summary)

    except Exception as e:
        raise RuntimeError(f"tool内部エラー: {type(e).__name__}: {e}")


def _tool_recommend_entry(
    lat: float, lon: float, radius_m: int, dog_size: str, age_years: float, weight_kg: float, max_routes: int
) -> str:
    out = _tool_recommend({
        "lat": lat, "lon": lon, "radius_m": radius_m, 
        "dog_size": dog_size, "age_years": age_years, "weight_kg": weight_kg,
        "max_routes": max_routes
    })
    return json.dumps(out.model_dump(), ensure_ascii=False)

recommend_tool = StructuredTool.from_function(
    name="recommend_walk_plan",
    description=(
        "散歩の安全な時間帯と、出発地からの徒歩ルート候補をまとめて返す。"
        "距離・公園/遊歩道の有無を考慮して採点し、スコア降順 topK を返す。"
    ),
    func=_tool_recommend_entry,
    args_schema=RecommendInput,
    return_direct=False,
)


# =========================
# Agent Factory & Runner
# =========================

SYSTEM = """あなたは犬の安全な散歩計画アシスタントです。
- 夏季の路面温度上昇（昼間 +4℃）を考慮した既存の関数が用意されています。
- 体格（小/中/大型）・年齢で温度しきい値が変わります（高齢犬は厳しめ）。
- 最終出力は JSON のみで返してください。text の冗長な説明は summary に短くまとめてください。
"""

USER_FMT = """入力:
- 出発地: ({lat}, {lon}) 半径: {radius_m} m
- 犬: サイズ={dog_size}, 年齢={age_years}, 体重={weight_kg}
- ルート候補は最大 {max_routes} 件

手順:
1) ツール recommend_walk_plan を1回だけ呼んで JSON を取得。
2) 返却する JSON はそのまま出力（加工しない）。
"""

def make_agent(model: str = "gpt-4o-mini", temperature: float = 0.0):
    base_llm = ChatOpenAI(model=model, temperature=temperature, timeout=60)

    # ★ ここで LLM に tools を“直バインド”＋ tool_choice を指定
    llm = base_llm.bind_tools(
        [geocode_tool, recommend_tool],
        tool_choice={"type": "function", "function": {"name": "recommend_walk_plan"}}
    )

    # 最小のプロンプト（system は既存の SYSTEM を使う）
    prompt = ChatPromptTemplate.from_messages([
        ("system", SYSTEM),
        ("human", "{input}"),
        MessagesPlaceholder("agent_scratchpad"),
    ])

    agent_graph = create_openai_tools_agent(llm, [geocode_tool, recommend_tool], prompt)

    executor = AgentExecutor(
        agent=agent_graph,
        tools=[geocode_tool, recommend_tool],
        verbose=False,
        handle_parsing_errors=True,
        return_intermediate_steps=True,
        max_iterations=2,
    )
    return executor

def run_recommend(
    lat: float, lon: float, dog_size: str, age_years: float, weight_kg: float,
    radius_m: int = 800, max_routes: int = 3, model: str = "gpt-4o-mini"
) -> RecommendResult:
    # 直接ツールを呼ぶ（LangChain Agent を経由しない）
    out = _tool_recommend({
        "lat": lat, "lon": lon, "radius_m": radius_m,
        "dog_size": dog_size, "age_years": age_years, "weight_kg": weight_kg,
        "max_routes": max_routes
    })
    return out
