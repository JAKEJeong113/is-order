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
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests

import db_conn
import mapping
import popularity
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
    if "pending_count" not in existing_cols:
        cur.execute(f"ALTER TABLE {pt.table_name} ADD COLUMN pending_count INTEGER NOT NULL DEFAULT 0")
    if "coupang_product_id" not in existing_cols:
        # 가격 재확인 시 이름 유사도 대신 쿠팡 상품 고유 ID로 "진짜 같은
        # 상품인지" 정확히 대조하기 위한 값(snapshot_prices 참고). 기존
        # 행은 다음 가격 확인 때 자연히 채워진다(마이그레이션 백필 불필요).
        cur.execute(f"ALTER TABLE {pt.table_name} ADD COLUMN coupang_product_id TEXT")

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
    existing_history_cols = {row[1] for row in cur.execute("PRAGMA table_info(price_history)").fetchall()}
    if "pack_qty" not in existing_history_cols:
        # 상품명에서 파싱한 묶음 수량(예: "20개입" -> 20) - 개당 매입가를
        # 나중에도(포장이 바뀌기 전 이력까지) 정확히 재계산할 수 있게 매
        # 기록 시점의 값을 같이 남긴다. 파싱 안 되는 단품은 NULL(=1개로 간주).
        cur.execute("ALTER TABLE price_history ADD COLUMN pack_qty INTEGER")

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
    existing_alert_cols = {row[1] for row in cur.execute("PRAGMA table_info(pending_price_alerts)").fetchall()}
    if "match_method" not in existing_alert_cols:
        # 'product_id'(쿠팡 상품 고유 ID로 정확히 대조된 확정 매칭) vs
        # 'similarity'(이름 유사도 + 다중 확인 통과) - 나중에 핫딜 위젯이
        # "대표님 승인 없이 즉시 노출해도 되는 알림"을 구분하는 데 쓴다.
        cur.execute("ALTER TABLE pending_price_alerts ADD COLUMN match_method TEXT NOT NULL DEFAULT 'similarity'")

    # 마진 경고 중 "상품명에서 묶음 수량을 못 읽은" 경우 전용 연속확인
    # 카운터 - _check_margin 참고. 이름에 수량이 없으면 검색이 그날 우연히
    # 수량 미표기 상품(다른 판매단위)을 매칭해온 것인지 실제 문제인지
    # 구분할 수 없어서, 여러 스캔 주기에 걸쳐 계속 같은 문제가 보여야만
    # 경고한다.
    cur.execute("""
    CREATE TABLE IF NOT EXISTS margin_warning_streaks (
        barcode TEXT PRIMARY KEY,
        streak INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
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


def _parse_coupang_product(raw: dict) -> dict:
    return {
        "product_id": raw.get("productId"),
        "image_url": raw.get("productImage"),
        "price": raw.get("productPrice"),
        "reference_url": raw.get("productUrl"),
        "product_name": raw.get("productName"),
        "is_rocket": bool(raw.get("isRocket", False)),
    }


def _fetch_coupang_products(keyword: str, limit: int = 1) -> list[dict]:
    """검색어로 쿠팡 상품을 검색해서 상위 limit개를 그대로 돌려준다(빈 결과면
    빈 리스트). 응답의 productId는 쿠팡이 매기는 상품 고유 식별자라(실측
    확인: 같은 검색이라도 재검색 때마다 순위가 바뀔 수 있는 productName과
    달리, 같은 상품이면 항상 같은 값) - 이름 유사도보다 훨씬 확실하게 "같은
    상품인지"를 판단하는 데 쓴다(snapshot_prices 참고)."""
    if not CP_ACCESS_KEY or not CP_SECRET_KEY:
        raise RuntimeError("CP_ACCESS_KEY / CP_SECRET_KEY 환경변수가 설정되지 않았습니다.")

    if not _reserve_search_api_slot():
        raise CoupangRateLimitError(
            f"검색 API 자체 안전 한도(분당 {SEARCH_API_SAFE_LIMIT_PER_MINUTE}회) 도달 - "
            "실제 쿠팡 한도(분당 50회) 초과를 막기 위해 이번 호출은 건너뜁니다."
        )

    query = urlencode({"keyword": keyword, "limit": str(limit)})
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
    # 일반판매자(마켓플레이스) 상품은 로켓상품(로켓배송/판매자로켓 등 "로켓"
    # 배지가 붙는 상품)보다 품절/가격 변동/판매자 교체가 훨씬 잦고 신뢰도가
    # 떨어진다(사용자 요청) - 검색 결과 단계에서부터 로켓상품이 아닌 건
    # 아예 후보로 보지 않는다. API 응답에 매 상품마다 isRocket이 실려온다
    # (실측 확인).
    return [p for p in (_parse_coupang_product(p) for p in products) if p["is_rocket"]]



# 쿠팡 상품 하나에 구매 수량 옵션이 여러 개 있는 경우가 흔하다(예: 야채타임
# 6개/10개/16개/20개, 새우깡 20개 등 - 사용자 확인). 검색 API가 돌려주는
# "기본" 가격은 이 중 어느 옵션인지 보장이 없어서, 무인매장이 실제로 사입할
# 만한 대량 옵션과 동떨어진(소용량) 값이 기본으로 잡히는 문제가 있었다.
# 그래서 검색 결과 후보 중 상품명에 "10개 이상"으로 읽히는 옵션이 있으면
# 그걸 우선 쓰고, 여러 개면 그중 가장 작은 걸(불필요하게 큰 박스로 튀지
# 않게) 고른다. 후보가 전부 10개 미만/수량 미표기면 기존처럼 1순위를 쓴다.
COUPANG_PREFERRED_MIN_PACK_QTY = 10


def _pick_preferred_candidate(candidates: list[dict], reference_name: str) -> dict | None:
    """실측으로 확인된 함정: "야채타임"/"새우깡" 같은 검색어에 "인기 과자
    혼합 10종 세트(...), 10개, 봉지과자"처럼 완전히 다른 상품 여러 개를
    묶은 "N종 세트" 상품이 섞여 나올 수 있는데, 이 "10개"는 "이 상품
    10개입"이 아니라 "10가지 다른 과자를 묶었다"는 뜻이라 수량 우선
    로직이 이런 걸 집으면 안 된다(실측: 두 검색어 모두에서 상위 후보로
    나왔고, 대상 상품명과의 유사도가 0.00이었음 - 세트 상품명 자체에
    검색어가 아예 안 들어있는 경우가 많아 유사도로 걸러진다). 그래서
    수량으로 비교하기 전에 먼저 검색어/기존 상품명과 이름이 어느 정도
    닮은 후보로만 추려서(product_match.MIN_GROUP_SCORE 이상), 그 안에서만
    "10개 이상" 옵션을 찾는다. 닮은 후보가 하나도 없으면(방어적으로) 원래
    후보군 전체에서, 그것도 없으면 1순위를 그대로 쓴다."""
    if not candidates:
        return None
    similar = [
        c for c in candidates
        if product_match.similarity(reference_name, c.get("product_name") or "") >= product_match.MIN_GROUP_SCORE
    ]
    pool = similar or candidates
    qualifying = [
        c for c in pool
        if (_extract_coupang_pack_qty(c.get("product_name") or "") or 0) >= COUPANG_PREFERRED_MIN_PACK_QTY
    ]
    if qualifying:
        return min(qualifying, key=lambda c: _extract_coupang_pack_qty(c["product_name"]))
    return pool[0]


def search_coupang_product(keyword: str) -> dict | None:
    """검색어로 쿠팡 상품을 검색해서 로켓상품 중 가장 적합한 것(검색어와
    이름이 닮은 후보 중 10개 이상 옵션 우선, 없으면 1순위)의 이미지/가격/
    상품 URL을 가져온다. limit을 1이 아니라 여유 있게 주는 이유는, 로켓상품이
    아닌 결과를 걸러내고 수량 옵션까지 비교한 뒤에도 실제로 쓸 수 있는
    후보가 남게 하기 위함이다."""
    products = _fetch_coupang_products(keyword, limit=PRICE_CHECK_ID_MATCH_CANDIDATES)
    return _pick_preferred_candidate(products, keyword)


# snapshot_prices가 productId 대조에 쓸 후보 개수. 너무 크면 API 응답이
# 무거워지고, 너무 작으면 실제로 같은 상품인데 검색 순위가 밀려나 있을 때
# 못 찾아 매번 폴백(이름 유사도)으로 떨어진다 - 적당히 여유를 둔다.
PRICE_CHECK_ID_MATCH_CANDIDATES = 10


def search_coupang_products_for_price_check(keyword: str) -> list[dict]:
    """snapshot_prices 전용 - ID 대조용 후보와 기존 이름 유사도 폴백에 쓸
    1순위 결과를 한 번의 API 호출로 같이 얻는다."""
    return _fetch_coupang_products(keyword, limit=PRICE_CHECK_ID_MATCH_CANDIDATES)


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
        INSERT INTO {pt.table_name} (item_key, item_name, image_url, price, reference_url, partners_link, click_count, image_refreshed_at, link_refreshed_at, coupang_product_id)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
        ON CONFLICT(item_key) DO UPDATE SET
            item_name=excluded.item_name,
            image_url=excluded.image_url,
            price=excluded.price,
            reference_url=excluded.reference_url,
            partners_link=excluded.partners_link,
            image_refreshed_at=excluded.image_refreshed_at,
            link_refreshed_at=excluded.link_refreshed_at,
            coupang_product_id=excluded.coupang_product_id
        """, (
            item_key, entry.menu_name, result["image_url"], result["price"], result["reference_url"],
            result["reference_url"], now, now,
            str(result["product_id"]) if result.get("product_id") is not None else None,
        ))
        conn.commit()
        saved += 1
        time.sleep(SEARCH_DELAY_SECONDS)

    conn.close()
    remaining = total_pending - saved - failed
    return {
        "ok": True, "count": saved, "failed": failed,
        "rate_limited": rate_limited, "remaining": remaining,
    }


# 인기 카드 정렬에 쓸 실제 판매량 집계 기간. popularity.get_top_items()가
# 홈 화면 "인기상품 TOP30"에 쓰는 기본 기간(60일)과 맞춰서, 같은 상품이
# 화면마다 다른 기준으로 다르게 줄서는 걸 막는다.
RANKING_POPULARITY_WINDOW_DAYS = 60


def get_rankings(pt: ProductType) -> list[dict]:
    """실제 판매량(전 가맹점 발주 집계 - popularity.order_events) 많은 순으로
    정렬한다. 예전에는 카드 클릭수(click_count) 기준이었는데, 클릭은 실제
    구매로 이어지는지 알 수 없는 약한 지표라 실제 발주에 담긴 수량 기준으로
    바꿨다. 쿠팡으로 판매되는 상품은 음료/과자 구분 없이 전부
    category='coupang'으로 로그되므로, 이 상품군 카탈로그의 item_key(바코드)
    집합으로 걸러서 합산한다. 아직 판매 이력이 없는 신상품은 판매량이 0이라
    맨 뒤로 밀리는데, 그 안에서는 click_count로 대신 정렬해 완전히 무작위로
    안 보이게 한다."""
    qty_map = popularity.get_qty_map("coupang", days=RANKING_POPULARITY_WINDOW_DAYS)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"""
    SELECT item_key, item_name, image_url, price, partners_link, click_count, link_refreshed_at, category
    FROM {pt.table_name}
    WHERE partners_link IS NOT NULL AND deleted = 0
    """)
    rows = cur.fetchall()
    conn.close()

    items = [
        {
            "item_key": r[0], "item_name": r[1], "image_url": r[2], "price": r[3],
            "partners_link": r[4], "click_count": r[5], "refreshed_at": r[6],
            "category": r[7] or pt.default_package_type,
            "total_qty": qty_map.get(r[0], 0),
        }
        for r in rows
    ]
    items.sort(key=lambda it: (-it["total_qty"], -it["click_count"], it["item_name"] or ""))
    return items


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
#
# (2026-07-29: 실제 알림 108건을 전수 확인한 결과, 50% 이상 떨어진 경우만
# "의심"으로 봤던 기존 기준을 그대로 통과한 뒤 2회 연속 확인까지 거쳐 "확정"된
# 상태로 실제 발송된 케이스가 여럿 나왔다 - 예: 빼빼로 화이트쿠키 45,400원 ->
# 8,650원(81% 하락), 오징어땅콩 27,340원 -> 4,640원(83% 하락). 검색 결과가
# 우연히 두 번 연속 같은 값을 주는 게 아니라, 검색어가 다른 판매단위/상품으로
# 안정적으로 재매칭된 것으로 보인다 - 즉 "연속 확인"만으로는 이런 종류의
# 오탐을 못 걸러낸다. 그래서 ①의심 기준 자체를 50%->20% 하락으로 낮추고
# (조청유과 46% 하락 건은 기존 기준을 아예 안 걸렸었음) ②유사도 기준을
# 올리고 ③60% 이상 떨어지는 극단적인 경우는 연속 확인 횟수를 2회->3회로
# 늘려 안정적인 오탐도 더 걸러낸다.)
PRICE_CHECK_STRICT_SIMILARITY_THRESHOLD = 0.85
PRICE_CHECK_DEVIATION_LOW_RATIO = 0.8    # 저장가의 80% 밑으로 떨어지면(20%+ 하락) 의심
PRICE_CHECK_DEVIATION_HIGH_RATIO = 2.0   # 저장가의 200% 위로 뛰면
PRICE_CHECK_EXTREME_DEVIATION_RATIO = 0.4  # 저장가의 40% 밑으로 떨어지면(60%+ 하락) 더 엄격하게
PRICE_CHECK_CONFIRMATIONS_REQUIRED = 2
PRICE_CHECK_EXTREME_CONFIRMATIONS_REQUIRED = 3

SEARCH_DELAY_SECONDS_PRICE_CHECK = 2.0

# 개당 매입가 계산용 - 쿠팡 상품명은 도매몰과 묶음수량 표기 관습이 다르다
# (실측 확인: 도매몰은 "20개입"처럼 반드시 "입"이 붙지만, 쿠팡은 "피크닉
# 사과맛, 200ml, 24개"/"갈아만든배, 340ml, 24개"처럼 "입" 없이 그냥
# "24개"만 붙는 경우가 흔함). product_match.py의 도매몰용 정규식
# (UNIT_QTY_RE, "개입"/"x개"만 인식)로는 이런 쿠팡 이름에서 수량을 아예
# 못 읽어서, 24개들이 묶음 가격을 그대로 낱개 가격으로 오인해 역마진으로
# 잘못 경고하는 사고가 있었다. "입" 유무와 무관하게 "숫자+개"를 잡되,
# 단어 경계(\b)로 "2개월"처럼 수량이 아닌 숫자+개 조합(한글 음절도
# 정규식상 단어문자라 "개"와 "월" 사이엔 경계가 없어 걸러짐)은 제외한다.
_COUPANG_PACK_QTY_RE = re.compile(r"(\d+)\s*개(?:입)?\b")


def _extract_coupang_pack_qty(text: str) -> int | None:
    m = _COUPANG_PACK_QTY_RE.search(text or "")
    return int(m.group(1)) if m else None


# 상품명에서 묶음 수량을 못 읽었을 때(pack_qty=None) 마진 경고를 몇 번
# 연속으로 봐야 실제로 알릴지. 이 경우 방금 찾은 가격이 진짜 개당가인지
# (검색이 우연히 수량 미표기의 다른 판매단위 상품을 골라온 건 아닌지)
# 확신할 수 없어서, 한 번만 보고 바로 알리지 않는다(실측 사례: "피크닉
# 사과"로 검색했더니 수량 표기가 아예 없는 "피크닉 사과 주스" 22,890원이
# 매칭되어 역마진으로 오탐한 적이 있었음 - 검색 결과가 매번 똑같지
# 않으므로 여러 스캔 주기에 걸쳐 계속 재현돼야 진짜 문제로 본다).
MARGIN_WARNING_UNPARSED_CONFIRMATIONS_REQUIRED = 2


def _bump_margin_streak(barcode: str) -> int:
    """이 바코드의 "수량 미확인 마진 경고" 연속 횟수를 1 늘리고 그 값을
    돌려준다."""
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO margin_warning_streaks (barcode, streak, updated_at)
    VALUES (?, 1, ?)
    ON CONFLICT(barcode) DO UPDATE SET streak = margin_warning_streaks.streak + 1, updated_at = excluded.updated_at
    RETURNING streak
    """, (barcode, now))
    streak = cur.fetchone()[0]
    conn.commit()
    conn.close()
    return streak


