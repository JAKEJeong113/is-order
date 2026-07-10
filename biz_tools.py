# biz_tools.py
"""브랜드 메인 페이지(/) "영업 꿀템 추천" 슬라이더 카드. 원래 index.html에
하드코딩되어 있던 10개 카드(이미지 파일도 실제로는 없어서 깨져 있었음)를
관리자가 상품명/이미지 링크/쿠팡 링크를 직접 입력해 추가·수정·삭제할 수
있도록 DB로 옮긴 것. 로그인 없이 누구나 보는 공개 페이지라 서버 렌더링으로
바로 내려준다(클라이언트에서 fetch로 채우지 않음)."""
import os
import sqlite3
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR))
DB_PATH = DATA_DIR / "inventory.db"

# 기존 하드코딩 카드 중 실제 검색 링크가 있던 5개만 최초 배포 시 시드로
# 넣는다(이미지 링크는 없었으므로 빈 값 - 관리자가 이후 채워 넣으면 됨).
_SEED_TOOLS = [
    ("아이스크림 디스플레이 냉동고", "https://www.coupang.com/np/search?q=아이스크림 냉동고"),
    ("아이스크림 용기컵", "https://www.coupang.com/np/search?q=아이스크림 종이컵"),
    ("아이스크림 스푼", "https://www.coupang.com/np/search?q=아이스크림 스푼"),
    ("아이스크림 콘", "https://www.coupang.com/np/search?q=아이스크림 콘"),
    ("토핑 & 시럽", "https://www.coupang.com/np/search?q=토핑 시럽"),
]


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_table() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS biz_tools (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_name TEXT NOT NULL,
        image_url TEXT,
        product_url TEXT NOT NULL,
        sort_order INTEGER NOT NULL DEFAULT 0,
        created_at TEXT
    )
    """)
    cur.execute("SELECT COUNT(*) FROM biz_tools")
    if cur.fetchone()[0] == 0:
        now = datetime.now().isoformat(timespec="seconds")
        for i, (name, url) in enumerate(_SEED_TOOLS):
            cur.execute(
                "INSERT INTO biz_tools (item_name, image_url, product_url, sort_order, created_at) VALUES (?, '', ?, ?, ?)",
                (name, url, i, now),
            )
    conn.commit()
    conn.close()


def list_tools() -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, item_name, image_url, product_url FROM biz_tools ORDER BY sort_order ASC, id ASC")
    rows = cur.fetchall()
    conn.close()
    return [{"id": r[0], "item_name": r[1], "image_url": r[2], "product_url": r[3]} for r in rows]


def add_tool(item_name: str, image_url: str, product_url: str) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM biz_tools")
    next_order = cur.fetchone()[0]
    now = datetime.now().isoformat(timespec="seconds")
    cur.execute(
        "INSERT INTO biz_tools (item_name, image_url, product_url, sort_order, created_at) VALUES (?, ?, ?, ?, ?)",
        (item_name, image_url, product_url, next_order, now),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def update_tool(tool_id: int, item_name: str, image_url: str, product_url: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE biz_tools SET item_name = ?, image_url = ?, product_url = ? WHERE id = ?",
        (item_name, image_url, product_url, tool_id),
    )
    updated = cur.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def delete_tool(tool_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM biz_tools WHERE id = ?", (tool_id,))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted
