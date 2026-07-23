# product_ranking.py
"""쿠팡 파트너스 추천 카드 시스템 - 음료 추천/과자 추천 등 여러 상품군에서
공유한다(원래 beverage_ranking.py였던 걸 상품군별로 재사용할 수 있게
일반화했다). 상품군마다 ProductType(전용 테이블/카탈로그 분류명/포장분류
목록)만 다르고 나머지 로직은 전부 동일하다.

카탈로그의 해당 카테고리 상품을 전부 카드로 보여주고, 카드를 클릭한 횟수를
기준으로 정렬한다(조회할 때마다 현재 클릭수로 다시 정렬하므로 실시간 반영).

이미지/가격/구매링크는 쿠팡 상품검색 API(products/search)로 한 번에 가져온다.
파트너스 인증키로 호출하므로 결과의 productUrl 자체가 이미 추적 태그가 붙은
링크라(예: link.coupang.com/re/AFFSDP?lptag=...) 별도 딥링크 변환이 필요 없다
(오히려 이미 변환된 링크를 딥링크 API에 다시 넣으면 "url convert failed"로
실패한다는 걸 실측으로 확인함).

이 API(검색 API)의 공식 한도는 쿠팡 파트너스 고객센터 회신(2026-07-21)
기준 분당 50회다. 그 이전에 코드에 있던 "시간당 10회"는 다른 화면에서
잘못 읽은 수치였고("시간당 90여회" 추정치도 이전에 틀렸었음), 실제로는
분당 기준이라는 걸 공식 메일로 확인했다. 아래 4가지가 쿠팡이 공지한
전체 한도다:
- 검색 API: 분당 50회
- 리포트 API: 시간당 500회 (이 프로젝트는 사용하지 않음)
- 모든 API 합산: 분당 100회
- 파트너스 웹 링크생성 기능: 분당 50회
경고 메시지가 3회 누적되면 계정 자체가 이용제한되고, 경고 후에는 24시간
동안 재사용이 잠긴다. refresh_products의 호출 간격이 0.3초로 매우 짧고
배치 크기 제한이 없어서, 미매칭 상품이 많이 쌓인 날은 분당 50회를 몇 배
초과하는 버스트가 발생했고 이게 실제로 계정 제한을 유발했다(2026-07-20
확인).

그래서 "아직 기준 URL이 없는 상품"에 대해서만 하루 한 번 백필하듯 돌린다
— 카탈로그가 안 바뀌면 둘째 날부터는 처리할 게 없어서 사실상 호출이
0에 수렴한다. 파트너스 링크는 만료되지 않는 고정 링크라 한 번 채워지면
다시 검색하지 않는다(재검색은 이미 맞는 매칭을 엉뚱한 상품으로 잘못
덮어쓸 위험만 있다).

실제 한도(분당 50회)를 절대 넘기지 않도록, search_coupang_product() 호출
전에 DB에 기록된 "최근 1분 이내 호출 수"를 먼저 확인하고 여유가 없으면
아예 API를 부르지 않고 CoupangRateLimitError를 낸다 - 이렇게 하면
refresh_products/snapshot_prices/관리자 수동 새로고침 등 이 함수를 부르는
모든 경로가 각자 따로 조절할 필요 없이 한 곳에서 공통으로 안전하게
제한된다.

검색어가 상품명 그대로라 가끔 엉뚱한 상품이 매칭되는 경우, 사람이 직접 확인한
링크를 set_manual_link()로 반영하면 이후 자동 검색에서 영구 제외된다.

클릭수는 순위 집계용이라 어느 갱신 작업에서도 건드리지 않는다."""
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests

import db_conn
import mapping
import product_match

BASE_DIR = Path(__file__).resolve().parent

CP_ACCESS_KEY = os.getenv("CP_ACCESS_KEY", "")
CP_SECRET_KEY = os.getenv("CP_SECRET_KEY", "")
CP_DOMAIN = "https://api-gateway.coupang.com"
CP_SEARCH_PATH = "/v2/providers/affiliate_open_api/apis/openapi/products/search"
SEARCH_DELAY_SECONDS = 0.3

