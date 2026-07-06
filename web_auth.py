# web_auth.py
"""PC 웹(/compare) 회원가입/로그인. 누구나 가입하면 가격비교는 바로 사용 가능하고,
관리자가 가맹점 지점과 연결(linked_store_name)해준 계정만 담기 기능이 열린다."""
import hashlib
import os
import secrets
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR))
DB_PATH = DATA_DIR / "inventory.db"

SESSION_TTL_DAYS = 30
PBKDF2_ITERATIONS = 260000
SESSION_COOKIE_NAME = "session_token"


def get_conn():
    return sqlite3.connect(DB_PATH)


def init_web_auth_tables():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS web_users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE,
        password_hash TEXT,
        password_salt TEXT,
        display_name TEXT,
        linked_store_name TEXT,
        created_at TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS web_sessions (
        token TEXT PRIMARY KEY,
        user_id INTEGER,
        created_at TEXT,
        expires_at TEXT
    )
    """)
    conn.commit()
    conn.close()


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS).hex()


def signup(email: str, password: str, display_name: str) -> tuple[bool, str]:
    email = email.strip().lower()
    if not email or "@" not in email:
        return False, "올바른 이메일을 입력해주세요."
    if len(password) < 6:
        return False, "비밀번호는 6자 이상이어야 합니다."

    salt = os.urandom(16)
    password_hash = _hash_password(password, salt)
    now = datetime.now().isoformat(timespec="seconds")

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
        INSERT INTO web_users (email, password_hash, password_salt, display_name, created_at)
        VALUES (?, ?, ?, ?, ?)
        """, (email, password_hash, salt.hex(), display_name.strip(), now))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return False, "이미 가입된 이메일입니다."
    conn.close()
    return True, "가입 완료"


def verify_login(email: str, password: str) -> int | None:
    email = email.strip().lower()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id, password_hash, password_salt FROM web_users WHERE email = ?", (email,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None

    user_id, stored_hash, salt_hex = row
    computed = _hash_password(password, bytes.fromhex(salt_hex))
    if secrets.compare_digest(computed, stored_hash):
        return user_id
    return None


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    expires = now + timedelta(days=SESSION_TTL_DAYS)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO web_sessions (token, user_id, created_at, expires_at)
    VALUES (?, ?, ?, ?)
    """, (token, user_id, now.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds")))
    conn.commit()
    conn.close()
    return token


def get_user_from_session(token: str | None) -> dict | None:
    if not token:
        return None

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT web_users.id, web_users.email, web_users.display_name, web_users.linked_store_name, web_sessions.expires_at
    FROM web_sessions JOIN web_users ON web_sessions.user_id = web_users.id
    WHERE web_sessions.token = ?
    """, (token,))
    row = cur.fetchone()
    conn.close()

    if not row:
        return None
    if datetime.fromisoformat(row[4]) < datetime.now():
        return None

    return {"id": row[0], "email": row[1], "display_name": row[2], "linked_store_name": row[3]}


def destroy_session(token: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM web_sessions WHERE token = ?", (token,))
    conn.commit()
    conn.close()


def list_users() -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT id, email, display_name, linked_store_name, created_at
    FROM web_users ORDER BY created_at DESC
    """)
    rows = cur.fetchall()
    conn.close()
    return [
        {"id": r[0], "email": r[1], "display_name": r[2], "linked_store_name": r[3], "created_at": r[4]}
        for r in rows
    ]


def link_store(user_id: int, store_name: str | None) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE web_users SET linked_store_name = ? WHERE id = ?", (store_name or None, user_id))
    conn.commit()
    conn.close()
