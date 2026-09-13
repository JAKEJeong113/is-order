# barcode_app_patch_notes.py
"""무인 바코드 검색기(barcode.is-cream.co.kr + 그걸 감싼 안드로이드 앱) 전용
패치노트. 본체(is-order)의 patch_notes.py는 로그인한 가맹점용 별도
버전 체계를 쓰므로, 이 앱만의 업데이트 이력을 헷갈리지 않게 테이블을
분리한다. 앱 메인 화면의 메뉴 > 패치노트에서 로그인 없이 누구나 본다."""
from datetime import datetime

import db_conn


def get_conn():
    return db_conn.get_conn()


def init_barcode_app_patch_notes_table() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS barcode_app_patch_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        version TEXT NOT NULL,
        title TEXT NOT NULL,
        detail TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)
    conn.commit()
    conn.close()


def add_barcode_app_patch_note(version: str, title: str, detail: str) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO barcode_app_patch_notes (version, title, detail, created_at) VALUES (?, ?, ?, ?) RETURNING id",
        (version, title, detail, now),
    )
    conn.commit()
    new_id = cur.fetchone()[0]
    conn.close()
    return new_id


def list_barcode_app_patch_notes(limit: int = 100) -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, version, title, detail, created_at FROM barcode_app_patch_notes ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()
    return [
        {"id": r[0], "version": r[1], "title": r[2], "detail": r[3], "created_at": r[4]}
        for r in rows
    ]


def delete_barcode_app_patch_note(note_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM barcode_app_patch_notes WHERE id = ?", (note_id,))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted
