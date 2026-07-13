# popularity.py
"""가맹점 전체 발주/장바구니 이력을 쌓아서 카테고리별 인기상품 TOP N을 계산한다."""
from datetime import datetime, timedelta

import db_conn

CATEGORIES = ("icecream", "coupang", "wholesale")


def get_conn():
    return db_conn.get_conn()


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
    # SQLite 전용 datetime('now', '-N days') 대신 파이썬에서 기준 시각을 계산해
    # 넘긴다 - created_at이 ISO 8601 문자열(예: "2026-07-13T13:28:25")이라
    # 문자열 비교만으로도 시간순 비교가 정확히 맞는다(DB 종류와 무관하게 동작).
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT item_key, item_name, SUM(qty) AS total_qty, COUNT(DISTINCT store_id) AS store_count
    FROM order_events
    WHERE category = ? AND created_at >= ?
    GROUP BY item_key
    ORDER BY total_qty DESC
    LIMIT ?
    """, (category, cutoff, limit))
    rows = cur.fetchall()
    conn.close()

    return [
        {"item_key": r[0], "item_name": r[1], "total_qty": r[2], "store_count": r[3]}
        for r in rows
    ]
