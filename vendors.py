# vendors.py
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR))
DB_PATH = DATA_DIR / "inventory.db"

VENDORS = {
    "yamimall": {"name": "야미몰", "base_url": "https://xn--352blx12s.com", "free_shipping_threshold": 150000},
    "ccdome": {"name": "과자생각", "base_url": "https://www.ccdome.co.kr", "free_shipping_threshold": 100000, "catalog_category_code": "017"},
    "3bong": {"name": "삼봉몰", "base_url": "https://3bong.kr", "free_shipping_threshold": 100000, "catalog_category_code": "021"},
    "samwon": {"name": "삼원유통", "base_url": "https://15774281.com", "free_shipping_threshold": 300000},
}


def _get_fernet() -> Fernet:
    key = os.getenv("VENDOR_CRED_KEY")
    if not key:
        raise RuntimeError("VENDOR_CRED_KEY 환경변수가 설정되지 않았습니다.")
    return Fernet(key.encode("utf-8"))


def get_conn():
    return sqlite3.connect(DB_PATH)


def init_vendor_table():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS vendor_credentials (
        vendor_id TEXT PRIMARY KEY,
        enabled INTEGER DEFAULT 0,
        login_id_enc TEXT,
        login_pwd_enc TEXT,
        updated_at TEXT
    )
    """)
    conn.commit()
    conn.close()


def list_vendors() -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT vendor_id, enabled, login_id_enc FROM vendor_credentials")
    rows = {r[0]: r for r in cur.fetchall()}
    conn.close()

    result = []
    for vendor_id, meta in VENDORS.items():
        row = rows.get(vendor_id)
        result.append({
            "vendor_id": vendor_id,
            "name": meta["name"],
            "base_url": meta["base_url"],
            "free_shipping_threshold": meta["free_shipping_threshold"],
            "enabled": bool(row[1]) if row else False,
            "has_credentials": bool(row and row[2]),
        })
    return result


def set_vendor_credentials(vendor_id: str, login_id: str, login_pwd: str) -> None:
    if vendor_id not in VENDORS:
        raise ValueError(f"알 수 없는 도매처: {vendor_id}")

    fernet = _get_fernet()
    login_id_enc = fernet.encrypt(login_id.encode("utf-8")).decode("utf-8")
    login_pwd_enc = fernet.encrypt(login_pwd.encode("utf-8")).decode("utf-8")
    now = datetime.now().isoformat(timespec="seconds")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO vendor_credentials (vendor_id, enabled, login_id_enc, login_pwd_enc, updated_at)
    VALUES (?, 1, ?, ?, ?)
    ON CONFLICT(vendor_id) DO UPDATE SET
        login_id_enc = excluded.login_id_enc,
        login_pwd_enc = excluded.login_pwd_enc,
        enabled = 1,
        updated_at = excluded.updated_at
    """, (vendor_id, login_id_enc, login_pwd_enc, now))
    conn.commit()
    conn.close()


def set_vendor_enabled(vendor_id: str, enabled: bool) -> None:
    if vendor_id not in VENDORS:
        raise ValueError(f"알 수 없는 도매처: {vendor_id}")

    now = datetime.now().isoformat(timespec="seconds")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO vendor_credentials (vendor_id, enabled, updated_at)
    VALUES (?, ?, ?)
    ON CONFLICT(vendor_id) DO UPDATE SET
        enabled = excluded.enabled,
        updated_at = excluded.updated_at
    """, (vendor_id, int(enabled), now))
    conn.commit()
    conn.close()


def get_vendor_credentials(vendor_id: str) -> tuple[str, str] | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT login_id_enc, login_pwd_enc, enabled FROM vendor_credentials WHERE vendor_id = ?",
        (vendor_id,),
    )
    row = cur.fetchone()
    conn.close()

    if not row or not row[2] or not row[0] or not row[1]:
        return None

    fernet = _get_fernet()
    try:
        login_id = fernet.decrypt(row[0].encode("utf-8")).decode("utf-8")
        login_pwd = fernet.decrypt(row[1].encode("utf-8")).decode("utf-8")
    except InvalidToken:
        raise RuntimeError(
            f"{vendor_id} 자격증명 복호화 실패 (VENDOR_CRED_KEY가 저장 시점과 다릅니다)"
        )
    return login_id, login_pwd


def get_enabled_vendor_ids() -> list[str]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT vendor_id FROM vendor_credentials WHERE enabled = 1 AND login_id_enc IS NOT NULL"
    )
    ids = [r[0] for r in cur.fetchall()]
    conn.close()
    return ids
