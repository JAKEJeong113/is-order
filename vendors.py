# vendors.py
import json
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
    "hdinter": {
        "name": "현동몰", "base_url": "https://hd-inter.co.kr", "free_shipping_threshold": 200000,
        # ccdome/3bong과 달리 '전체상품' 단일 코드가 없어서 대분류 카테고리를 모두 순회한다.
        "catalog_category_code": [
            "020003", "020005", "020006", "021003", "021004",
            "022003", "022004", "023001", "023002", "025002",
        ],
    },
    "moomarket": {
        "name": "무마켓", "base_url": "https://moomarket.co.kr", "free_shipping_threshold": 150000,
        "catalog_category_code": "240",  # 카페24 "전체상품" 카테고리(cate_no)
    },
    "douyou": {
        "name": "또요몰", "base_url": "https://www.douyoudouyou.com", "free_shipping_threshold": 150000,
        # 야미몰과 동일한 플랫폼(자체 제작 도매몰) - "전체상품" 단일 코드가 없어서
        # 대분류 아래 세부 카테고리를 모두 순회한다.
        "catalog_category_code": [
            "001001003", "001001004", "001001022", "001001023", "001001024", "001001025",
            "001004001", "001004002", "001004003", "001004004", "001004005", "001004006",
            "001004007", "001004008",
            "001005001", "001005002", "001005003", "001005004", "001005005", "001005006",
            "001006001", "001006002", "001006003",
            "001007001", "001007002", "001007003", "001007004", "001007005", "001007006",
            "001008001", "001008002", "001008003", "001008004", "001008006", "001008007",
        ],
    },
}

# 실제 자동 담기(add_to_cart)가 구현된 도매처 목록. 텔레그램 봇/웹 장바구니
# 양쪽에서 공유해서 쓴다(둘 다 이 목록 밖 도매처는 자동 담기 후보로 보지 않음).
CART_SUPPORTED_VENDORS = ("yamimall", "ccdome", "3bong", "hdinter", "moomarket", "douyou")


def _get_fernet() -> Fernet:
    key = os.getenv("VENDOR_CRED_KEY")
    if not key:
        raise RuntimeError("VENDOR_CRED_KEY 환경변수가 설정되지 않았습니다.")
    return Fernet(key.encode("utf-8"))


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


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


# --- 지점별 도매처 계정 (실제 담기/구매용. 가격비교용 크롤링은 위 대표 계정을 그대로 사용).
# 한 지점이 같은 도매처에 계정을 여러 개 가질 수 있다(다매장 운영 시 도매처 하나에
# 매장별로 계정을 따로 쓰는 경우) - 계정마다 별명(nickname)으로 구분하고, 그중 하나를
# "기본 계정"(is_default)으로 표시해 별명을 몰라도 되는 기존 호출부(웹 /my-vendors,
# 계정 지정 없이 담기 등)가 그대로 동작하게 한다. ---

def init_store_vendor_table():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS store_vendor_credentials (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        store_id TEXT NOT NULL,
        vendor_id TEXT NOT NULL,
        nickname TEXT NOT NULL,
        login_id_enc TEXT,
        login_pwd_enc TEXT,
        is_default INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT
    )
    """)
    conn.commit()
    conn.close()
    # 옛 스키마(별명 없음)가 이미 배포돼 있으면 위 CREATE TABLE IF NOT EXISTS는
    # 조용히 무시되므로, nickname 컬럼이 실제로 있는지 마이그레이션에서 먼저
    # 확인/보정한 다음에야 그 컬럼을 쓰는 인덱스를 만들 수 있다.
    _migrate_legacy_store_vendor_credentials()

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_store_vendor_nickname
    ON store_vendor_credentials (store_id, vendor_id, nickname)
    """)
    conn.commit()
    conn.close()


