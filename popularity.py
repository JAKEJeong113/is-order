# popularity.py
"""가맹점 전체 발주/장바구니 이력을 쌓아서 카테고리별 인기상품 TOP N을 계산한다."""
import os
import sqlite3
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR))
DB_PATH = DATA_DIR / "inventory.db"

CATEGORIES = ("icecream", "coupang", "wholesale")


def get_conn():
    return sqlite3.connect(DB_PATH)


def init_popularity_table():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS order_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        store_id TEXT,
        category TEXT,
        item_key TEXT,
        item_name TEXT,
        qty INTEGER,
        created_at TEXT
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_order_events_category ON order_events (category, item_key)")
    conn.commit()
    conn.close()


def log_event(store_id: str, category: str, item_key: str, item_name: str, qty: int = 1) -> None:
    if category not in CATEGORIES or not item_key:
        return

    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO order_events (store_id, category, item_key, item_name, qty, created_at)
    VALUES (?, ?, ?, ?, ?, ?)
    """, (store_id or "unknown", category, item_key, item_name, qty, now))
    conn.commit()
    conn.close()


def get_top_items(category: str, limit: int = 30, days: int = 60) -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"""
    SELECT item_key, item_name, SUM(qty) AS total_qty, COUNT(DISTINCT store_id) AS store_count
    FROM order_events
    WHERE category = ? AND created_at >= datetime('now', '-{int(days)} days')
    GROUP BY item_key
    ORDER BY total_qty DESC
    LIMIT ?
    """, (category, limit))
    rows = cur.fetchall()
    conn.close()

    return [
        {"item_key": r[0], "item_name": r[1], "total_qty": r[2], "store_count": r[3]}
        for r in rows
    ]
