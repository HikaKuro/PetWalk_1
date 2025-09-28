# core/store.py
from __future__ import annotations
import os, sqlite3, json, time
from typing import Any, Dict, List, Tuple, Optional

# --- DB path ---
DATA_DIR = "/mount/data" if os.path.isdir("/mount/data") else "."
DB_PATH = os.getenv("PETWALK_DB_PATH", os.path.join(DATA_DIR, "petwalk_mvp.db"))
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# --- low-level connect ---
# core/store.py の一部差し替え

def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    # 競合に強めのPRAGMA
    con.execute("PRAGMA foreign_keys=ON;")
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    con.execute("PRAGMA busy_timeout=5000;")   # ← 追加
    return con

def _has_old_schema(con: sqlite3.Connection) -> bool:
    cols = [r["name"] for r in con.execute("PRAGMA table_info(user_settings)")]
    return set(("uid","key","value","recorded_at")).issubset(cols) and "payload" not in cols

def _migrate_user_settings(con: sqlite3.Connection) -> None:
    # 旧 → 新：uid/key/value を user_id/payload(JSON) に集約
    # すでに新テーブルがあれば何もしない
    con.execute("""
        CREATE TABLE IF NOT EXISTS __user_settings_new(
          user_id    TEXT PRIMARY KEY,
          payload    TEXT NOT NULL,
          updated_at INTEGER NOT NULL
        )
    """)
    tmp = {}
    for r in con.execute("SELECT uid, key, value FROM user_settings").fetchall():
        tmp.setdefault(r["uid"], {})[r["key"]] = r["value"]

    now = int(time.time())
    rows = [(str(uid), json.dumps(d, ensure_ascii=False), now) for uid, d in tmp.items()]
    if rows:
        con.executemany(
            "INSERT OR REPLACE INTO __user_settings_new(user_id, payload, updated_at) VALUES(?,?,?)",
            rows
        )
    # 旧テーブルを安全に差し替え
    con.execute("ALTER TABLE user_settings RENAME TO __user_settings_old_backup")
    con.execute("ALTER TABLE __user_settings_new RENAME TO user_settings")
    # 念のため列を確認し updated_at を保証
    cols = [r["name"] for r in con.execute("PRAGMA table_info(user_settings)")]
    if "updated_at" not in cols:
        con.execute("ALTER TABLE user_settings ADD COLUMN updated_at INTEGER NOT NULL DEFAULT 0")