# 쿠팡 파트너스 검색 API 공식 한도는 분당 50회(2026-07-21 공식 메일 확인) -
# 여유를 두고 분당 35회까지만 스스로 쓰도록 제한한다(안전마진 15회, 약 30%).
# 딥링크 생성(파트너스 웹 링크생성 기능)도 별도로 분당 50회 한도가 있어
# 같은 방식(다른 버킷)으로 보호한다 - main.py의 대표상품 링크 생성이 이
# 버킷을 쓴다.
SEARCH_API_SAFE_LIMIT_PER_MINUTE = 35
DEEPLINK_API_SAFE_LIMIT_PER_MINUTE = 35


class CoupangRateLimitError(RuntimeError):
    pass


def init_search_api_rate_limit_table() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS coupang_search_api_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bucket TEXT NOT NULL DEFAULT 'search',
        called_at TEXT NOT NULL
    )
    """)
    existing_cols = {row[1] for row in cur.execute("PRAGMA table_info(coupang_search_api_calls)").fetchall()}
    if "bucket" not in existing_cols:
        cur.execute("ALTER TABLE coupang_search_api_calls ADD COLUMN bucket TEXT NOT NULL DEFAULT 'search'")
    conn.commit()
    conn.close()


def reserve_coupang_api_slot(bucket: str, limit_per_minute: int) -> bool:
    """쿠팡 파트너스 API(검색/딥링크 등)를 실제로 호출하기 전에 먼저
    부른다. bucket별로 따로 집계해서(검색 API와 딥링크 API는 쿠팡이
    공지한 한도가 서로 별개라 예산을 나눠 쓸 필요가 없다) 최근 1분 이내
    호출 기록이 안전 한도 미만이면 이번 호출을 기록하고 True, 아니면
    API를 아예 부르지 않고 False를 돌려준다."""
    now = datetime.now(timezone.utc)
    window_start_iso = (now - timedelta(minutes=1)).isoformat()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM coupang_search_api_calls WHERE called_at < ?", (window_start_iso,))
    cur.execute("SELECT COUNT(*) FROM coupang_search_api_calls WHERE bucket = ?", (bucket,))
    count = cur.fetchone()[0]
    if count >= limit_per_minute:
        conn.commit()
        conn.close()
        return False
    cur.execute("INSERT INTO coupang_search_api_calls (bucket, called_at) VALUES (?, ?)", (bucket, now.isoformat()))
    conn.commit()
    conn.close()
    return True


def _reserve_search_api_slot() -> bool:
    return reserve_coupang_api_slot("search", SEARCH_API_SAFE_LIMIT_PER_MINUTE)


class ProductType:
    """상품군 하나를 정의한다. table_name은 이 모듈 안에서만 SQL에 직접
    끼워넣으므로(f-string) 반드시 코드에 하드코딩된 값만 써야 한다 - 외부
    입력값을 table_name으로 쓰면 안 된다."""
    def __init__(self, key: str, table_name: str, catalog_category: str,
                 package_types: list[str], default_package_type: str = "미분류"):
        self.key = key
        self.table_name = table_name
        self.catalog_category = catalog_category
        self.package_types = package_types
        self.default_package_type = default_package_type


# 음료 용기 형태 기준 분류.
BEVERAGE = ProductType(
    key="beverage", table_name="beverage_catalog", catalog_category="음료수",
    package_types=["작은캔", "뚱캔", "페트", "병", "팩", "미분류"],
)
# 과자 포장 형태 기준 분류.
SNACK = ProductType(
    key="snack", table_name="snack_catalog", catalog_category="과자",
    package_types=["봉지", "박스", "낱개", "초콜릿", "젤리", "사탕", "시리얼", "미분류"],
)


def get_conn():
    return db_conn.get_conn()


def init_table(pt: ProductType) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"""
    CREATE TABLE IF NOT EXISTS {pt.table_name} (
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
    cur.execute(f"""
    UPDATE {pt.table_name} SET partners_link = reference_url
    WHERE partners_link IS NULL AND reference_url IS NOT NULL
    """)

    existing_cols = {row[1] for row in cur.execute(f"PRAGMA table_info({pt.table_name})").fetchall()}
    if "manual_override" not in existing_cols:
        cur.execute(f"ALTER TABLE {pt.table_name} ADD COLUMN manual_override INTEGER NOT NULL DEFAULT 0")
    if "category" not in existing_cols:
        cur.execute(f"ALTER TABLE {pt.table_name} ADD COLUMN category TEXT NOT NULL DEFAULT '{pt.default_package_type}'")
    if "deleted" not in existing_cols:
        cur.execute(f"ALTER TABLE {pt.table_name} ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
    if "price_checked_at" not in existing_cols:
        cur.execute(f"ALTER TABLE {pt.table_name} ADD COLUMN price_checked_at TEXT")
    if "pending_price" not in existing_cols:
        cur.execute(f"ALTER TABLE {pt.table_name} ADD COLUMN pending_price INTEGER")

    conn.commit()
    conn.close()


def init_price_tracking_tables() -> None:
    """음료/과자 공통으로 쓰는 가격 이력 + 최저가 알림 큐. product_type
    ("beverage"/"snack")으로 상품군을 구분한다 - 카탈로그 테이블처럼 상품군별로
    나누지 않는 이유는 두 테이블 다 조회 패턴이 거의 없고(주로 item_key로만
    조회), 나눠봐야 얻는 이득이 없기 때문."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS price_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_type TEXT NOT NULL,
        item_key TEXT NOT NULL,
        price INTEGER NOT NULL,
        recorded_at TEXT NOT NULL
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_price_history_item ON price_history (product_type, item_key, recorded_at)")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS pending_price_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_type TEXT NOT NULL,
        item_key TEXT NOT NULL,
        item_name TEXT NOT NULL,
        old_low INTEGER,
        new_price INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
    )
    """)
    conn.commit()
    conn.close()


def add_custom_catalog_item(barcode: str, menu_name: str, recommended_price: int | None) -> None:
    """텔레그램 "바코드추가"(관리자 전용, 구분 없이 이름/가격만 받음)용 -
    mapping.catalog_items에 직접 쓴다. 이미 있는 바코드면 구분(is_coupang)
    등 나머지 값은 그대로 두고 이름/가격만 갱신하고, 새 바코드면 미분류(99)로
    저장한다 - 발주 분류에 반영하려면 관리자 웹(/admin/barcode-catalog)에서
    구분을 지정해야 한다."""
    existing = mapping.load_catalog().get(barcode)
    item = mapping.CoupangCatalogItem(
        barcode=barcode,
        menu_name=menu_name,
        search_keyword=existing.search_keyword if existing else "",
        fixed_url=existing.fixed_url if existing else "",
        pack_qty=existing.pack_qty if existing else 1,
        min_order=existing.min_order if existing else 1,
        notes=existing.notes if existing else "",
        is_coupang=existing.is_coupang if existing else 99,
        icecream_box_qty=existing.icecream_box_qty if existing else 0,
        category=existing.category if existing else "",
        menu_code=existing.menu_code if existing else "",
        recommended_price=recommended_price if recommended_price is not None else (existing.recommended_price if existing else 0),
    )
    mapping.upsert_catalog_item(item)


def set_manual_link(
    pt: ProductType, item_key: str, item_name: str, image_url: str,
    price: int | None, reference_url: str, category: str | None = None,
) -> None:
    """사람이 직접 확인한 상품명/분류/이미지/링크를 반영하고, 이후 자동 검색
    갱신에서 영구적으로 제외한다(엉뚱한 상품으로 재매칭되는 걸 막기 위함)."""
    if category not in pt.package_types:
        category = pt.default_package_type
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"""
    INSERT INTO {pt.table_name} (item_key, item_name, image_url, price, reference_url, partners_link, click_count, image_refreshed_at, link_refreshed_at, manual_override, category, deleted)
    VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, 1, ?, 0)
    ON CONFLICT(item_key) DO UPDATE SET
        item_name=excluded.item_name,
        image_url=excluded.image_url,
        price=excluded.price,
        reference_url=excluded.reference_url,
        partners_link=excluded.partners_link,
        image_refreshed_at=excluded.image_refreshed_at,
        link_refreshed_at=excluded.link_refreshed_at,
        manual_override=1,
        category=excluded.category,
        deleted=0
    """, (item_key, item_name, image_url, price, reference_url, reference_url, now, now, category))
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

    if not _reserve_search_api_slot():
        raise CoupangRateLimitError(
            f"검색 API 자체 안전 한도(분당 {SEARCH_API_SAFE_LIMIT_PER_MINUTE}회) 도달 - "
            "실제 쿠팡 한도(분당 50회) 초과를 막기 위해 이번 호출은 건너뜁니다."
        )

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
        "product_name": top.get("productName"),
    }


