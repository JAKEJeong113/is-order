# beverage_ranking.py
"""음료 추천 카드 목록 + 쿠팡 파트너스 링크/이미지 캐시.
카탈로그의 '음료수' 카테고리 상품을 전부 카드로 보여주고, 카드를 클릭한 횟수를
기준으로 정렬한다(조회할 때마다 현재 클릭수로 다시 정렬하므로 실시간 반영).

이미지/가격은 쿠팡 상품검색 API(products/search)로 가져오는데, 이 API는 시간당
호출 한도가 엄격하고(실측 시간당 약 90여회) 초과하면 최대 24시간 잠기며 3회
누적되면 계정 자체가 제한된다. 그래서 이 API는 "아직 기준 URL이 없는 상품"에
대해서만 하루 한 번 백필하듯 돌린다 — 카탈로그가 안 바뀌면 둘째 날부터는 처리할
게 없어서 사실상 호출이 0에 수렴한다.

파트너스 추적 링크(24시간 유효)는 별도로, 이미 기준 URL이 있는 상품에 한해
deeplink API로 매일 갱신한다. deeplink API는 발주 임포트 때마다 호출해도 문제
없었던 걸 이미 확인했기 때문에 매일 돌려도 안전하다.

클릭수는 순위 집계용이라 어느 갱신 작업에서도 건드리지 않는다."""
import hashlib
import hmac
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests

import mapping

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR))
DB_PATH = DATA_DIR / "inventory.db"
COUPANG_CATALOG_XLSX_PATH = BASE_DIR / "coupang_catalog_sample_2.xlsx"

CP_ACCESS_KEY = os.getenv("CP_ACCESS_KEY", "")
CP_SECRET_KEY = os.getenv("CP_SECRET_KEY", "")
CP_DOMAIN = "https://api-gateway.coupang.com"
CP_SEARCH_PATH = "/v2/providers/affiliate_open_api/apis/openapi/products/search"
CP_DEEPLINK_PATH = "/v2/providers/affiliate_open_api/apis/openapi/deeplink"

BEVERAGE_CATEGORY = "음료수"
SEARCH_DELAY_SECONDS = 0.3
LINK_DELAY_SECONDS = 0.15


class CoupangRateLimitError(RuntimeError):
    pass


def get_conn():
    return sqlite3.connect(DB_PATH)