def _migrate_legacy_store_vendor_credentials() -> None:
    """이 테이블은 원래 (store_id, vendor_id)가 PK라 도매처당 계정을 하나만 저장했다.
    계정을 여러 개(별명 포함) 두도록 스키마를 바꿨는데, 이미 옛 스키마로 배포되어
    있으면 위 CREATE TABLE IF NOT EXISTS가 조용히 무시되므로, 옛 스키마를 감지해서
    기존 계정을 "기본" 별명의 기본 계정으로 옮겨 담는다."""
    conn = get_conn()
    cur = conn.cursor()
    cols = {row[1] for row in cur.execute("PRAGMA table_info(store_vendor_credentials)").fetchall()}
    if "nickname" in cols:
        conn.close()
        return

    cur.execute("ALTER TABLE store_vendor_credentials RENAME TO store_vendor_credentials_old")
    cur.execute("""
    CREATE TABLE store_vendor_credentials (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        store_id TEXT NOT NULL,
        vendor_id TEXT NOT NULL,
        nickname TEXT NOT NULL,
        login_id_enc TEXT,
        login_pwd_enc TEXT,
        is_default INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT
    )
    """)
    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS idx_store_vendor_nickname
    ON store_vendor_credentials (store_id, vendor_id, nickname)
    """)
    cur.execute("""
    INSERT INTO store_vendor_credentials (store_id, vendor_id, nickname, login_id_enc, login_pwd_enc, is_default, updated_at)
    SELECT store_id, vendor_id, '기본', login_id_enc, login_pwd_enc, 1, updated_at
    FROM store_vendor_credentials_old
    """)
    cur.execute("DROP TABLE store_vendor_credentials_old")
    conn.commit()
    conn.close()


def add_store_vendor_account(store_id: str, vendor_id: str, nickname: str, login_id: str, login_pwd: str) -> int:
    """계정을 하나 추가한다(같은 별명이 이미 있으면 그 계정의 아이디/비번을 갱신).
    해당 지점/도매처에 등록된 계정이 하나도 없었으면 이 계정을 자동으로 기본
    계정으로 지정한다. 반환값은 계정 id(계정 선택/세션 캐시 키로 사용)."""
    if vendor_id not in VENDORS:
        raise ValueError(f"알 수 없는 도매처: {vendor_id}")

    fernet = _get_fernet()
    login_id_enc = fernet.encrypt(login_id.encode("utf-8")).decode("utf-8")
    login_pwd_enc = fernet.encrypt(login_pwd.encode("utf-8")).decode("utf-8")
    now = datetime.now().isoformat(timespec="seconds")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM store_vendor_credentials WHERE store_id = ? AND vendor_id = ?",
        (store_id, vendor_id),
    )
    existing_count = cur.fetchone()[0]
    is_first = existing_count == 0
    # 별명을 생략하면 "기본"으로 자동 지정하되, 이미 계정이 있는 상태에서 또
    # 생략하면 "기본"과 충돌해 기존 계정을 덮어써버리므로 "계정N"으로 구분한다.
    nickname = (nickname or "").strip() or ("기본" if is_first else f"계정{existing_count + 1}")

    cur.execute("""
    INSERT INTO store_vendor_credentials (store_id, vendor_id, nickname, login_id_enc, login_pwd_enc, is_default, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(store_id, vendor_id, nickname) DO UPDATE SET
        login_id_enc = excluded.login_id_enc,
        login_pwd_enc = excluded.login_pwd_enc,
        updated_at = excluded.updated_at
    """, (store_id, vendor_id, nickname, login_id_enc, login_pwd_enc, int(is_first), now))
    conn.commit()

    cur.execute(
        "SELECT id FROM store_vendor_credentials WHERE store_id = ? AND vendor_id = ? AND nickname = ?",
        (store_id, vendor_id, nickname),
    )
    account_id = cur.fetchone()[0]
    conn.close()
    return account_id


def set_store_vendor_credentials(store_id: str, vendor_id: str, login_id: str, login_pwd: str) -> None:
    """웹 /my-vendors의 단일 계정 저장용 - 기본 계정이 있으면 그 계정을 덮어쓰고,
    없으면 "기본"이라는 별명으로 새로 만든다(텔레그램에서 여러 계정을 등록해도
    웹은 여전히 계정 하나만 다루므로, 기본 계정을 그대로 갱신 대상으로 쓴다)."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT nickname FROM store_vendor_credentials WHERE store_id = ? AND vendor_id = ? AND is_default = 1",
        (store_id, vendor_id),
    )
    row = cur.fetchone()
    conn.close()
    nickname = row[0] if row else "기본"
    add_store_vendor_account(store_id, vendor_id, nickname, login_id, login_pwd)


