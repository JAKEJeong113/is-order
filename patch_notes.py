# patch_notes.py
"""기능 업데이트 시 버전별 요약을 남기는 패치노트. 관리자만 작성/삭제하고,
로그인한 가맹점 전체가 읽을 수 있다."""
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


def init_patch_notes_table():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS patch_notes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        version TEXT NOT NULL,
        summary TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)
    conn.commit()
    conn.close()


def add_patch_note(version: str, summary: str) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO patch_notes (version, summary, created_at) VALUES (?, ?, ?)",
        (version, summary, now),
    )
    conn.commit()
    new_id = cur.lastrowid
    conn.close()
    return new_id


def list_patch_notes() -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, version, summary, created_at FROM patch_notes ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    return [
        {"id": r[0], "version": r[1], "summary": r[2], "created_at": r[3]}
        for r in rows
    ]


def delete_patch_note(note_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM patch_notes WHERE id = ?", (note_id,))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted
