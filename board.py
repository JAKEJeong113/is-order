# board.py
"""패치노트 페이지 하단에 붙는 두 게시판.
- 공지사항: 관리자만 작성, 로그인한 모든 가맹점이 읽는다(패치노트와 동일한 성격).
- 제안하기: 가맹점이 자유롭게 제출하고, 자기가 낸 것만 다시 볼 수 있다.
  전체 목록은 대표님(관리자)만 본다 - 특정 지점의 불만/이슈가 다른
  가맹점에 노출되면 안 되기 때문."""
from datetime import datetime

import db_conn


def get_conn():
    return db_conn.get_conn()


def init_announcements_table() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS announcements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)
    conn.commit()
    conn.close()


def add_announcement(title: str, content: str) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO announcements (title, content, created_at) VALUES (?, ?, ?) RETURNING id",
        (title, content, now),
    )
    conn.commit()
    new_id = cur.fetchone()[0]
    conn.close()
    return new_id


def list_announcements() -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, title, content, created_at FROM announcements ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    return [{"id": r[0], "title": r[1], "content": r[2], "created_at": r[3]} for r in rows]


def delete_announcement(announcement_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM announcements WHERE id = ?", (announcement_id,))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def init_suggestions_table() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS suggestions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        store_id TEXT NOT NULL,
        display_name TEXT,
        content TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'new',
        created_at TEXT NOT NULL
    )
    """)
    conn.commit()
    conn.close()


def add_suggestion(store_id: str, display_name: str, content: str) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO suggestions (store_id, display_name, content, status, created_at) "
        "VALUES (?, ?, ?, 'new', ?) RETURNING id",
        (store_id, display_name, content, now),
    )
    conn.commit()
    new_id = cur.fetchone()[0]
    conn.close()
    return new_id


def list_suggestions() -> list[dict]:
    """관리자용 - 전체 지점의 제안을 최신순으로 본다."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT id, store_id, display_name, content, status, created_at
    FROM suggestions ORDER BY id DESC
    """)
    rows = cur.fetchall()
    conn.close()
    return [
        {"id": r[0], "store_id": r[1], "display_name": r[2], "content": r[3], "status": r[4], "created_at": r[5]}
        for r in rows
    ]


def list_my_suggestions(store_id: str) -> list[dict]:
    """제출한 본인만 자기 제안 이력을 볼 수 있다."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT id, content, status, created_at FROM suggestions
    WHERE store_id = ? ORDER BY id DESC
    """, (store_id,))
    rows = cur.fetchall()
    conn.close()
    return [{"id": r[0], "content": r[1], "status": r[2], "created_at": r[3]} for r in rows]


def mark_suggestion_read(suggestion_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE suggestions SET status = 'read' WHERE id = ?", (suggestion_id,))
    updated = cur.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def delete_suggestion(suggestion_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM suggestions WHERE id = ?", (suggestion_id,))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted
