# store_expiry.py
"""지점별로 실제 담기(add_to_cart) 시점에 화면에서 읽은 유통기한을 기록해두고,
임박하면 텔레그램으로 알려주는 기능. 담을 때마다 그 시점 값으로 최신화하고,
같은 상품을 재발주하면 새 유통기한으로 덮어써서 항상 "지금 매장에 있을 것으로
예상되는 재고"의 유통기한을 반영한다(배치별로 따로 추적하지 않음 - 재발주=
새 배치가 들어왔다고 가정). 유통기한이 지난 항목은 더 이상 추적할 이유가
없어 알림 체크 때 자동으로 삭제한다."""
from datetime import date, datetime

import db_conn

# 알림 기준일(일 단위) - store_item_expiry의 notified_{n}d 컬럼과 1:1 대응.
# 지점마다 store_expiry_prefs에서 이 중 어떤 걸 받을지 켜고 끌 수 있다.
THRESHOLDS = (30, 14, 7)


def get_conn():
    return db_conn.get_conn()


def init_store_expiry_tables() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS store_item_expiry (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        store_id TEXT NOT NULL,
        vendor_id TEXT NOT NULL,
        item_key TEXT NOT NULL,
        item_name TEXT NOT NULL,
        expiry_date TEXT NOT NULL,
        notified_30d INTEGER NOT NULL DEFAULT 0,
        notified_14d INTEGER NOT NULL DEFAULT 0,
        notified_7d INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )
    """)
    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_store_item_expiry_unique
    ON store_item_expiry (store_id, vendor_id, item_key)
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS store_expiry_prefs (
        store_id TEXT PRIMARY KEY,
        notify_30d INTEGER NOT NULL DEFAULT 1,
        notify_14d INTEGER NOT NULL DEFAULT 1,
        notify_7d INTEGER NOT NULL DEFAULT 1
    )
    """)
    conn.commit()
    conn.close()


def upsert_expiry(store_id: str, vendor_id: str, item_key: str, item_name: str, expiry: date) -> None:
    """담기가 실제로 성공했고 유통기한을 읽어냈을 때만 호출한다(읽기 실패시
    호출 자체를 안 하면 됨 - 이 함수는 값이 있다고 가정한다). 재발주로 다시
    호출되면 새 유통기한으로 덮어쓰고, 이전 배치 기준으로 이미 보낸 알림
    여부(notified_*)도 초기화한다 - 안 그러면 새로 들어온 재고인데 예전 알림
    이력 때문에 정작 필요한 알림이 조용히 씹힐 수 있다."""
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO store_item_expiry
        (store_id, vendor_id, item_key, item_name, expiry_date, notified_30d, notified_14d, notified_7d, updated_at)
    VALUES (?, ?, ?, ?, ?, 0, 0, 0, ?)
    ON CONFLICT(store_id, vendor_id, item_key) DO UPDATE SET
        item_name = excluded.item_name,
        expiry_date = excluded.expiry_date,
        notified_30d = 0,
        notified_14d = 0,
        notified_7d = 0,
        updated_at = excluded.updated_at
    """, (store_id, vendor_id, item_key, item_name, expiry.isoformat(), now))
    conn.commit()
    conn.close()


def get_expiry_prefs(store_id: str) -> dict:
    """설정을 한 번도 안 건드린 지점은 기본으로 3개 기준 전부 켜져 있다고
    본다 - 아무 설정 없이도 바로 동작해야 하고, 알림을 원치 않으면 지점이
    직접 꺼야 자연스럽다(옵트아웃이 옵트인보다 이 기능 취지에 맞음)."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT notify_30d, notify_14d, notify_7d FROM store_expiry_prefs WHERE store_id = ?",
        (store_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return {30: True, 14: True, 7: True}
    return {30: bool(row[0]), 14: bool(row[1]), 7: bool(row[2])}


def set_expiry_pref(store_id: str, days: int, enabled: bool) -> bool:
    if days not in THRESHOLDS:
        return False
    field = f"notify_{days}d"
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"""
    INSERT INTO store_expiry_prefs (store_id, {field})
    VALUES (?, ?)
    ON CONFLICT(store_id) DO UPDATE SET {field} = excluded.{field}
    """, (store_id, int(enabled)))
    conn.commit()
    conn.close()
    return True


def list_all_expiry_rows() -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT id, store_id, vendor_id, item_key, item_name, expiry_date, notified_30d, notified_14d, notified_7d
    FROM store_item_expiry
    """)
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "id": r[0], "store_id": r[1], "vendor_id": r[2], "item_key": r[3], "item_name": r[4],
            "expiry_date": r[5], "notified_30d": bool(r[6]), "notified_14d": bool(r[7]), "notified_7d": bool(r[8]),
        }
        for r in rows
    ]


def mark_notified(row_id: int, days: int) -> None:
    if days not in THRESHOLDS:
        return
    field = f"notified_{days}d"
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"UPDATE store_item_expiry SET {field} = 1 WHERE id = ?", (row_id,))
    conn.commit()
    conn.close()


def delete_expiry_row(row_id: int) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM store_item_expiry WHERE id = ?", (row_id,))
    conn.commit()
    conn.close()