def init_beverage_ranking_table():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS beverage_catalog (
        item_key TEXT PRIMARY KEY,
        item_name TEXT,
        image_url TEXT,
        price INTEGER,
        reference_url TEXT,
        partners_link TEXT,
        click_count INTEGER NOT NULL DEFAULT 0,
        image_refreshed_at TEXT,
        link_refreshed_at TEXT
    )
    """)
    conn.commit()
    conn.close()


def _make_signed_date() -> str:
    return datetime.now(timezone.utc).strftime("%y%m%dT%H%M%SZ")


def _make_authorization(method: str, path: str, query: str, access_key: str, secret_key: str) -> str:
    signed_date = _make_signed_date()
    message = f"{signed_date}{method}{path}{query}"
    signature = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"CEA algorithm=HmacSHA256, access-key={access_key}, signed-date={signed_date}, signature={signature}"


def search_coupang_product(keyword: str) -> dict | None:
    """검색어로 쿠팡 상품을 검색해서 1순위 상품의 이미지/가격/상품 URL을 가져온다."""
    if not CP_ACCESS_KEY or not CP_SECRET_KEY:
        raise RuntimeError("CP_ACCESS_KEY / CP_SECRET_KEY 환경변수가 설정되지 않았습니다.")

    query = urlencode({"keyword": keyword, "limit": "1"})
    authorization = _make_authorization("GET", CP_SEARCH_PATH, query, CP_ACCESS_KEY, CP_SECRET_KEY)

    resp = requests.get(
        f"{CP_DOMAIN}{CP_SEARCH_PATH}?{query}",
        headers={"Authorization": authorization},
        timeout=30,
    )
    resp.raise_for_status()
    result = resp.json()
    if result.get("rCode") == "403":
        raise CoupangRateLimitError(result.get("rMessage") or "쿠팡 상품검색 API 호출 한도 초과")
    if result.get("rCode") != "0":
        raise RuntimeError(f"쿠팡 상품검색 실패: {result}")

    products = (result.get("data") or {}).get("productData") or []
    if not products:
        return None

    top = products[0]
    return {
        "image_url": top.get("productImage"),
        "price": top.get("productPrice"),
        "reference_url": top.get("productUrl"),
    }


def create_partners_link_for_url(target_url: str) -> str:
    """이미 알고 있는 쿠팡 상품 URL을 파트너스 추적 링크로 변환한다(24시간 유효)."""
    if not CP_ACCESS_KEY or not CP_SECRET_KEY:
        raise RuntimeError("CP_ACCESS_KEY / CP_SECRET_KEY 환경변수가 설정되지 않았습니다.")

    authorization = _make_authorization("POST", CP_DEEPLINK_PATH, "", CP_ACCESS_KEY, CP_SECRET_KEY)
    resp = requests.post(
        f"{CP_DOMAIN}{CP_DEEPLINK_PATH}",
        headers={"Authorization": authorization, "Content-Type": "application/json"},
        data=json.dumps({"coupangUrls": [target_url]}),
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


def refresh_beverage_products() -> dict:
    """카탈로그의 음료수 상품 중 기준 URL(reference_url)이 아직 없는 것만 상품검색
    API로 채운다. 한 번 채워지면 다시 건드리지 않으므로, 카탈로그가 그대로면 둘째
    날부터는 처리할 항목이 없어 호출이 거의 발생하지 않는다. 그래도 시간당 한도를
    만나면(신규 항목이 한꺼번에 많이 추가된 경우 등) 그 시점에 멈춘다."""
    try:
        catalog = mapping.load_coupang_catalog_xlsx(str(COUPANG_CATALOG_XLSX_PATH))
    except Exception as e:
        print("[BEVERAGE_RANKING] 카탈로그 로드 실패:", e)
        return {"ok": False, "error": str(e)}

    beverage_entries = [
        (barcode, entry) for barcode, entry in catalog.items()
        if entry.category.strip() == BEVERAGE_CATEGORY
    ]

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT item_key FROM beverage_catalog WHERE reference_url IS NOT NULL")
    already_backfilled = {r[0] for r in cur.fetchall()}
    pending_entries = [(k, e) for k, e in beverage_entries if k not in already_backfilled]

    saved = 0
    failed = 0
    rate_limited = False
    for item_key, entry in pending_entries:
        keyword = entry.search_keyword or entry.menu_name or item_key
        try:
            result = search_coupang_product(keyword)
        except CoupangRateLimitError as e:
            print(f"[BEVERAGE_RANKING] API 호출 한도 초과로 이번 실행 중단: {e}")
            rate_limited = True
            break
        except Exception as e:
            print(f"[BEVERAGE_RANKING] {keyword!r} 쿠팡 검색 실패:", e)
            failed += 1
            time.sleep(SEARCH_DELAY_SECONDS)
            continue

        if not result or not result.get("reference_url"):
            print(f"[BEVERAGE_RANKING] {keyword!r} 검색 결과 없음")
            failed += 1
            time.sleep(SEARCH_DELAY_SECONDS)
            continue

        now = datetime.now().isoformat(timespec="seconds")
        cur.execute("""
        INSERT INTO beverage_catalog (item_key, item_name, image_url, price, reference_url, click_count, image_refreshed_at)
        VALUES (?, ?, ?, ?, ?, 0, ?)
        ON CONFLICT(item_key) DO UPDATE SET
            item_name=excluded.item_name,
            image_url=excluded.image_url,
            price=excluded.price,
            reference_url=excluded.reference_url,
            image_refreshed_at=excluded.image_refreshed_at
        """, (item_key, entry.menu_name, result["image_url"], result["price"], result["reference_url"], now))
        saved += 1
        time.sleep(SEARCH_DELAY_SECONDS)

    conn.commit()
    conn.close()
    remaining = len(pending_entries) - saved - failed
    return {
        "ok": True, "count": saved, "failed": failed,
        "rate_limited": rate_limited, "remaining": remaining,
    }


def refresh_beverage_links() -> dict:
    """기준 URL이 있는 음료들의 파트너스 추적 링크만 매일 새로 발급한다(24시간 유효)."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT item_key, reference_url FROM beverage_catalog WHERE reference_url IS NOT NULL")
    rows = cur.fetchall()

    now = datetime.now().isoformat(timespec="seconds")
    saved = 0
    failed = 0
    for item_key, reference_url in rows:
        try:
            link = create_partners_link_for_url(reference_url)
        except Exception as e:
            print(f"[BEVERAGE_RANKING] {item_key} 파트너스 링크 갱신 실패:", e)
            failed += 1
            time.sleep(LINK_DELAY_SECONDS)
            continue

        cur.execute(
            "UPDATE beverage_catalog SET partners_link = ?, link_refreshed_at = ? WHERE item_key = ?",
            (link, now, item_key),
        )
        saved += 1
        time.sleep(LINK_DELAY_SECONDS)

    conn.commit()
    conn.close()
    return {"ok": True, "count": saved, "failed": failed, "refreshed_at": now}


def get_beverage_rankings() -> list[dict]:
    """클릭수 내림차순으로 정렬해서 반환한다(조회 시점마다 다시 정렬되므로 실시간 반영)."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT item_key, item_name, image_url, price, partners_link, click_count, link_refreshed_at
    FROM beverage_catalog
    WHERE partners_link IS NOT NULL
    ORDER BY click_count DESC, item_name ASC
    """)
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "item_key": r[0], "item_name": r[1], "image_url": r[2], "price": r[3],
            "partners_link": r[4], "click_count": r[5], "refreshed_at": r[6],
        }
        for r in rows
    ]


def record_click(item_key: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE beverage_catalog SET click_count = click_count + 1 WHERE item_key = ?", (item_key,))
    updated = cur.rowcount > 0
    conn.commit()
    conn.close()
    return updated
