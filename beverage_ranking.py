# beverage_ranking.py
"""전 가맹점 합산 음료 인기순위 + 쿠팡 파트너스 링크 캐시.
파트너스 링크는 발급 후 24시간만 유효해서 매일 새로 발급해서 저장해둔다."""
import hashlib
import hmac
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

import mapping
import popularity

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR))
DB_PATH = DATA_DIR / "inventory.db"
COUPANG_CATALOG_XLSX_PATH = BASE_DIR / "coupang_catalog_sample_2.xlsx"

CP_ACCESS_KEY = os.getenv("CP_ACCESS_KEY", "")
CP_SECRET_KEY = os.getenv("CP_SECRET_KEY", "")
CP_DOMAIN = "https://api-gateway.coupang.com"
CP_METHOD = "POST"
CP_PATH = "/v2/providers/affiliate_open_api/apis/openapi/deeplink"

RANKING_LIMIT = 20


def get_conn():
    return sqlite3.connect(DB_PATH)


def init_beverage_ranking_table():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS beverage_rankings (
        rank INTEGER PRIMARY KEY,
        item_key TEXT,
        item_name TEXT,
        total_qty INTEGER,
        store_count INTEGER,
        partners_link TEXT,
        refreshed_at TEXT
    )
    """)
    conn.commit()
    conn.close()


def _build_search_url(keyword: str) -> str:
    q = quote(keyword)
    return f"https://www.coupang.com/np/search?component=&q={q}&channel=user"


def _make_signed_date() -> str:
    return datetime.now(timezone.utc).strftime("%y%m%dT%H%M%SZ")


def _make_authorization(method: str, path: str, query: str, access_key: str, secret_key: str) -> str:
    signed_date = _make_signed_date()
    message = f"{signed_date}{method}{path}{query}"
    signature = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"CEA algorithm=HmacSHA256, access-key={access_key}, signed-date={signed_date}, signature={signature}"


def create_partners_link(keyword: str) -> str:
    if not CP_ACCESS_KEY or not CP_SECRET_KEY:
        raise RuntimeError("CP_ACCESS_KEY / CP_SECRET_KEY 환경변수가 설정되지 않았습니다.")

    source_url = _build_search_url(keyword)
    authorization = _make_authorization(CP_METHOD, CP_PATH, "", CP_ACCESS_KEY, CP_SECRET_KEY)

    resp = requests.post(
        f"{CP_DOMAIN}{CP_PATH}",
        headers={"Authorization": authorization, "Content-Type": "application/json"},
        data=json.dumps({"coupangUrls": [source_url]}),
        timeout=30,
    )
    resp.raise_for_status()
    result = resp.json()
    if result.get("rCode") != "0":
        raise RuntimeError(f"파트너스 링크 생성 실패: {result}")

    data = result.get("data", [])
    if not data:
        raise RuntimeError(f"파트너스 링크 결과 없음: {result}")

    shorten_url = data[0].get("shortenUrl")
    if not shorten_url:
        raise RuntimeError(f"shortenUrl 없음: {result}")
    return shorten_url


def refresh_beverage_rankings() -> dict:
    """음료 인기순위 top N을 다시 계산하고, 각각 새 파트너스 링크를 발급해서 저장한다."""
    catalog = {}
    try:
        catalog = mapping.load_coupang_catalog_xlsx(str(COUPANG_CATALOG_XLSX_PATH))
    except Exception as e:
        print("[BEVERAGE_RANKING] 카탈로그 로드 실패:", e)

    top_items = popularity.get_top_items("beverage", limit=RANKING_LIMIT)
    now = datetime.now().isoformat(timespec="seconds")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM beverage_rankings")

    saved = 0
    for i, item in enumerate(top_items, start=1):
        item_key = item.get("item_key") or ""
        cat_entry = catalog.get(item_key)
        # 검색 키워드는 카탈로그에 등록된 정제된 키워드를 우선 쓰고, 없으면 상품명 그대로 쓴다.
        keyword = (cat_entry.search_keyword if cat_entry and cat_entry.search_keyword else "") or item.get("item_name") or item_key

        link = None
        try:
            link = create_partners_link(keyword)
        except Exception as e:
            print(f"[BEVERAGE_RANKING] {keyword!r} 파트너스 링크 생성 실패:", e)

        cur.execute("""
        INSERT INTO beverage_rankings (rank, item_key, item_name, total_qty, store_count, partners_link, refreshed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (i, item_key, item.get("item_name"), item.get("total_qty"), item.get("store_count"), link, now))
        saved += 1

    conn.commit()
    conn.close()
    return {"ok": True, "count": saved, "refreshed_at": now}


def get_beverage_rankings() -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT rank, item_name, total_qty, store_count, partners_link, refreshed_at
    FROM beverage_rankings ORDER BY rank ASC
    """)
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "rank": r[0], "item_name": r[1], "total_qty": r[2],
            "store_count": r[3], "partners_link": r[4], "refreshed_at": r[5],
        }
        for r in rows
    ]