def _reset_margin_streak(barcode: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM margin_warning_streaks WHERE barcode = ?", (barcode,))
    conn.commit()
    conn.close()


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
    (pending_price_alerts에도 같이 기록됨).

    manual_override = 1(관리자가 직접 링크를 지정한 상품)만 추적한다 -
    자동검색(refresh_products)으로 채워진 상품은 검색 API가 고른 옵션/
    후보가 실제로 맞는 상품인지 보장이 없어서(수량 옵션 오매칭 등), 가격
    추적의 정확도를 관리자가 육안으로 확인한 링크로만 한정해달라는 요청
    (2026-09-23)에 따른 것. 자동검색 자체(신제품 발견/표시)는 그대로
    유지되고, 반복 가격조회 대상에서만 제외된다."""
    try:
        catalog = mapping.load_catalog()
    except Exception as e:
        print(f"[PRODUCT_RANKING:{pt.key}] 카탈로그 로드 실패:", e)
        return {"ok": False, "error": str(e)}

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"""
    SELECT item_key, item_name, price, pending_price, pending_count, coupang_product_id FROM {pt.table_name}
    WHERE reference_url IS NOT NULL AND deleted = 0 AND manual_override = 1
    ORDER BY price_checked_at ASC NULLS FIRST
    LIMIT ?
    """, (limit,))
    targets = cur.fetchall()

    checked = 0
    recorded = 0
    rate_limited = False
    new_lows = []
    margin_warnings = []
    now = datetime.now().isoformat(timespec="seconds")

    for item_key, stored_name, stored_price, pending_price, pending_count, stored_product_id in targets:
        entry = catalog.get(item_key)
        keyword = (entry.search_keyword or entry.menu_name) if entry else stored_name
        checked += 1

        try:
            candidates = search_coupang_products_for_price_check(keyword)
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

        if not candidates:
            conn.commit()
            time.sleep(SEARCH_DELAY_SECONDS_PRICE_CHECK)
            continue

        # 저장된 productId와 정확히 일치하는 후보가 있으면 "진짜 같은 상품"임이
        # 확정된다 - 재검색 때마다 순위가 바뀌거나 이름이 미묘하게 다르게
        # 나와도(실측: 짧은 카탈로그 이름 특성상 유사도만으론 헐거움) 흔들리지
        # 않는 판단 근거라, 이름 유사도/다중확인 없이 바로 신뢰한다.
        id_match = None
        if stored_product_id:
            id_match = next(
                (c for c in candidates if c.get("product_id") is not None and str(c["product_id"]) == stored_product_id),
                None,
            )

        # id_match(이미 확정된 상품)면 그대로 쓰고, 아직 확정된 적 없으면
        # (재검색 첫 성공 등) 여기서도 10개 이상 옵션을 우선한다 - 검색
        # 순위 1위가 소용량 옵션인 경우가 흔해서(실측: 야채타임/새우깡 등).
        result = id_match or _pick_preferred_candidate(candidates, stored_name or "")
        if not result.get("price"):
            conn.commit()
            time.sleep(SEARCH_DELAY_SECONDS_PRICE_CHECK)
            continue

        found_name = result.get("product_name") or ""
        new_price = result["price"]
        match_method = "product_id" if id_match else "similarity"

        if id_match:
            required_confirmations = 1
        else:
            sim = product_match.similarity(stored_name or "", found_name)

            threshold = PRICE_CHECK_SIMILARITY_THRESHOLD
            is_deviant = False
            is_extreme = False
            if stored_price and new_price:
                ratio = new_price / stored_price
                is_deviant = ratio < PRICE_CHECK_DEVIATION_LOW_RATIO or ratio > PRICE_CHECK_DEVIATION_HIGH_RATIO
                is_extreme = ratio < PRICE_CHECK_EXTREME_DEVIATION_RATIO
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
            # 확정하지 않고, 같은 가격이 연속으로 재확인돼야 실제 가격 변동으로
            # 인정한다. 다만 2026-07-29 실측으로, "검색어가 다른 상품/판매단위로
            # 안정적으로 재매칭된" 경우 2회 연속 확인 정도는 쉽게 통과한다는 걸
            # 확인했다(빼빼로 화이트쿠키 81% 하락, 오징어땅콩 83% 하락 등이 실제로
            # 이 경로로 통과해 발송됨) - 하락폭이 클수록(60%+) 우연이 아니라 안정적인
            # 오탐일 가능성이 크다고 보고 요구 확인 횟수를 3회로 늘린다.
            # (id_match가 없을 때만 이 다중확인 절차를 거친다 - productId로
            # 확정된 경우는 애초에 이런 오탐이 구조적으로 불가능하다.)
            required_confirmations = (
                PRICE_CHECK_EXTREME_CONFIRMATIONS_REQUIRED if is_extreme
                else PRICE_CHECK_CONFIRMATIONS_REQUIRED if is_deviant
                else 1
            )

        if required_confirmations > 1:
            confirmed_count = (pending_count or 0) + 1 if pending_price == new_price else 1
            if confirmed_count < required_confirmations:
                cur.execute(
                    f"UPDATE {pt.table_name} SET pending_price = ?, pending_count = ? WHERE item_key = ?",
                    (new_price, confirmed_count, item_key),
                )
                print(f"[PRODUCT_RANKING:{pt.key}] {item_key!r} 가격 급변 감지({stored_price} -> {new_price}, "
                      f"{confirmed_count}/{required_confirmations}회 확인) - 재확인 필요")
                conn.commit()
                time.sleep(SEARCH_DELAY_SECONDS_PRICE_CHECK)
                continue
            cur.execute(f"UPDATE {pt.table_name} SET pending_price = NULL, pending_count = 0 WHERE item_key = ?", (item_key,))
        elif pending_price is not None or pending_count:
            cur.execute(f"UPDATE {pt.table_name} SET pending_price = NULL, pending_count = 0 WHERE item_key = ?", (item_key,))

        cur.execute(
            "SELECT MIN(price) FROM price_history WHERE product_type = ? AND item_key = ?",
            (pt.key, item_key),
        )
        prior_low = cur.fetchone()[0]

        # 개당 매입가 추적(사용자 요청) - 쿠팡(is_coupang=1) 상품만 대상으로,
        # 이번에 신뢰하고 쓴 상품명에서 묶음 수량("20개입" 등)을 파싱해 개당
        # 매입가를 계산한다. 도매몰 상품은 pack_qty가 이미 발주 단위(1타
        # 개수)라는 다른 의미로 쓰이고 있어 이 경로에서는 절대 건드리지
        # 않는다(mapping.update_coupang_pack_qty가 is_coupang=1 조건으로
        # 한 번 더 막아준다).
        pack_qty = None
        unit_cost = new_price
        if entry and entry.is_coupang == 1:
            pack_qty = _extract_coupang_pack_qty(found_name) or _extract_coupang_pack_qty(stored_name or "")
            if pack_qty and pack_qty > 1:
                unit_cost = round(new_price / pack_qty)
                mapping.update_coupang_pack_qty(item_key, pack_qty)

        cur.execute(
            "INSERT INTO price_history (product_type, item_key, price, recorded_at, pack_qty) VALUES (?, ?, ?, ?, ?)",
            (pt.key, item_key, new_price, now, pack_qty),
        )
        # 이번에 실제로 신뢰하고 쓴 결과의 productId로 갱신/백필한다 - 처음
        # 매칭 때는 없었거나(기존 행) id_match가 아니었던 행도, 유사도 검증을
        # 통과한 안전한 시점에 채워둬서 다음 확인부터는 ID 경로를 탈 수 있게
        # 한다.
        new_product_id = str(result["product_id"]) if result.get("product_id") is not None else stored_product_id
        cur.execute(
            f"UPDATE {pt.table_name} SET price = ?, coupang_product_id = ? WHERE item_key = ?",
            (new_price, new_product_id, item_key),
        )
        recorded += 1

        # 도매몰처럼 매입가를 직접 크롤링할 수 없는 쿠팡 상품은, 방금 구한
        # 개당 매입가를 카탈로그의 추천판매가와 비교해 마진율이 낮으면(역마진
        # 포함 20% 이하 - 사용자 요청) 관리자에게 알린다. 마진율 정의는
        # catalog_margin.py(도매몰 추천판매가 계산)와 같은 "총이익률" 방식
        # (판매가 기준: (판매가-원가)/판매가)으로 통일한다 - 역마진이면 이
        # 값 자체가 음수로 나와 자연히 20% 이하 조건에 포함된다.
        #
        # pack_qty를 상품명에서 못 읽은 경우(수량 미표기)는 방금 구한
        # unit_cost가 진짜 개당가인지 확신할 수 없어서(검색이 그날 우연히
        # 수량 미표기의 다른 판매단위 상품을 골라왔을 수 있음 - 실측
        # 사례 있음), 바로 알리지 않고 여러 스캔 주기에 걸쳐 연속으로 같은
        # 문제가 재현돼야만 알린다. pack_qty를 정상적으로 읽었을 때는
        # 확실한 정보이므로 예전처럼 바로 알린다.
        if entry and entry.is_coupang == 1 and entry.recommended_price:
            margin_pct = round((entry.recommended_price - unit_cost) / entry.recommended_price * 100, 1)
            has_reliable_qty = bool(pack_qty and pack_qty > 1)
            if margin_pct <= 20:
                should_warn = has_reliable_qty
                if not has_reliable_qty:
                    streak = _bump_margin_streak(item_key)
                    should_warn = streak >= MARGIN_WARNING_UNPARSED_CONFIRMATIONS_REQUIRED
                    if should_warn:
                        _reset_margin_streak(item_key)
                if should_warn:
                    margin_warnings.append({
                        "item_key": item_key, "item_name": stored_name,
                        "recommended_price": entry.recommended_price,
                        "unit_cost": unit_cost, "pack_qty": pack_qty or 1,
                        "margin_pct": margin_pct,
                        "confirmed_qty": has_reliable_qty,
                    })
            elif not has_reliable_qty:
                _reset_margin_streak(item_key)

        if prior_low is not None and new_price < prior_low:
            cur.execute("""
            INSERT INTO pending_price_alerts (product_type, item_key, item_name, old_low, new_price, created_at, status, match_method)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """, (pt.key, item_key, stored_name, prior_low, new_price, now, match_method))
            new_lows.append({
                "item_key": item_key, "item_name": stored_name, "old_low": prior_low,
                "new_price": new_price, "match_method": match_method,
            })

        conn.commit()
        time.sleep(SEARCH_DELAY_SECONDS_PRICE_CHECK)

    conn.close()
    return {
        "ok": True, "checked": checked, "recorded": recorded,
        "rate_limited": rate_limited, "new_lows": new_lows,
        "margin_warnings": margin_warnings,
    }


def get_price_history(pt: ProductType, item_key: str) -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT price, recorded_at, pack_qty FROM price_history
    WHERE product_type = ? AND item_key = ?
    ORDER BY recorded_at ASC
    """, (pt.key, item_key))
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "price": r[0], "recorded_at": r[1], "pack_qty": r[2],
            "unit_cost": round(r[0] / r[2]) if r[2] and r[2] > 1 else r[0],
        }
        for r in rows
    ]


