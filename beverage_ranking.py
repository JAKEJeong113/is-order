# beverage_ranking.py
"""음료 추천 카드 목록 + 쿠팡 파트너스 링크/이미지 캐시.
카탈로그의 '음료수' 카테고리 상품을 전부 카드로 보여주고, 카드를 클릭한 횟수를
기준으로 정렬한다(조회할 때마다 현재 클릭수로 다시 정렬하므로 실시간 반영).

이미지/가격/구매링크는 쿠팡 상품검색 API(products/search)로 한 번에 가져온다.
파트너스 인증키로 호출하므로 결과의 productUrl 자체가 이미 추적 태그가 붙은
링크라(예: link.coupang.com/re/AFFSDP?lptag=...) 별도 딥링크 변환이 필요 없다
(오히려 이미 변환된 링크를 딥링크 API에 다시 넣으면 "url convert failed"로
실패한다는 걸 실측으로 확인함).

이 API는 시간당 호출 한도가 엄격하고(실측 시간당 약 90여회) 초과하면 최대
24시간 잠기며 3회 누적되면 계정 자체가 제한된다. 그래서 "아직 기준 URL이 없는
상품"에 대해서만 하루 한 번 백필하듯 돌린다 — 카탈로그가 안 바뀌면 둘째 날부터는
처리할 게 없어서 사실상 호출이 0에 수렴한다. 파트너스 링크는 만료되지 않는
고정 링크라 한 번 채워지면 다시 검색하지 않는다(재검색은 이미 맞는 매칭을
엉뚱한 상품으로 잘못 덮어쓸 위험만 있다).

검색어가 상품명 그대로라 가끔 엉뚱한 상품이 매칭되는 경우, 사람이 직접 확인한
링크를 set_manual_beverage_link()로 반영하면 이후 자동 검색에서 영구 제외된다.

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

BEVERAGE_CATEGORY = "음료수"
SEARCH_DELAY_SECONDS = 0.3


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
    # 딥링크 변환이 항상 실패하는 걸 모르고 먼저 백필된 항목들(reference_url은
    # 있지만 partners_link가 비어있는 상태로 남은 것)을 자가 복구한다.
    # reference_url 자체가 이미 추적 태그 붙은 링크라 그대로 써도 된다.
    cur.execute("""
    UPDATE beverage_catalog SET partners_link = reference_url
    WHERE partners_link IS NULL AND reference_url IS NOT NULL
    """)

    # 검색 키워드가 상품명 그대로라 엉뚱한 상품이 매칭되는 경우가 있어(예: 이름은
    # 비슷한데 다른 상품 이미지가 붙음), 사람이 직접 검증해서 넣은 링크는 이후
    # 자동 갱신이 절대 건드리지 않도록 표시해두는 플래그.
    existing_cols = {row[1] for row in cur.execute("PRAGMA table_info(beverage_catalog)").fetchall()}
    if "manual_override" not in existing_cols:
        cur.execute("ALTER TABLE beverage_catalog ADD COLUMN manual_override INTEGER NOT NULL DEFAULT 0")

    conn.commit()
    conn.close()


def set_manual_beverage_link(item_key: str, item_name: str, image_url: str, price: int | None, reference_url: str) -> None:
    """사람이 직접 확인한 상품 링크/이미지를 반영하고, 이후 자동 검색 갱신에서
    영구적으로 제외한다(엉뚱한 상품으로 재매칭되는 걸 막기 위함)."""
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO beverage_catalog (item_key, item_name, image_url, price, reference_url, partners_link, click_count, image_refreshed_at, link_refreshed_at, manual_override)
    VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, 1)
    ON CONFLICT(item_key) DO UPDATE SET
        item_name=excluded.item_name,
        image_url=excluded.image_url,
        price=excluded.price,
        reference_url=excluded.reference_url,
        partners_link=excluded.partners_link,
        image_refreshed_at=excluded.image_refreshed_at,
        link_refreshed_at=excluded.link_refreshed_at,
        manual_override=1
    """, (item_key, item_name, image_url, price, reference_url, reference_url, now, now))
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


def refresh_beverage_products(limit: int | None = None) -> dict:
    """카탈로그의 음료수 상품 중 기준 URL(reference_url)이 아직 없고 사람이
    수동으로 고정(manual_override)하지도 않은 항목만 상품검색 API로 채운다.
    쿠팡 파트너스 링크는 만료되지 않는 고정 링크라 한 번 채워지면(또는 수동
    고정되면) 다시 건드리지 않는다 - 재검색은 이미 맞게 매칭된 상품을 엉뚱한
    상품으로 잘못 덮어쓸 위험만 있고 얻는 이득이 없다. 카탈로그가 그대로면
    둘째 날부터는 처리할 항목이 없어 호출이 거의 발생하지 않는다.

    limit을 주면 미처리 항목 중 앞에서부터 그만큼만 처리한다 - 최초 백필처럼
    미처리 항목이 시간당 한도에 가까울 때, 관리자가 안전한 만큼만 수동으로
    나눠서 돌려볼 수 있게 하기 위함(나머지는 다음 예약 실행 때 이어서 처리됨)."""
    try:
        catalog = mapping.load_coupang_catalog_xlsx(str(COUPANG_CATALOG_XLSX_PATH))
    except Exception as e:
        print("[BEVERAGE_RANKING] 카탈로그 로드 실패:", e)
        return {"ok": False, "error": str(e)}

    beverage_entries = {
        barcode: entry for barcode, entry in catalog.items()
        if entry.category.strip() == BEVERAGE_CATEGORY
    }

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT item_key FROM beverage_catalog WHERE reference_url IS NOT NULL OR manual_override = 1")
    already_done = {r[0] for r in cur.fetchall()}

    pending_entries = [(k, e) for k, e in beverage_entries.items() if k not in already_done]
    total_pending = len(pending_entries)
    if limit is not None:
        pending_entries = pending_entries[:limit]

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
        # 상품검색 API를 파트너스 인증키로 호출하면 결과 productUrl 자체가 이미
        # 추적 태그가 붙은 링크로 나온다(예: link.coupang.com/re/AFFSDP?lptag=...) -
        # 이 URL을 딥링크 변환 API에 다시 넣으면 "이미 변환된 링크"라 실패한다
        # (실측: rCode 400 "url convert failed"). 그래서 별도 딥링크 변환 없이
        # reference_url을 그대로 partners_link로 써도 이미 수익 추적이 된다.
        cur.execute("""
        INSERT INTO beverage_catalog (item_key, item_name, image_url, price, reference_url, partners_link, click_count, image_refreshed_at, link_refreshed_at)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
        ON CONFLICT(item_key) DO UPDATE SET
            item_name=excluded.item_name,
            image_url=excluded.image_url,
            price=excluded.price,
            reference_url=excluded.reference_url,
            partners_link=excluded.partners_link,
            image_refreshed_at=excluded.image_refreshed_at,
            link_refreshed_at=excluded.link_refreshed_at
        """, (item_key, entry.menu_name, result["image_url"], result["price"], result["reference_url"], result["reference_url"], now, now))
        conn.commit()
        saved += 1
        time.sleep(SEARCH_DELAY_SECONDS)

    conn.close()
    remaining = total_pending - saved - failed
    return {
        "ok": True, "count": saved, "failed": failed,
        "rate_limited": rate_limited, "remaining": remaining,
    }


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