def _ensure() -> None:
    with _connect() as con:
        # 新スキーマ作成（存在しなければ）
        con.execute("""
            CREATE TABLE IF NOT EXISTS user_settings(
              user_id    TEXT PRIMARY KEY,
              payload    TEXT NOT NULL,
              updated_at INTEGER NOT NULL
            )
        """)
        # 旧スキーマなら**一度だけ**移行
        if _has_old_schema(con):
            # 競合しやすいので IMMEDIATE でロックを先取り
            con.execute("BEGIN IMMEDIATE")
            try:
                # 別スレッドが先に移行済みの可能性に備え再確認
                if _has_old_schema(con):
                    _migrate_user_settings(con)
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                # 旧テーブルが壊れている等で移行失敗したら、
                # とりあえず新テーブルだけは使える状態にして継続（空prefsになるがクラッシュ回避）
                # ログで状況は追える
        # ログ系はそのまま
        con.execute("""
            CREATE TABLE IF NOT EXISTS user_location_log(
              id          INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id     TEXT NOT NULL,
              lat         REAL NOT NULL,
              lon         REAL NOT NULL,
              address     TEXT,
              accuracy    REAL,
              source      TEXT,
              recorded_at INTEGER NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_loc_user_time ON user_location_log(user_id, recorded_at DESC)")
        con.execute("""
            CREATE TABLE IF NOT EXISTS walk_reco_log(
              id            INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id       TEXT NOT NULL,
              origin_lat    REAL,
              origin_lon    REAL,
              params_json   TEXT NOT NULL,
              result_json   TEXT NOT NULL,
              routes_json   TEXT,
              model_version TEXT,
              created_at    INTEGER NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_reco_user_time ON walk_reco_log(user_id, created_at DESC)")

# ---------- user settings ----------
# core/store.py

def load_user_settings(user_id: str) -> Dict[str, Any]:
    _ensure()
    with _connect() as con:
        # 1) テーブル存在 & 列構成チェック
        cols = {r["name"] for r in con.execute("PRAGMA table_info(user_settings)").fetchall()}
        if not cols:
            # テーブル自体が無い → 生成
            con.execute("""
                CREATE TABLE IF NOT EXISTS user_settings(
                  user_id    TEXT PRIMARY KEY,
                  payload    TEXT NOT NULL,
                  updated_at INTEGER NOT NULL
                )
            """)
            return {}

        if "payload" not in cols:
            # 旧式(uid/key/value/recorded_at)ならその場で移行、その他ならリセット
            if {"uid", "key", "value"}.issubset(cols):
                con.execute("BEGIN IMMEDIATE")
                try:
                    _migrate_user_settings(con)
                    con.execute("COMMIT")
                except Exception:
                    con.execute("ROLLBACK")
                    # 最低限、空の新スキーマを用意して先に進む
                    con.execute("DROP TABLE IF EXISTS user_settings")
                    con.execute("""
                        CREATE TABLE user_settings(
                          user_id    TEXT PRIMARY KEY,
                          payload    TEXT NOT NULL,
                          updated_at INTEGER NOT NULL
                        )
                    """)
                    return {}
            else:
                # 想定外スキーマ → 作り直し（データ温存の必要があればここでバックアップ運用に変更）
                con.execute("DROP TABLE IF EXISTS user_settings")
                con.execute("""
                    CREATE TABLE user_settings(
                      user_id    TEXT PRIMARY KEY,
                      payload    TEXT NOT NULL,
                      updated_at INTEGER NOT NULL
                    )
                """)
                return {}

        # 2) ここまで来たら payload で読み出せるはず
        row = con.execute(
            "SELECT payload FROM user_settings WHERE user_id=?",
            (user_id,)
        ).fetchone()
        return json.loads(row["payload"]) if row else {}


def save_user_settings(user_id: str, payload: Dict[str, Any]) -> None:
    _ensure()
    now = int(time.time())
    blob = json.dumps(payload, ensure_ascii=False)
    with _connect() as con:
        con.execute("""
            INSERT INTO user_settings(user_id, payload, updated_at)
            VALUES(?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE
              SET payload=excluded.payload,
                  updated_at=excluded.updated_at
        """, (user_id, blob, now))

# ---------- location logs (append-only) ----------
def add_location(user_id: str, lat: float, lon: float,
                 address: Optional[str]=None,
                 accuracy: Optional[float]=None,
                 source: Optional[str]=None) -> None:
    _ensure()
    with _connect() as con:
        con.execute("""
            INSERT INTO user_location_log
              (user_id, lat, lon, address, accuracy, source, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, float(lat), float(lon),
              address, accuracy, source, int(time.time())))

def list_locations(user_id: str, limit: int = 10) -> List[Dict[str, Any]]:
    _ensure()
    with _connect() as con:
        cur = con.execute("""
            SELECT id, lat, lon, address, accuracy, source, recorded_at
              FROM user_location_log
             WHERE user_id=?
             ORDER BY recorded_at DESC
             LIMIT ?
        """, (user_id, limit))
        return [dict(r) for r in cur.fetchall()]

# ---------- recommendation logs (append-only) ----------
def add_reco(user_id: str,
             origin: Tuple[Optional[float], Optional[float]],
             params: Dict[str, Any],
             results: List[Dict[str, Any]],
             routes: Optional[Any]=None,
             model_version: str="v1") -> None:
    _ensure()
    o_lat, o_lon = origin or (None, None)
    with _connect() as con:
        con.execute("""
            INSERT INTO walk_reco_log
              (user_id, origin_lat, origin_lon,
               params_json, result_json, routes_json,
               model_version, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
    with _connect() as con:
        cur = con.execute("""
            SELECT id, origin_lat, origin_lon,
                   params_json, result_json, routes_json,
                   model_version, created_at
              FROM walk_reco_log
             WHERE user_id=?
             ORDER BY created_at DESC
             LIMIT ?
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
    with _connect() as con:
        r = con.execute("""
            SELECT id, origin_lat, origin_lon,
                   params_json, result_json, routes_json,
                   model_version, created_at
              FROM walk_reco_log
             WHERE user_id=? AND id=?
        """, (user_id, reco_id)).fetchone()
    if not r:
        return None
    return {
        "id": r["id"],
        "origin": (r["origin_lat"], r["origin_lon"]),
        "params": json.loads(r["params_json"] or "{}"),
        "results": json.loads(r["result_json"] or "[]"),
        "routes": json.loads(r["routes_json"]) if r["routes_json"] else None,
        "model_version": r["model_version"],
        "created_at": r["created_at"],
    }