def list_active_hotdeals(pt: ProductType, limit: int = 30) -> list[dict]:
    """"핫딜 안내" 페이지용 - 타임세일 성격이라 가격이 다시 오르면 그 즉시
    사라져야 한다(사용자 요청). 그래서 "한 번 감지된 걸 계속 보여주는" 방식이
    아니라, 조회할 때마다 "지금 이 순간도 역대 최저가에 머물러 있는 상품"만
    실시간으로 걸러서 돌려준다 - 다음 가격 스캔에서 가격이 오르면(캐시된
    price가 바뀌면) 그 즉시 이 목록에서도 빠진다.

    pending_price_alerts와 INNER JOIN해서 "실제로 역대 최저가 갱신이 감지된
    적 있는" 상품만 후보로 삼는다(가격을 한 번밖에 안 검사한 상품이 우연히
    "현재가=최저가"인 것과 구분하기 위함). match_method='product_id'(쿠팡
    상품 고유 ID로 확정 대조된 것)만 쓴다 - 이름 유사도 매칭은 실측으로
    다른 상품이 잘못 매칭되는 사례가 있어(예: "피크닉 사과" 검색에 수량
    표기도 없는 "피크닉 사과 주스"가 매칭됨), 공개 페이지에 엉뚱한 상품
    사진/링크가 뜨는 걸 막기 위해 확정 매칭만 노출한다.

    pack_qty/unit_cost도 같이 내려준다("이 가격이 몇 개들이인지, 개당
    얼마인지" 사용자 요청) - 최저가(t.price)를 기록했던 그 순간의
    price_history 행에서 pack_qty를 그대로 가져온다(현재 catalog_items의
    pack_qty를 쓰지 않는 이유: 그 값은 이후 다른 상품 매칭으로 갱신됐을 수
    있어서, "이 최저가 자체가 몇 개들이였는지"는 그 시점 기록이 더
    정확함). 수량을 못 읽은 상품은 pack_qty가 NULL로 내려가고, 화면에서는
    개당가 표기를 생략한다."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f"""
    SELECT t.item_key, t.item_name, t.image_url, t.partners_link, t.price, MAX(a.created_at) AS detected_at,
        (
            SELECT ph.pack_qty FROM price_history ph
            WHERE ph.product_type = ? AND ph.item_key = t.item_key AND ph.price = t.price
            ORDER BY ph.recorded_at DESC LIMIT 1
        ) AS pack_qty
    FROM {pt.table_name} t
    JOIN pending_price_alerts a
        ON a.product_type = ? AND a.item_key = t.item_key
        AND a.match_method = 'product_id' AND a.new_price = t.price
    WHERE t.partners_link IS NOT NULL AND t.deleted = 0
      AND t.price = (SELECT MIN(price) FROM price_history WHERE product_type = ? AND item_key = t.item_key)
    GROUP BY t.item_key, t.item_name, t.image_url, t.partners_link, t.price
    ORDER BY detected_at DESC
    LIMIT ?
    """, (pt.key, pt.key, pt.key, limit))
    rows = cur.fetchall()
    conn.close()
    result = []
    for r in rows:
        price, pack_qty = r[4], r[6]
        unit_cost = round(price / pack_qty) if pack_qty and pack_qty > 1 else None
        result.append({
            "item_key": r[0], "item_name": r[1], "image_url": r[2], "partners_link": r[3],
            "price": price, "detected_at": r[5], "product_type": pt.key,
            "pack_qty": pack_qty, "unit_cost": unit_cost,
        })
    return result


def list_recent_price_alerts(limit: int = 200) -> list[dict]:
    """관리자 페이지에서 최근 감지된 최저가 알림을 상태(대기/알림전송/전체발송/생략)
    구분 없이 전부 최신순으로 보여줄 때 쓴다."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT id, product_type, item_key, item_name, old_low, new_price, created_at, status, match_method
    FROM pending_price_alerts ORDER BY id DESC LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "id": r[0], "product_type": r[1], "item_key": r[2], "item_name": r[3],
            "old_low": r[4], "new_price": r[5], "created_at": r[6], "status": r[7],
            "match_method": r[8],
        }
        for r in rows
    ]


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
