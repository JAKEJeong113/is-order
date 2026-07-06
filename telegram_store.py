# telegram_store.py
"""텔레그램으로 발주하는 가맹점 승인 관리 + 확인 대기중인 담기 목록 저장."""
import os
import sqlite3
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR))
DB_PATH = DATA_DIR / "inventory.db"


def get_conn():
    return sqlite3.connect(DB_PATH)


def init_telegram_tables():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS telegram_stores (
        chat_id TEXT PRIMARY KEY,
        store_name TEXT,
        display_name TEXT,
        approved INTEGER DEFAULT 0,
        requested_at TEXT,
        approved_at TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS telegram_pending_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id TEXT,
        item_name TEXT,
        vendor_id TEXT,
        vendor_name TEXT,
        product_url TEXT,
        item_key TEXT,
        price INTEGER,
        qty INTEGER,
        created_at TEXT
    )
    """)
    conn.commit()
    conn.close()


def register_request(chat_id: str, display_name: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO telegram_stores (chat_id, display_name, approved, requested_at)
    VALUES (?, ?, 0, ?)
    ON CONFLICT(chat_id) DO NOTHING
    """, (chat_id, display_name, now))
    conn.commit()
    conn.close()


def is_approved(chat_id: str) -> tuple[bool, str | None]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT approved, store_name FROM telegram_stores WHERE chat_id = ?", (chat_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return False, None
    return bool(row[0]), row[1]


def list_stores() -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT chat_id, store_name, display_name, approved, requested_at
    FROM telegram_stores ORDER BY requested_at DESC
    """)
    rows = cur.fetchall()
    conn.close()
    return [
        {"chat_id": r[0], "store_name": r[1], "display_name": r[2], "approved": bool(r[3]), "requested_at": r[4]}
        for r in rows
    ]


def approve_store(chat_id: str, store_name: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    UPDATE telegram_stores SET approved = 1, store_name = ?, approved_at = ? WHERE chat_id = ?
    """, (store_name, now, chat_id))
    conn.commit()
    conn.close()


def revoke_store(chat_id: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE telegram_stores SET approved = 0 WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


def save_pending_items(chat_id: str, items: list[dict]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM telegram_pending_items WHERE chat_id = ?", (chat_id,))
    cur.executemany("""
    INSERT INTO telegram_pending_items
    (chat_id, item_name, vendor_id, vendor_name, product_url, item_key, price, qty, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (
            chat_id, it["item_name"], it["vendor_id"], it["vendor_name"],
            it["product_url"], it["item_key"], it.get("price"), it.get("qty", 1), now,
        )
        for it in items
    ])
    conn.commit()
    conn.close()


def get_pending_items(chat_id: str) -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT item_name, vendor_id, vendor_name, product_url, item_key, price, qty
    FROM telegram_pending_items WHERE chat_id = ?
    """, (chat_id,))
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "item_name": r[0], "vendor_id": r[1], "vendor_name": r[2],
            "product_url": r[3], "item_key": r[4], "price": r[5], "qty": r[6],
        }
        for r in rows
    ]


def clear_pending(chat_id: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM telegram_pending_items WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()
