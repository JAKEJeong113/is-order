import os
import sqlite3
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR))
DB_PATH = DATA_DIR / "inventory.db"


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    # 가맹점이 늘면서 동시 쓰기가 겹칠 때 "database is locked"로 즉시 실패하는
    # 대신 최대 5초까지 기다렸다가 재시도하도록 한다.
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # WAL 모드는 DB 파일에 한 번 설정하면 계속 유지되는 파일 단위 설정이라
    # 앱 시작 시 한 번만 실행하면 된다 - 읽기 작업이 쓰기 작업을 막지 않게
    # 되어 동시 접속이 늘어날 때 락 경합이 크게 줄어든다.
    cur.execute("PRAGMA journal_mode=WAL")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS inventory (
        barcode TEXT PRIMARY KEY,
        menu_name TEXT,
        current_stock INTEGER DEFAULT 0,
        box_qty INTEGER DEFAULT 1,
        updated_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS inventory_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        barcode TEXT,
        menu_name TEXT,
        change_type TEXT,
        change_qty INTEGER,
        before_stock INTEGER,
        after_stock INTEGER,
        memo TEXT,
        created_at TEXT
    )
    """)

    conn.commit()
    conn.close()


def get_inventory(barcode: str):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    SELECT barcode, menu_name, current_stock, box_qty
    FROM inventory
    WHERE barcode = ?
    """, (barcode,))

    row = cur.fetchone()
    conn.close()

    if not row:
        return None

    return {
        "barcode": row[0],
        "menu_name": row[1],
        "current_stock": row[2],
        "box_qty": row[3],
    }


def upsert_inventory(barcode: str, menu_name: str, current_stock: int, box_qty: int = 1, change_type: str = "ADJUST", memo: str = ""):
    now = datetime.now().isoformat(timespec="seconds")
    existing = get_inventory(barcode)
    before_stock = existing["current_stock"] if existing else 0

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO inventory (barcode, menu_name, current_stock, box_qty, updated_at)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(barcode) DO UPDATE SET
        menu_name = excluded.menu_name,
        current_stock = excluded.current_stock,
        box_qty = excluded.box_qty,
        updated_at = excluded.updated_at
    """, (barcode, menu_name, current_stock, box_qty, now))

    cur.execute("""
    INSERT INTO inventory_history
    (barcode, menu_name, change_type, change_qty, before_stock, after_stock, memo, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        barcode,
        menu_name,
        change_type,
        current_stock - before_stock,
        before_stock,
        current_stock,
        memo,
        now
    ))

    conn.commit()
    conn.close()


def change_stock(barcode: str, menu_name: str, change_qty: int, box_qty: int = 1, change_type: str = "ADJUST", memo: str = ""):
    existing = get_inventory(barcode)
    before_stock = existing["current_stock"] if existing else 0
    after_stock = max(0, before_stock + change_qty)

    upsert_inventory(
        barcode=barcode,
        menu_name=menu_name,
        current_stock=after_stock,
        box_qty=box_qty,
        change_type=change_type,
        memo=memo
    )

    return after_stock