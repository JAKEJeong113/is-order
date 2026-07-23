# web_auth.py
"""PC 웹(/compare) 회원가입/로그인. 누구나 가입하면 가격비교는 바로 사용 가능하고,
관리자가 가맹점 지점과 연결(linked_store_name)해준 계정만 담기 기능이 열린다."""
import hashlib
import os
import secrets
from datetime import datetime, timedelta

import psycopg2

import db_conn

SESSION_TTL_DAYS = 30
PBKDF2_ITERATIONS = 260000
SESSION_COOKIE_NAME = "session_token"


def get_conn():
    return db_conn.get_conn()


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
    cur.execute("""
    CREATE TABLE IF NOT EXISTS password_reset_tokens (
        token TEXT PRIMARY KEY,
        user_id INTEGER,
        created_at TEXT,
        expires_at TEXT,
        used INTEGER NOT NULL DEFAULT 0
    )
    """)
    existing_cols = {row[1] for row in cur.execute("PRAGMA table_info(web_users)").fetchall()}
    for col in ("business_reg_number", "address", "phone"):
        if col not in existing_cols:
            cur.execute(f"ALTER TABLE web_users ADD COLUMN {col} TEXT")
    if "business_reg_image" not in existing_cols:
        cur.execute("ALTER TABLE web_users ADD COLUMN business_reg_image BYTEA")
    if "business_reg_image_mimetype" not in existing_cols:
        cur.execute("ALTER TABLE web_users ADD COLUMN business_reg_image_mimetype TEXT")
    conn.commit()
    conn.close()


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS).hex()


ALLOWED_BUSINESS_REG_IMAGE_MIMETYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_BUSINESS_REG_IMAGE_BYTES = 8 * 1024 * 1024


def signup(
    email: str, password: str, display_name: str,
    business_reg_number: str = "", address: str = "", phone: str = "",
    business_reg_image_bytes: bytes | None = None, business_reg_image_mimetype: str | None = None,
) -> tuple[bool, str]:
    email = email.strip().lower()
    display_name = display_name.strip()
    business_reg_number = business_reg_number.strip().replace("-", "")
    address = address.strip()
    phone = phone.strip()

    if not display_name:
        return False, "상호명을 입력해주세요."
    if not business_reg_number:
        return False, "사업자등록번호를 입력해주세요."
    if not business_reg_number.isdigit() or len(business_reg_number) != 10:
        return False, "사업자등록번호는 숫자 10자리여야 합니다."
    if not address:
        return False, "주소를 입력해주세요."
    if not email or "@" not in email:
        return False, "올바른 이메일을 입력해주세요."
    if len(password) < 6:
        return False, "비밀번호는 6자 이상이어야 합니다."
    if not phone:
        return False, "연락처를 입력해주세요."
    if not business_reg_image_bytes:
        return False, "사업자등록증 이미지를 첨부해주세요."
    if business_reg_image_mimetype not in ALLOWED_BUSINESS_REG_IMAGE_MIMETYPES:
        return False, "사업자등록증은 JPG, PNG, WEBP 이미지만 가능합니다."
    if len(business_reg_image_bytes) > MAX_BUSINESS_REG_IMAGE_BYTES:
        return False, "사업자등록증 이미지 용량은 8MB 이하여야 합니다."

    salt = os.urandom(16)
    password_hash = _hash_password(password, salt)
    now = datetime.now().isoformat(timespec="seconds")

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute("""
        INSERT INTO web_users
            (email, password_hash, password_salt, display_name, business_reg_number, address, phone,
             business_reg_image, business_reg_image_mimetype, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            email, password_hash, salt.hex(), display_name, business_reg_number, address, phone,
            psycopg2.Binary(business_reg_image_bytes), business_reg_image_mimetype, now,
        ))
        conn.commit()
    except psycopg2.IntegrityError:
        conn.rollback()
        conn.close()
        return False, "이미 가입된 이메일입니다."
    conn.close()
    return True, "가입 완료"


def get_business_reg_image(user_id: int) -> tuple[bytes, str] | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT business_reg_image, business_reg_image_mimetype FROM web_users WHERE id = ?
    """, (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row or not row[0]:
        return None
    image_bytes = bytes(row[0])
    return image_bytes, row[1] or "image/jpeg"


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


RESET_TOKEN_TTL_MINUTES = 30


def create_reset_token(email: str) -> str | None:
    """존재하는 이메일이면 토큰을 만들어 돌려주고, 없으면 None.
    이메일 존재 여부를 API 응답으로 노출하지 않기 위해 호출부에서
    None이어도 사용자에게는 항상 같은 성공 메시지를 보여준다."""
    email = email.strip().lower()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT id FROM web_users WHERE email = ?", (email,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return None

    token = secrets.token_urlsafe(32)
    now = datetime.now()
    expires = now + timedelta(minutes=RESET_TOKEN_TTL_MINUTES)
    cur.execute("""
    INSERT INTO password_reset_tokens (token, user_id, created_at, expires_at, used)
    VALUES (?, ?, ?, ?, 0)
    """, (token, row[0], now.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds")))
    conn.commit()
    conn.close()
    return token


def verify_reset_token(token: str) -> int | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT user_id, expires_at, used FROM password_reset_tokens WHERE token = ?
    """, (token,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    user_id, expires_at, used = row
    if used or datetime.fromisoformat(expires_at) < datetime.now():
        return None
    return user_id


def reset_password(token: str, new_password: str) -> tuple[bool, str]:
    if len(new_password) < 6:
        return False, "비밀번호는 6자 이상이어야 합니다."

    user_id = verify_reset_token(token)
    if not user_id:
        return False, "유효하지 않거나 만료된 링크입니다."

    salt = os.urandom(16)
    password_hash = _hash_password(new_password, salt)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    UPDATE web_users SET password_hash = ?, password_salt = ? WHERE id = ?
    """, (password_hash, salt.hex(), user_id))
    cur.execute("UPDATE password_reset_tokens SET used = 1 WHERE token = ?", (token,))
    cur.execute("DELETE FROM web_sessions WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()
    return True, "비밀번호가 변경되었습니다."


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


def get_user_by_id(user_id: int) -> dict | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT id, email, display_name FROM web_users WHERE id = ?
    """, (user_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {"id": row[0], "email": row[1], "display_name": row[2]}


def delete_user(user_id: int) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM password_reset_tokens WHERE user_id = ?", (user_id,))
    cur.execute("DELETE FROM web_sessions WHERE user_id = ?", (user_id,))
    cur.execute("DELETE FROM web_users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()


def link_store(user_id: int, store_name: str | None) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE web_users SET linked_store_name = ? WHERE id = ?", (store_name or None, user_id))
    conn.commit()
    conn.close()


def get_linked_store_name_by_email(email: str) -> str | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT linked_store_name FROM web_users WHERE email = ?", (email.strip().lower(),))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def get_email_by_linked_store_name(store_name: str) -> str | None:
    """반대 방향 조회 - 텔레그램 지점(store_name)에 연결된 웹 계정 이메일을
    찾는다(도매처 활성화/주거래처 설정을 웹↔텔레그램 양쪽에 동기화할 때 씀)."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT email FROM web_users WHERE linked_store_name = ? LIMIT 1", (store_name,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None