def list_store_vendor_accounts(store_id: str, vendor_id: str) -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT id, nickname, is_default FROM store_vendor_credentials
    WHERE store_id = ? AND vendor_id = ?
    ORDER BY is_default DESC, id ASC
    """, (store_id, vendor_id))
    rows = cur.fetchall()
    conn.close()
    return [{"id": r[0], "nickname": r[1], "is_default": bool(r[2])} for r in rows]


def resolve_store_vendor_account(store_id: str, vendor_id: str, account_id: int | None = None) -> dict | None:
    """계정 하나를 확정해서 {id, nickname, login_id, login_pwd}로 반환한다.
    account_id를 안 주면 기본 계정(없으면 가장 먼저 등록한 계정)을 쓴다 - 계정
    구분 없이 호출하던 기존 코드가 계속 동작하게 하는 폴백이다."""
    conn = get_conn()
    cur = conn.cursor()
    if account_id is not None:
        cur.execute(
            "SELECT id, nickname, login_id_enc, login_pwd_enc FROM store_vendor_credentials WHERE id = ? AND store_id = ? AND vendor_id = ?",
            (account_id, store_id, vendor_id),
        )
    else:
        cur.execute("""
        SELECT id, nickname, login_id_enc, login_pwd_enc FROM store_vendor_credentials
        WHERE store_id = ? AND vendor_id = ?
        ORDER BY is_default DESC, id ASC LIMIT 1
        """, (store_id, vendor_id))
    row = cur.fetchone()
    conn.close()

    if not row or not row[2] or not row[3]:
        return None

    fernet = _get_fernet()
    try:
        login_id = fernet.decrypt(row[2].encode("utf-8")).decode("utf-8")
        login_pwd = fernet.decrypt(row[3].encode("utf-8")).decode("utf-8")
    except InvalidToken:
        raise RuntimeError(f"{store_id}/{vendor_id} 자격증명 복호화 실패")
    return {"id": row[0], "nickname": row[1], "login_id": login_id, "login_pwd": login_pwd}


def get_store_vendor_credentials(store_id: str, vendor_id: str, account_id: int | None = None) -> tuple[str, str] | None:
    account = resolve_store_vendor_account(store_id, vendor_id, account_id)
    if not account:
        return None
    return account["login_id"], account["login_pwd"]


# 지점별 계정 등록 + 자동 담기를 지원하는 도매처 (현동몰/무마켓/또요몰은 봇 구현 전까지는
# 계정 등록 화면에는 노출하되 담기 자동화 대상에는 아직 넣지 않는다)
STORE_MANAGED_VENDOR_IDS = ("yamimall", "ccdome", "3bong", "hdinter", "moomarket", "douyou")


def list_store_vendor_status(store_id: str) -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT vendor_id FROM store_vendor_credentials WHERE store_id = ?", (store_id,))
    registered = {r[0] for r in cur.fetchall()}
    conn.close()

    disabled, preferred = get_store_vendor_prefs(store_id)

    return [
        {
            "vendor_id": vid, "name": meta["name"], "registered": vid in registered,
            "enabled": vid not in disabled, "is_preferred": vid == preferred,
        }
        for vid, meta in VENDORS.items()
        if vid in STORE_MANAGED_VENDOR_IDS
    ]


# --- 지점별 도매처 활성화/비활성화 + 주 도매처 설정. 텔레그램 봇은 telegram_stores
# 테이블에 같은 개념을 별도로 갖고 있다(store_id 형식이 chat_id라 이 테이블과
# 자연스럽게 공유하기보다는, 웹(store_id="web:이메일")용으로 새로 둔다). ---

def init_store_vendor_prefs_table() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS store_vendor_prefs (
        store_id TEXT PRIMARY KEY,
        disabled_vendors TEXT,
        preferred_vendor TEXT
    )
    """)
    conn.commit()
    conn.close()


def get_store_vendor_prefs(store_id: str) -> tuple[set, str | None]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT disabled_vendors, preferred_vendor FROM store_vendor_prefs WHERE store_id = ?", (store_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return set(), None
    disabled = {v for v in (row[0] or "").split(",") if v}
    return disabled, row[1]


def _save_store_vendor_prefs(store_id: str, disabled: set, preferred: str | None) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO store_vendor_prefs (store_id, disabled_vendors, preferred_vendor)
    VALUES (?, ?, ?)
    ON CONFLICT(store_id) DO UPDATE SET
        disabled_vendors = excluded.disabled_vendors,
        preferred_vendor = excluded.preferred_vendor
    """, (store_id, ",".join(sorted(disabled)), preferred))
    conn.commit()
    conn.close()


def set_vendor_enabled_for_store(store_id: str, vendor_id: str, enabled: bool) -> None:
    disabled, preferred = get_store_vendor_prefs(store_id)
    if enabled:
        disabled.discard(vendor_id)
    else:
        disabled.add(vendor_id)
        # 비활성화한 도매처가 주 도매처였으면 그 지정도 같이 풀어준다.
        if preferred == vendor_id:
            preferred = None
    _save_store_vendor_prefs(store_id, disabled, preferred)


def set_preferred_vendor_for_store(store_id: str, vendor_id: str | None) -> None:
    disabled, _ = get_store_vendor_prefs(store_id)
    _save_store_vendor_prefs(store_id, disabled, vendor_id)


# --- 담기 속도 개선용: 지점별 로그인 세션(쿠키) 캐시. 매번 새로 로그인하는 대신
# 저장된 쿠키로 브라우저 컨텍스트를 만들어서 로그인 과정을 건너뛴다. ---

def init_session_table():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS store_vendor_sessions (
        store_id TEXT,
        vendor_id TEXT,
        storage_state TEXT,
        saved_at TEXT,
        PRIMARY KEY (store_id, vendor_id)
    )
    """)
    conn.commit()
    conn.close()


def get_session_state(store_id: str, vendor_id: str) -> dict | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT storage_state FROM store_vendor_sessions WHERE store_id = ? AND vendor_id = ?",
        (store_id, vendor_id),
    )
    row = cur.fetchone()
    conn.close()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except ValueError:
        return None


def save_session_state(store_id: str, vendor_id: str, state: dict) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO store_vendor_sessions (store_id, vendor_id, storage_state, saved_at)
    VALUES (?, ?, ?, ?)
    ON CONFLICT(store_id, vendor_id) DO UPDATE SET
        storage_state = excluded.storage_state,
        saved_at = excluded.saved_at
    """, (store_id, vendor_id, json.dumps(state), now))
    conn.commit()
    conn.close()


def clear_session_state(store_id: str, vendor_id: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM store_vendor_sessions WHERE store_id = ? AND vendor_id = ?",
        (store_id, vendor_id),
    )
    conn.commit()
    conn.close()
