# telegram_store.py
"""텔레그램으로 발주하는 가맹점 승인 관리 + 확인 대기중인 담기 목록 저장."""
import json
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

    # 기존에 만들어진 테이블에 새 컬럼을 안전하게 추가 (마이그레이션)
    existing_cols = {row[1] for row in cur.execute("PRAGMA table_info(telegram_stores)").fetchall()}
    new_columns = [
        "phone TEXT", "business_number TEXT", "registration_step TEXT",
        "cred_vendor TEXT", "cred_step TEXT", "cred_temp_id TEXT",
        "preferred_vendor TEXT", "disabled_vendors TEXT", "disambig_state TEXT",
    ]
    for col_def in new_columns:
        col_name = col_def.split()[0]
        if col_name not in existing_cols:
            cur.execute(f"ALTER TABLE telegram_stores ADD COLUMN {col_def}")

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


def get_registration(chat_id: str) -> dict | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT chat_id, store_name, display_name, phone, business_number, registration_step, approved,
           cred_vendor, cred_step, cred_temp_id, preferred_vendor, disabled_vendors
    FROM telegram_stores WHERE chat_id = ?
    """, (chat_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "chat_id": row[0], "store_name": row[1], "display_name": row[2],
        "phone": row[3], "business_number": row[4],
        "registration_step": row[5], "approved": bool(row[6]),
        "cred_vendor": row[7], "cred_step": row[8], "cred_temp_id": row[9],
        "preferred_vendor": row[10],
        "disabled_vendors": [v for v in (row[11] or "").split(",") if v],
    }


def set_preferred_vendor(chat_id: str, vendor_id: str | None) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE telegram_stores SET preferred_vendor = ? WHERE chat_id = ?", (vendor_id, chat_id))
    conn.commit()
    conn.close()


def get_disambig_state(chat_id: str) -> dict | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT disambig_state FROM telegram_stores WHERE chat_id = ?", (chat_id,))
    row = cur.fetchone()
    conn.close()
    if not row or not row[0]:
        return None
    return json.loads(row[0])


def set_disambig_state(chat_id: str, state: dict | None) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE telegram_stores SET disambig_state = ? WHERE chat_id = ?",
        (json.dumps(state, ensure_ascii=False) if state else None, chat_id),
    )
    conn.commit()
    conn.close()


def set_vendor_enabled_for_store(chat_id: str, vendor_id: str, enabled: bool) -> None:
    reg = get_registration(chat_id)
    disabled = set(reg["disabled_vendors"]) if reg else set()
    if enabled:
        disabled.discard(vendor_id)
    else:
        disabled.add(vendor_id)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE telegram_stores SET disabled_vendors = ? WHERE chat_id = ?",
        (",".join(sorted(disabled)), chat_id),
    )
    conn.commit()
    conn.close()


def start_credential_menu(chat_id: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    UPDATE telegram_stores SET cred_vendor = NULL, cred_step = 'vendor', cred_temp_id = NULL
    WHERE chat_id = ?
    """, (chat_id,))
    conn.commit()
    conn.close()


def start_credential_registration(chat_id: str, vendor_id: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    UPDATE telegram_stores SET cred_vendor = ?, cred_step = 'id', cred_temp_id = NULL
    WHERE chat_id = ?
    """, (vendor_id, chat_id))
    conn.commit()
    conn.close()


def save_credential_id(chat_id: str, login_id: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    UPDATE telegram_stores SET cred_temp_id = ?, cred_step = 'pwd' WHERE chat_id = ?
    """, (login_id, chat_id))
    conn.commit()
    conn.close()


def clear_credential_registration(chat_id: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    UPDATE telegram_stores SET cred_vendor = NULL, cred_step = NULL, cred_temp_id = NULL WHERE chat_id = ?
    """, (chat_id,))
    conn.commit()
    conn.close()


def start_registration(chat_id: str, display_name: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO telegram_stores (chat_id, display_name, registration_step, approved, requested_at)
    VALUES (?, ?, 'store_name', 0, ?)
    ON CONFLICT(chat_id) DO NOTHING
    """, (chat_id, display_name, now))
    conn.commit()
    conn.close()


def save_registration_field(chat_id: str, field: str, value: str, next_step: str | None) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        f"UPDATE telegram_stores SET {field} = ?, registration_step = ? WHERE chat_id = ?",
        (value, next_step, chat_id),
    )
    conn.commit()
    conn.close()


def register_request(chat_id: str, display_name: str) -> None:
    """하위호환용: 등록 절차 없이 바로 대기 상태로만 남기고 싶을 때."""
    start_registration(chat_id, display_name)


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
    SELECT chat_id, store_name, display_name, phone, business_number, registration_step, approved, requested_at
    FROM telegram_stores ORDER BY requested_at DESC
    """)
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "chat_id": r[0], "store_name": r[1], "display_name": r[2],
            "phone": r[3], "business_number": r[4], "registration_step": r[5],
            "approved": bool(r[6]), "requested_at": r[7],
        }
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