def refresh_products(pt: ProductType, limit: int | None = None) -> dict:
    """카탈로그의 해당 상품군 중 기준 URL(reference_url)이 아직 없고 사람이
    수동으로 고정(manual_override)하지도 않은 항목만 상품검색 API로 채운다.
    쿠팡 파트너스 링크는 만료되지 않는 고정 링크라 한 번 채워지면(또는 수동
    고정되면) 다시 건드리지 않는다 - 재검색은 이미 맞게 매칭된 상품을 엉뚱한
    상품으로 잘못 덮어쓸 위험만 있고 얻는 이득이 없다. 카탈로그가 그대로면
    둘째 날부터는 처리할 항목이 없어 호출이 거의 발생하지 않는다.

    limit을 주면 미처리 항목 중 앞에서부터 그만큼만 처리한다 - 최초 백필처럼
    미처리 항목이 시간당 한도에 가까울 때, 관리자가 안전한 만큼만 수동으로
    나눠서 돌려볼 수 있게 하기 위함(나머지는 다음 예약 실행 때 이어서 처리됨)."""
    try:
        catalog = mapping.load_catalog()
    except Exception as e:
        print(f"[PRODUCT_RANKING:{pt.key}] 카탈로그 로드 실패:", e)
        return {"ok": False, "error": str(e)}

    entries = {
        barcode: entry for barcode, entry in catalog.items()
        if entry.category.strip() == pt.catalog_category
    }

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"SELECT item_key FROM {pt.table_name} WHERE reference_url IS NOT NULL OR manual_override = 1 OR deleted = 1")
    already_done = {r[0] for r in cur.fetchall()}

    pending_entries = [(k, e) for k, e in entries.items() if k not in already_done]
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
            print(f"[PRODUCT_RANKING:{pt.key}] API 호출 한도 초과로 이번 실행 중단: {e}")
            rate_limited = True
            break
        except Exception as e:
            print(f"[PRODUCT_RANKING:{pt.key}] {keyword!r} 쿠팡 검색 실패:", e)
            failed += 1
            time.sleep(SEARCH_DELAY_SECONDS)
            continue

        if not result or not result.get("reference_url"):
            print(f"[PRODUCT_RANKING:{pt.key}] {keyword!r} 검색 결과 없음")
            failed += 1
            time.sleep(SEARCH_DELAY_SECONDS)
            continue

        now = datetime.now().isoformat(timespec="seconds")
        # 상품검색 API를 파트너스 인증키로 호출하면 결과 productUrl 자체가 이미
        # 추적 태그가 붙은 링크로 나온다 - 별도 딥링크 변환 없이 reference_url을
        # 그대로 partners_link로 써도 이미 수익 추적이 된다.
        cur.execute(f"""
        INSERT INTO {pt.table_name} (item_key, item_name, image_url, price, reference_url, partners_link, click_count, image_refreshed_at, link_refreshed_at)
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


def get_rankings(pt: ProductType) -> list[dict]:
    """클릭수 내림차순으로 정렬해서 반환한다(조회 시점마다 다시 정렬되므로 실시간 반영)."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"""
    SELECT item_key, item_name, image_url, price, partners_link, click_count, link_refreshed_at, category
    FROM {pt.table_name}
    WHERE partners_link IS NOT NULL AND deleted = 0
    ORDER BY click_count DESC, item_name ASC
    """)
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "item_key": r[0], "item_name": r[1], "image_url": r[2], "price": r[3],
            "partners_link": r[4], "click_count": r[5], "refreshed_at": r[6],
            "category": r[7] or pt.default_package_type,
        }
        for r in rows
    ]


def record_click(pt: ProductType, item_key: str) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"UPDATE {pt.table_name} SET click_count = click_count + 1 WHERE item_key = ?", (item_key,))
    updated = cur.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def search_products(keyword: str, limit: int = 5) -> list[dict]:
    """음료/과자 추천 카탈로그 전체(BEVERAGE + SNACK)에서 이름에 keyword가
    부분일치(대소문자 무관)하는, 구매 가능한(partners_link 있는) 상품을 찾는다.
    텔레그램 "구매링크" 명령에서 쓴다 - 발주용 도매처 가격비교(price_compare)와는
    완전히 별개로, 고객용 추천 카드에 등록된 쿠팡 링크만 대상으로 한다."""
    results = []
    for pt in (BEVERAGE, SNACK):
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(f"""
        SELECT item_name, price, partners_link, category, click_count
        FROM {pt.table_name}
        WHERE partners_link IS NOT NULL AND item_name LIKE ? ESCAPE '\\'
        """, (f"%{keyword.replace('%', '\\%').replace('_', '\\_')}%",))
        for r in cur.fetchall():
            results.append({
                "item_name": r[0], "price": r[1], "partners_link": r[2],
                "category": r[3], "click_count": r[4], "product_type": pt.key,
            })
        conn.close()

    results.sort(key=lambda x: x["click_count"], reverse=True)
    return results[:limit]


def search_catalog(query: str, limit: int = 5) -> list[dict]:
    """카탈로그(mapping.load_catalog(), DB 기반)에서 바코드 또는 상품명으로
    찾는다. 텔레그램 "바코드" 명령에서 쓴다 - 도매처 발주용 가격비교
    (price_compare)나 고객용 추천 카드(음료/과자 캐시 테이블)와는 완전히
    별개로, 카탈로그 자체의 바코드/추천판매가를 그대로 조회한다."""
    query = query.strip()
    if not query:
        return []

    try:
        catalog = mapping.load_catalog()
    except Exception as e:
        print("[PRODUCT_RANKING] 카탈로그 로드 실패:", e)
        catalog = {}

    query_lower = query.lower()

    matched = []
    for e in catalog.values():
        if not (
            query in e.barcode
            or query_lower in (e.menu_name or "").lower()
            or query_lower in (e.search_keyword or "").lower()
        ):
            continue
        matched.append({"barcode": e.barcode, "menu_name": e.menu_name, "recommended_price": e.recommended_price})

    # 바코드가 정확히 일치하는 게 있으면 그것만(가장 명확한 케이스, 다른 상품과 안 섞이게)
    exact = [e for e in matched if e["barcode"] == query]
    chosen = exact or matched

    return chosen[:limit]


def delete_product(pt: ProductType, item_key: str) -> bool:
    """추천 목록(고객용 페이지)과 관리 페이지 양쪽에서 영구적으로 제거한다.
    카탈로그(엑셀)에도 있는 상품이면 소프트 삭제(deleted=1)로 표시만 남겨서
    다음 백필 때 자동 검색으로 다시 살아나지 못하게 막는다(예전엔 하드
    삭제라, 카탈로그에 남아있는 상품은 다음 날 백필에서 그대로 재생성됐다).
    카탈로그에 아직 없던 item_key라도 최소 행(tombstone)을 남겨 동일하게 막는다."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"""
    INSERT INTO {pt.table_name} (item_key, deleted)
    VALUES (?, 1)
    ON CONFLICT(item_key) DO UPDATE SET deleted = 1
    """, (item_key,))
    conn.commit()
    conn.close()
    return True


# 재검색 결과가 원래 저장된 상품과 다른 상품일 가능성이 있으면(다른 맛/용량 등)
# 그날 가격은 기록하지 않는다 - product_match.similarity()는 이미 도매처 간
# 크로스 매칭에 쓰던 bigram 기반 유사도(0~1)라, 여기서는 "정말 같은 상품인지"를
# 재확인하는 용도로 좀 더 높은 기준을 쓴다.
PRICE_CHECK_SIMILARITY_THRESHOLD = 0.4

# catalog의 item_name은 "빵부장 말차"처럼 짧은 표시용 이름이라 대부분 용량/개입
# 수량이 안 적혀 있다 - product_match.similarity()의 용량/수량 보너스·페널티가
# 이 비교에서는 사실상 항상 무력화된다는 뜻. 그래서 같은 브랜드/맛인데 낱개와
# 묶음(예: 55g 1개 vs 55g x 16개)처럼 완전히 다른 판매단가를 가진 상품이
# 텍스트 유사도만으로 통과해버려, 가격이 실제로는 안 떨어졌는데 "최저가 감지"로
# 오탐되는 사례가 실측으로 확인됐다(빵부장 말차, 웰치스 제로 오렌지 등).
# 방어책: 지금 저장된 가격 대비 새 가격이 큰 폭으로 변했다면(다른 판매단위일
# 가능성) 텍스트 유사도만으로는 부족하다고 보고 훨씬 높은 기준을 요구한다 -
# 진짜 가격 변동(세일 등)은 이 정도로 유사도가 낮아지지 않는다.
PRICE_CHECK_STRICT_SIMILARITY_THRESHOLD = 0.75
PRICE_CHECK_DEVIATION_LOW_RATIO = 0.5   # 저장가의 50% 밑으로 떨어지면
PRICE_CHECK_DEVIATION_HIGH_RATIO = 2.0  # 저장가의 200% 위로 뛰면

SEARCH_DELAY_SECONDS_PRICE_CHECK = 2.0


def snapshot_prices(pt: ProductType, limit: int = 15) -> dict:
    """이미 매칭된 상품들의 오늘자 가격을 순환 조회해서 price_history에
    쌓는다. reference_url/image_url/partners_link는 절대 건드리지 않는다 -
    가격만 갱신하려고 매번 키워드로 재검색하면 그날그날 검색 1순위가 바뀌어
    엉뚱한 상품의 가격으로 기록될 위험이 있어서, 재검색 결과 상품명을 저장된
    이름과 비교해 유사도가 낮으면(다른 상품으로 의심) 그날 가격 기록만
    건너뛴다(카드 자체는 그대로 유지).

    price_checked_at 기준 오래된 순으로 limit개씩만 처리하므로, 여러 번에
    걸쳐 나눠 부르면(스케줄러가 짧은 간격으로 반복 호출) 결국 전체 카탈로그를
    한 바퀴 돈다 - 시간당 호출 한도를 넘지 않게 batch 크기/간격을 호출하는
    쪽(main.py 스케줄러)에서 조절한다.

    반환하는 new_lows: 이번 배치에서 역대 최저가를 갱신한 상품 목록
    (pending_price_alerts에도 같이 기록됨)."""
    try:
        catalog = mapping.load_catalog()
    except Exception as e:
        print(f"[PRODUCT_RANKING:{pt.key}] 카탈로그 로드 실패:", e)
        return {"ok": False, "error": str(e)}

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"""
    SELECT item_key, item_name, price, pending_price FROM {pt.table_name}
    WHERE reference_url IS NOT NULL AND deleted = 0
    ORDER BY price_checked_at ASC NULLS FIRST
    LIMIT ?
    """, (limit,))
    targets = cur.fetchall()

    checked = 0
    recorded = 0
    rate_limited = False
    new_lows = []
    now = datetime.now().isoformat(timespec="seconds")

    for item_key, stored_name, stored_price, pending_price in targets:
        entry = catalog.get(item_key)
        keyword = (entry.search_keyword or entry.menu_name) if entry else stored_name
        checked += 1

        try:
            result = search_coupang_product(keyword)
        except CoupangRateLimitError as e:
            print(f"[PRODUCT_RANKING:{pt.key}] 가격 조회 중 API 한도 초과, 이번 배치 중단: {e}")
            rate_limited = True
            break
        except Exception as e:
            print(f"[PRODUCT_RANKING:{pt.key}] {keyword!r} 가격 조회 실패:", e)
            cur.execute(f"UPDATE {pt.table_name} SET price_checked_at = ? WHERE item_key = ?", (now, item_key))
            conn.commit()
            time.sleep(SEARCH_DELAY_SECONDS_PRICE_CHECK)
            continue

        # 실패/스킵이어도 순환 커서는 앞으로 보낸다 - 안 그러면 매번 같은
        # 항목에서 계속 걸려서 뒤쪽 항목들이 영영 갱신 안 됨.
        cur.execute(f"UPDATE {pt.table_name} SET price_checked_at = ? WHERE item_key = ?", (now, item_key))

        if not result or not result.get("price"):
            conn.commit()
            time.sleep(SEARCH_DELAY_SECONDS_PRICE_CHECK)
            continue

        found_name = result.get("product_name") or ""
        new_price = result["price"]
        sim = product_match.similarity(stored_name or "", found_name)

        threshold = PRICE_CHECK_SIMILARITY_THRESHOLD
        is_deviant = False
        if stored_price and new_price:
            ratio = new_price / stored_price
            is_deviant = ratio < PRICE_CHECK_DEVIATION_LOW_RATIO or ratio > PRICE_CHECK_DEVIATION_HIGH_RATIO
            if is_deviant:
                threshold = PRICE_CHECK_STRICT_SIMILARITY_THRESHOLD

        if sim < threshold:
            print(f"[PRODUCT_RANKING:{pt.key}] {item_key!r} 재검색 결과가 다른 상품/판매단위로 의심됨"
                  f"(저장된 이름={stored_name!r}, 저장가={stored_price}, 검색결과={found_name!r}, "
                  f"검색가={new_price}, 유사도={sim:.2f}, 기준={threshold}) - 가격 기록 건너뜀")
            conn.commit()
            time.sleep(SEARCH_DELAY_SECONDS_PRICE_CHECK)
            continue

        # 상품명 유사도만으로는 "같은 브랜드/맛인데 낱개/묶음처럼 판매단위가
        # 다른 상품"을 걸러내지 못하는 사례가 실측으로 확인됐다(짧은 검색결과
        # 이름이 우연히 저장된 이름과 완전히 같아 유사도가 1.0으로 나오는 경우
        # 등). 그래서 가격이 크게 벌어졌을 때는 유사도가 아무리 높아도 한 번에
        # 확정하지 않고, 바로 다음 스캔 주기에서 같은 가격이 다시 확인되어야만
        # (2회 연속 확인) 실제 가격 변동으로 인정한다 - 잘못된 검색결과가 우연히
        # 두 번 연속 뜨는 경우는 거의 없기 때문에 오탐을 사실상 걸러내면서도,
        # 진짜 가격 변동(세일 등)은 한 주기(약 30분) 늦게라도 정상 반영된다.
        if is_deviant:
            if pending_price != new_price:
                cur.execute(f"UPDATE {pt.table_name} SET pending_price = ? WHERE item_key = ?", (new_price, item_key))
                print(f"[PRODUCT_RANKING:{pt.key}] {item_key!r} 가격 급변 1차 감지({stored_price} -> {new_price}) "
                      f"- 다음 확인에서 같은 값이 또 나오면 확정")
                conn.commit()
                time.sleep(SEARCH_DELAY_SECONDS_PRICE_CHECK)
                continue
            cur.execute(f"UPDATE {pt.table_name} SET pending_price = NULL WHERE item_key = ?", (item_key,))
        elif pending_price is not None:
            cur.execute(f"UPDATE {pt.table_name} SET pending_price = NULL WHERE item_key = ?", (item_key,))

        cur.execute(
            "SELECT MIN(price) FROM price_history WHERE product_type = ? AND item_key = ?",
            (pt.key, item_key),
        )
        prior_low = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO price_history (product_type, item_key, price, recorded_at) VALUES (?, ?, ?, ?)",
            (pt.key, item_key, new_price, now),
        )
        cur.execute(f"UPDATE {pt.table_name} SET price = ? WHERE item_key = ?", (new_price, item_key))
        recorded += 1

        if prior_low is not None and new_price < prior_low:
            cur.execute("""
            INSERT INTO pending_price_alerts (product_type, item_key, item_name, old_low, new_price, created_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
            """, (pt.key, item_key, stored_name, prior_low, new_price, now))
            new_lows.append({"item_key": item_key, "item_name": stored_name, "old_low": prior_low, "new_price": new_price})

        conn.commit()
        time.sleep(SEARCH_DELAY_SECONDS_PRICE_CHECK)

    conn.close()
    return {
        "ok": True, "checked": checked, "recorded": recorded,
        "rate_limited": rate_limited, "new_lows": new_lows,
    }


def get_price_history(pt: ProductType, item_key: str) -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT price, recorded_at FROM price_history
    WHERE product_type = ? AND item_key = ?
    ORDER BY recorded_at ASC
    """, (pt.key, item_key))
    rows = cur.fetchall()
    conn.close()
    return [{"price": r[0], "recorded_at": r[1]} for r in rows]


def claim_notifiable_alerts() -> list[dict]:
    """아직 대표님께 알리지 않은(status='pending') 최저가 알림을 전부 원자적으로
    'notified' 상태로 바꾸면서 동시에 가져온다. 음료/과자 가격 스캔 작업이 같은
    30분 간격이라 사실상 동시에 도는데, 예전에는 "조회 후 별도로 상태 변경"
    2단계였어서 두 작업이 거의 동시에 조회하면 같은 pending 알림을 둘 다 보고
    중복으로 발송하는 사고가 있었다(실측 확인). SELECT 따로 안 하고
    UPDATE...RETURNING 한 번으로 처리하면(cart_jobs.py의 작업 큐 claim과 같은
    패턴) FOR UPDATE SKIP LOCKED 덕분에 동시에 두 트랜잭션이 돌아도 같은 행을
    두 번 가져갈 수 없다."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    UPDATE pending_price_alerts SET status = 'notified'
    WHERE id IN (
        SELECT id FROM pending_price_alerts WHERE status = 'pending' ORDER BY id ASC FOR UPDATE SKIP LOCKED
    )
    RETURNING id, product_type, item_key, item_name, old_low, new_price
    """)
    rows = cur.fetchall()
    conn.commit()
    conn.close()
    return [
        {"id": r[0], "product_type": r[1], "item_key": r[2], "item_name": r[3], "old_low": r[4], "new_price": r[5]}
        for r in rows
    ]


