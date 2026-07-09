# web_cart.py
"""compare 페이지의 1차 담기(isorder 자체 장바구니). 실제 도매몰 담기는
Playwright로 로그인부터 하는 자동화라 상품 하나에도 수십 초~수 분이 걸려서,
상품마다 누르고 바로 기다려야 하면 여러 개를 빠르게 담을 수 없다. compare
페이지의 "담기"는 이 내부 장바구니에 빠르게(DB 저장만) 쌓아두고, 실제 도매몰
담기는 /cart 페이지에서 사용자가 원할 때 실행한다."""
import os
import sqlite3
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR))
DB_PATH = DATA_DIR / "inventory.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_web_cart_table():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS web_cart_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        store_id TEXT NOT NULL,
        item_name TEXT,
        vendor_id TEXT,
        vendor_name TEXT,
        product_url TEXT,
        item_key TEXT,
        price INTEGER,
        qty INTEGER NOT NULL DEFAULT 1,
        added_at TEXT
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_web_cart_store ON web_cart_items (store_id)")
    conn.commit()
    conn.close()


def add_item(store_id: str, item_name: str, vendor_id: str, vendor_name: str,
             product_url: str, item_key: str, price: int | None, qty: int) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO web_cart_items
        (store_id, item_name, vendor_id, vendor_name, product_url, item_key, price, qty, added_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (store_id, item_name, vendor_id, vendor_name, product_url, item_key, price, qty, now))
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def list_items(store_id: str) -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT id, item_name, vendor_id, vendor_name, product_url, item_key, price, qty, added_at
    FROM web_cart_items WHERE store_id = ? ORDER BY id ASC
    """, (store_id,))
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "id": r[0], "item_name": r[1], "vendor_id": r[2], "vendor_name": r[3],
            "product_url": r[4], "item_key": r[5], "price": r[6], "qty": r[7], "added_at": r[8],
        }
        for r in rows
    ]


def update_qty(store_id: str, item_id: int, qty: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE web_cart_items SET qty = ? WHERE id = ? AND store_id = ?",
        (qty, item_id, store_id),
    )
    updated = cur.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def delete_item(store_id: str, item_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM web_cart_items WHERE id = ? AND store_id = ?", (item_id, store_id))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted
