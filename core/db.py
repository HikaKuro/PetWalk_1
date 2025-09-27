from datetime import datetime
from sqlalchemy import create_engine, text
import json


class DB:
    def __init__(self, url: str):
        self.engine = create_engine(url, future=True)
        self._init()

    def _init(self):
        with self.engine.begin() as con:
            con.execute(text(
                """
                CREATE TABLE IF NOT EXISTS walk_plans(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    origin_lat REAL, origin_lon REAL,
                    dest_lat REAL, dest_lon REAL,
                    polyline TEXT,
                    windows_json TEXT,
                    score INTEGER,
                    created_at TEXT
                );
                """
            ))
            con.execute(text(
                """
                CREATE TABLE IF NOT EXISTS coupons(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER,
                    token TEXT,
                    issued_at TEXT,
                    redeemed_at TEXT
                );
                """
            ))


    def save_plan(self, origin_lat, origin_lon, dest_lat, dest_lon, polyline, windows, score):
        with self.engine.begin() as con:
            res = con.execute(
                text("INSERT INTO walk_plans(origin_lat,origin_lon,dest_lat,dest_lon,polyline,windows_json,score,created_at) VALUES(:ol,:on,:dl,:dn,:pl,:wj,:sc,:ca)")
                ,{
                    "ol": origin_lat, "on": origin_lon,
                    "dl": dest_lat, "dn": dest_lon,
                    "pl": polyline, "wj": json.dumps(windows, ensure_ascii=False),
                    "sc": score, "ca": datetime.utcnow().isoformat()
                }
            )
        return res.lastrowid


    def get_stats(self):
        with self.engine.begin() as con:
            cnt = con.execute(text("SELECT COUNT(*) FROM walk_plans")); total = cnt.scalar()
        return {"plans": total}