def resolve_pending_alerts(status: str) -> list[dict]:
    """대표님이 텔레그램에서 "전체발송"/"생략"으로 응답했을 때, 알림 보냈던
    (status='notified') 건들을 전부 확정 상태로 바꾸고 그 목록을 돌려준다
    (방송 메시지 구성용). 가맹점이 "구매링크 (상품명)"을 따로 안 쳐도 되도록
    구매 링크도 같이 실어서 돌려준다."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT a.id, a.product_type, a.item_key, a.item_name, a.old_low, a.new_price,
           COALESCE(b.partners_link, s.partners_link) AS partners_link
    FROM pending_price_alerts a
    LEFT JOIN beverage_catalog b ON a.product_type = 'beverage' AND a.item_key = b.item_key
    LEFT JOIN snack_catalog s ON a.product_type = 'snack' AND a.item_key = s.item_key
    WHERE a.status = 'notified'
    ORDER BY a.id ASC
    """)
    rows = cur.fetchall()
    if rows:
        ids = [r[0] for r in rows]
        placeholders = ",".join("?" * len(ids))
        cur.execute(f"UPDATE pending_price_alerts SET status = ? WHERE id IN ({placeholders})", [status, *ids])
        conn.commit()
    conn.close()
    return [
        {
            "id": r[0], "product_type": r[1], "item_key": r[2], "item_name": r[3],
            "old_low": r[4], "new_price": r[5], "partners_link": r[6],
        }
        for r in rows
    ]


