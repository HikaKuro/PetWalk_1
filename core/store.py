# core/store.py
from __future__ import annotations
import os, sqlite3, json, time
from typing import Any, Dict, List, Tuple, Optional

# 置換案（安全版）
DATA_DIR = "/mount/data" if os.path.isdir("/mount/data") else "."
DB_PATH = os.getenv("PETWALK_DB_PATH", os.path.join(DATA_DIR, "petwalk_mvp.db"))
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON;")
    con.execute("PRAGMA journal_mode=WAL;")
    return con

def _ensure():
    with _conn() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS user_settings(
          user_id   TEXT PRIMARY KEY,
          payload   TEXT NOT NULL,
          updated_at INTEGER NOT NULL
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS user_location_log(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id   TEXT NOT NULL,
          lat       REAL NOT NULL,
          lon       REAL NOT NULL,
          address   TEXT,
          accuracy  REAL,
          source    TEXT,
          recorded_at INTEGER NOT NULL
        )""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_loc_user_time ON user_location_log(user_id, recorded_at DESC)")
        con.execute("""
        CREATE TABLE IF NOT EXISTS walk_reco_log(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id     TEXT NOT NULL,
          origin_lat  REAL,
          origin_lon  REAL,
          params_json TEXT NOT NULL,
          result_json TEXT NOT NULL,
          routes_json TEXT,
          model_version TEXT,
          created_at  INTEGER NOT NULL
        )""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_reco_user_time ON walk_reco_log(user_id, created_at DESC)")

# ---------- user settings ----------
def load_user_settings(user_id: str) -> Dict[str, Any]:
    _ensure()
    with _conn() as con:
        row = con.execute("SELECT payload FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
    return json.loads(row["payload"]) if row else {}

def save_user_settings(user_id: str, payload: Dict[str, Any]) -> None:
    _ensure()
    now = int(time.time())
    blob = json.dumps(payload, ensure_ascii=False)
    with _conn() as con:
        con.execute("""
            INSERT INTO user_settings(user_id, payload, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at
        """, (user_id, blob, now))

# ---------- location logs (append-only) ----------
def add_location(user_id: str, lat: float, lon: float,
                 address: Optional[str]=None, accuracy: Optional[float]=None, source: Optional[str]=None) -> None:
    _ensure()
    with _conn() as con:
        con.execute("""
            INSERT INTO user_location_log(user_id, lat, lon, address, accuracy, source, recorded_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
        """, (user_id, float(lat), float(lon), address, accuracy, source, int(time.time())))

def list_locations(user_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    _ensure()
    with _conn() as con:
        cur = con.execute("""
            SELECT id, lat, lon, address, accuracy, source, recorded_at
            FROM user_location_log
            WHERE user_id=? ORDER BY recorded_at DESC LIMIT ?
        """, (user_id, limit))
        return [dict(r) for r in cur.fetchall()]

# ---------- recommendation logs (append-only) ----------
def add_reco(user_id: str, origin: Tuple[Optional[float], Optional[float]],
             params: Dict[str, Any], results: List[Dict[str, Any]],
             routes: Optional[Any]=None, model_version: str="v1") -> None:
    _ensure()
    o_lat, o_lon = origin or (None, None)
    with _conn() as con:
        con.execute("""
            INSERT INTO walk_reco_log(user_id, origin_lat, origin_lon, params_json, result_json, routes_json, model_version, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            float(o_lat) if o_lat is not None else None,
            float(o_lon) if o_lon is not None else None,
            json.dumps(params, ensure_ascii=False),
            json.dumps(results, ensure_ascii=False),
            json.dumps(routes, ensure_ascii=False) if routes is not None else None,
            model_version,
            int(time.time())
        ))

def list_recos(user_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    _ensure()
    with _conn() as con:
        cur = con.execute("""
            SELECT id, origin_lat, origin_lon, params_json, result_json, routes_json, model_version, created_at
            FROM walk_reco_log
            WHERE user_id=? ORDER BY created_at DESC LIMIT ?
        """, (user_id, limit))
        rows = []
        for r in cur.fetchall():
            rows.append({
                "id": r["id"],
                "origin": (r["origin_lat"], r["origin_lon"]),
                "params": json.loads(r["params_json"] or "{}"),
                "results": json.loads(r["result_json"] or "[]"),
                "routes": json.loads(r["routes_json"]) if r["routes_json"] else None,
                "model_version": r["model_version"],
                "created_at": r["created_at"],
            })
        return rows

def get_reco(user_id: str, reco_id: int) -> Optional[Dict[str, Any]]:
    _ensure()
    with _conn() as con:
        r = con.execute("""
            SELECT id, origin_lat, origin_lon, params_json, result_json, routes_json, model_version, created_at
            FROM walk_reco_log
            WHERE user_id=? AND id=?
        """, (user_id, reco_id)).fetchone()
    if not r: return None
    return {
        "id": r["id"],
        "origin": (r["origin_lat"], r["origin_lon"]),
        "params": json.loads(r["params_json"] or "{}"),
        "results": json.loads(r["result_json"] or "[]"),
        "routes": json.loads(r["routes_json"]) if r["routes_json"] else None,
        "model_version": r["model_version"],
        "created_at": r["created_at"],
    }
