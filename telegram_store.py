# telegram_store.py
"""텔레그램으로 발주하는 가맹점 승인 관리 + 확인 대기중인 담기 목록 저장."""
import json
from datetime import datetime

import db_conn


def get_conn():
    return db_conn.get_conn()


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
        "cred_vendor TEXT", "cred_step TEXT", "cred_temp_id TEXT", "cred_nickname TEXT",
        "preferred_vendor TEXT", "disabled_vendors TEXT", "disambig_state TEXT",
        "rejected_at TEXT", "reject_reason TEXT",
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

    existing_pending_cols = {row[1] for row in cur.execute("PRAGMA table_info(telegram_pending_items)").fetchall()}
    if "alt_offers_json" not in existing_pending_cols:
        cur.execute("ALTER TABLE telegram_pending_items ADD COLUMN alt_offers_json TEXT")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS broadcast_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        message TEXT NOT NULL,
        sent_count INTEGER NOT NULL,
        failed_count INTEGER NOT NULL,
        total_count INTEGER NOT NULL,
        sent_at TEXT
    )
    """)

    conn.commit()
    conn.close()


def add_broadcast_history(message: str, sent_count: int, failed_count: int, total_count: int) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO broadcast_history (message, sent_count, failed_count, total_count, sent_at)
    VALUES (?, ?, ?, ?, ?) RETURNING id
    """, (message, sent_count, failed_count, total_count, now))
    conn.commit()
    new_id = cur.fetchone()[0]
    conn.close()
    return new_id


def list_broadcast_history(limit: int = 50) -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT id, message, sent_count, failed_count, total_count, sent_at
    FROM broadcast_history ORDER BY id DESC LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "id": r[0], "message": r[1], "sent_count": r[2],
            "failed_count": r[3], "total_count": r[4], "sent_at": r[5],
        }
        for r in rows
    ]


def get_registration(chat_id: str) -> dict | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT chat_id, store_name, display_name, phone, business_number, registration_step, approved,
           cred_vendor, cred_step, cred_temp_id, preferred_vendor, disabled_vendors,
           rejected_at, reject_reason, cred_nickname
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
        "rejected_at": row[12], "reject_reason": row[13], "cred_nickname": row[14],
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
    UPDATE telegram_stores SET cred_vendor = NULL, cred_step = 'vendor', cred_temp_id = NULL, cred_nickname = NULL
    WHERE chat_id = ?
    """, (chat_id,))
    conn.commit()
    conn.close()


def start_credential_registration(chat_id: str, vendor_id: str) -> None:
    """도매처가 정해진 다음 단계는 별명 입력이다 - 계정을 여러 개 등록할 수
    있어서 어느 단계든 별명부터 물어야 구분이 된다."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    UPDATE telegram_stores SET cred_vendor = ?, cred_step = 'nickname', cred_temp_id = NULL, cred_nickname = NULL
    WHERE chat_id = ?
    """, (vendor_id, chat_id))
    conn.commit()
    conn.close()


def save_credential_nickname(chat_id: str, nickname: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    UPDATE telegram_stores SET cred_nickname = ?, cred_step = 'id' WHERE chat_id = ?
    """, (nickname, chat_id))
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
    UPDATE telegram_stores SET cred_vendor = NULL, cred_step = NULL, cred_temp_id = NULL, cred_nickname = NULL WHERE chat_id = ?
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


def restart_registration(chat_id: str, display_name: str) -> None:
    """반려된 신청을 다시 처음부터 시작한다 - start_registration은 INSERT ...
    ON CONFLICT DO NOTHING이라 chat_id 행이 이미 있으면(반려돼도 행은 남아있음)
    조용히 무시돼서 재신청이 막혀버린다. 그래서 반려 후에는 이 함수로 기존 행을
    초기 상태로 되돌려서 등록 절차를 다시 밟게 한다."""
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    UPDATE telegram_stores SET display_name = ?, store_name = NULL, phone = NULL,
        business_number = NULL, registration_step = 'store_name', approved = 0,
        requested_at = ?, rejected_at = NULL, reject_reason = NULL
    WHERE chat_id = ?
    """, (display_name, now, chat_id))
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
    SELECT chat_id, store_name, display_name, phone, business_number, registration_step, approved,
           requested_at, rejected_at, reject_reason
    FROM telegram_stores ORDER BY requested_at DESC
    """)
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "chat_id": r[0], "store_name": r[1], "display_name": r[2],
            "phone": r[3], "business_number": r[4], "registration_step": r[5],
            "approved": bool(r[6]), "requested_at": r[7],
            "rejected_at": r[8], "reject_reason": r[9],
        }
        for r in rows
    ]


def approve_store(chat_id: str, store_name: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    # 반려 이력이 있다가 뒤늦게 승인하는 경우도 있으니, 승인 시 반려 표시는 지운다.
    cur.execute("""
    UPDATE telegram_stores SET approved = 1, store_name = ?, approved_at = ?,
        rejected_at = NULL, reject_reason = NULL
    WHERE chat_id = ?
    """, (store_name, now, chat_id))
    conn.commit()
    conn.close()


def reject_store(chat_id: str, reason: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    UPDATE telegram_stores SET approved = 0, rejected_at = ?, reject_reason = ? WHERE chat_id = ?
    """, (now, reason, chat_id))
    conn.commit()
    conn.close()


def revoke_store(chat_id: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE telegram_stores SET approved = 0 WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()


def delete_store(chat_id: str) -> bool:
    """처리됨(승인/반려) 목록에서 완전히 제거한다. 등록 요청 자체를 지우는
    것이라 승인된 가맹점을 지우면 다시 봇에 메시지를 보냈을 때 신규 등록
    절차부터 시작하게 된다 - 관리 페이지에서 처리됨 항목 정리용으로만 쓴다."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM telegram_stores WHERE chat_id = ?", (chat_id,))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def save_pending_items(chat_id: str, items: list[dict]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM telegram_pending_items WHERE chat_id = ?", (chat_id,))
    cur.executemany("""
    INSERT INTO telegram_pending_items
    (chat_id, item_name, vendor_id, vendor_name, product_url, item_key, price, qty, created_at, alt_offers_json)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, [
        (
            chat_id, it["item_name"], it["vendor_id"], it["vendor_name"],
            it["product_url"], it["item_key"], it.get("price"), it.get("qty", 1), now,
            json.dumps(it.get("alt_offers") or [], ensure_ascii=False),
        )
        for it in items
    ])
    conn.commit()
    conn.close()


def get_pending_items(chat_id: str) -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT item_name, vendor_id, vendor_name, product_url, item_key, price, qty, alt_offers_json
    FROM telegram_pending_items WHERE chat_id = ?
    """, (chat_id,))
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "item_name": r[0], "vendor_id": r[1], "vendor_name": r[2],
            "product_url": r[3], "item_key": r[4], "price": r[5], "qty": r[6],
            "alt_offers": json.loads(r[7]) if r[7] else [],
        }
        for r in rows
    ]


def clear_pending(chat_id: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM telegram_pending_items WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()