def resolve_pending_alerts_by_ids(ids: list[int], status: str) -> list[dict]:
    """대표님이 "15 발송"/"15 생략"처럼 특정 알림 번호만 골라 응답했을 때,
    그 번호에 해당하는(status='notified') 건들만 확정 상태로 바꾸고 그
    목록을 돌려준다. 지정하지 않은 나머지 notified 건들은 그대로 남아
    다음 응답을 기다린다 - resolve_pending_alerts()와 달리 전부가 아니라
    골라낸 것만 처리한다."""
    if not ids:
        return []
    conn = get_conn()
    cur = conn.cursor()
    placeholders = ",".join("?" * len(ids))
    cur.execute(f"""
    SELECT a.id, a.product_type, a.item_key, a.item_name, a.old_low, a.new_price,
           COALESCE(b.partners_link, s.partners_link) AS partners_link
    FROM pending_price_alerts a
    LEFT JOIN beverage_catalog b ON a.product_type = 'beverage' AND a.item_key = b.item_key
    LEFT JOIN snack_catalog s ON a.product_type = 'snack' AND a.item_key = s.item_key
    WHERE a.status = 'notified' AND a.id IN ({placeholders})
    ORDER BY a.id ASC
    """, ids)
    rows = cur.fetchall()
    if rows:
        found_ids = [r[0] for r in rows]
        fp = ",".join("?" * len(found_ids))
        cur.execute(f"UPDATE pending_price_alerts SET status = ? WHERE id IN ({fp})", [status, *found_ids])
        conn.commit()
    conn.close()
    return [
        {
            "id": r[0], "product_type": r[1], "item_key": r[2], "item_name": r[3],
            "old_low": r[4], "new_price": r[5], "partners_link": r[6],
        }
        for r in rows
    ]
