from datetime import datetime

import db_conn


def get_conn():
    return db_conn.get_conn()


def init_db():
    conn = get_conn()
    cur = conn.cursor()

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