# consumables.py
"""매장 소모품(컵/스푼/냅킨 등) 구매 링크 목록. 관리자 페이지에서 이름/이미지
링크/구매 링크를 직접 입력해 추가·수정·삭제하고, 텔레그램 "소모품" 명령으로
바로 조회한다. biz_tools.py(영업 꿀템)와 완전히 같은 구조 - 시드 데이터만
없다(관리자가 처음부터 채워 넣음)."""
from datetime import datetime

import db_conn


def get_conn():
    return db_conn.get_conn()


def init_table() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS consumables (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_name TEXT NOT NULL,
        image_url TEXT,
        product_url TEXT NOT NULL,
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT
    )
    """)
    conn.commit()
    conn.close()


def list_items() -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, item_name, image_url, product_url FROM consumables ORDER BY sort_order ASC, id ASC")
    rows = cur.fetchall()
    conn.close()
    return [{"id": r[0], "item_name": r[1], "image_url": r[2], "product_url": r[3]} for r in rows]


def add_item(item_name: str, image_url: str, product_url: str) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM consumables")
    next_order = cur.fetchone()[0]
    now = datetime.now().isoformat(timespec="seconds")
    cur.execute(
        "INSERT INTO consumables (item_name, image_url, product_url, sort_order, created_at) VALUES (?, ?, ?, ?, ?) RETURNING id",
        (item_name, image_url, product_url, next_order, now),
    )
    conn.commit()
    new_id = cur.fetchone()[0]
    conn.close()
    return new_id


def update_item(item_id: int, item_name: str, image_url: str, product_url: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE consumables SET item_name = ?, image_url = ?, product_url = ? WHERE id = ?",
        (item_name, image_url, product_url, item_id),
    )
    updated = cur.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def delete_item(item_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM consumables WHERE id = ?", (item_id,))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted
