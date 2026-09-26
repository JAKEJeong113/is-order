# cu_price_crawl.py
"""CU(BGF리테일) 상품 페이지에서 상품명/판매가(편의점 소비자가)를 크롤링해서
cu_retail_prices에 저장하고, 이걸로 catalog_items 중 쿠팡(is_coupang=1)
분류인데 추천판매가가 비어있는 상품의 가격을 검증한다.

배경(사용자 확인, 2026-09-25): 무인매장은 편의점보다 매입가를 같거나 낮게
잡는 게 기본 전제라, "편의점판매가 >= 추천판매가"가 항상 성립해야 한다.
catalog_auto_import.py(도매몰 크롤링)는 쿠팡 분류 상품의 추천판매가를 절대
채우지 않도록 이미 막아뒀는데(마진을 낮게 잡는 쿠팡 특성상 도매몰식 계산값이
너무 높게 나올 위험 - catalog_auto_import.py 참고), 그럼 쿠팡 분류 상품은
추천판매가를 어떻게 채우나? -> 쿠팡 검색 API로 현재 매입가를 구해 도매몰과
동일한 마진 계산(catalog_margin.compute_recommended_price)을 적용해 후보값을
만들고, 그 값이 실제로 합리적인지(편의점판매가보다 낮은지) CU 크롤링 데이터로
검증한다.

CU 사이트 구조(실측 확인, 2026-09-25):
  - 로그인/JS 렌더링 불필요 - "POST /product/productAjax.do"가 순수 서버사이드
    페이징 API라 requests만으로 충분하다(플레이라이트 불필요, 이 프로젝트의
    다른 크롤러보다 훨씬 가볍다).
  - 대분류 코드: 간편식사(10)/즉석조리(20)/과자류(30)/아이스크림(40)/
    식품(50)/음료(60)/생활용품(70) - 페이지당 40개, 빈 목록이 오면 그
    카테고리 끝. 간편식사/즉석조리/생활용품은 크롤링 대상에서 제외한다
    (사용자 요청, 2026-09-25 - 무인매장 취급 품목과 겹치지 않음).
  - 상품 목록에는 바코드 필드가 따로 없지만, 상품 이미지 파일명 자체가
    바코드다(예: "8801771037705.jpg") - 실측 샘플(과자류 1페이지 40개)에서
    38/40(95%)이 EAN 체크섬까지 통과, 나머지는 "바코드_1.jpg"처럼 접미사가
    붙은 형태(접미사 제거 후 검증하면 됨). 상품명 자체에는 용량(g/ml) 표기가
    없어서(예: "도)마제소밥함박정식"), 바코드로 못 찾으면 이름+용량으로
    추정하는 건 사실상 불가능하다 - 이름만으로 하는 유사도 매칭은 이 세션에서
    이미 "N종 세트" 같은 오매칭 사고로 신뢰도가 낮다는 게 확인된 방식이라,
    바코드가 없는 상품(약 5~8%)은 그냥 건너뛴다(바코드 매칭만 신뢰).
"""
import re
from datetime import datetime

import requests

import catalog_margin
import db_conn
import mapping
import product_ranking
from godomall_bot import _validate_barcode_checksum

CU_CATEGORY_CODES = {
    "30": "과자류", "40": "아이스크림", "50": "식품", "60": "음료",
}
CU_AJAX_URL = "https://cu.bgfretail.com/product/productAjax.do"
CU_ITEMS_PER_PAGE_STOP = 60  # 이 값(넉넉한 상한)까지 갔는데도 안 끝나면 이상 상황으로 보고 중단

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://cu.bgfretail.com/product/product.do?category=product&depth2=4&sf=N",
    "X-Requested-With": "XMLHttpRequest",
}

ROUND_UNIT = 100
# 쿠팡 분류 상품 전용 마진율(사용자 확인, 2026-09-26) - 도매몰(40~50%)보다
# 낮다. 온라인(쿠팡) 최저가와 경쟁해야 해서 마진을 박하게 잡는 게 맞고,
# 실제로 9/11 또요몰 크롤링이 도매몰 마진 기준 명시가를 쿠팡 분류 34개
# 상품에 잘못 덮어써서 편의점판매가보다 비싸지는 사고가 있었다(되돌림
# 처리함) - 그 사고를 막기 위해 catalog_auto_import.py는 쿠팡 분류 상품을
# 아예 건드리지 않도록 막아뒀고, 쿠팡 분류 상품의 가격은 오직 이 모듈에서만
# (쿠팡 매입가 기준 20~30% 마진 → 편의점가로 상한) 계산한다.
COUPANG_MARGIN_RANGE = (20, 30)


def init_coupang_price_exclusions_table() -> None:
    """이 모듈(fix_coupang_prices)이 절대 건드리면 안 되는 바코드 목록.
    "모구모구 요거트향"(8850389109229) 사례처럼 CU 크롤링이 다른 상품과
    바코드를 잘못 엮어서(실측: 전혀 다른 320ml 음료 190원과 매칭) 명백히
    말이 안 되는 가격이 반영되는 경우, 관리자가 여기 등록해서 앞으로 이
    바코드는 이 로직이 절대 가격을 안 건드리게 한다(사용자 요청,
    2026-09-26)."""
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS coupang_price_exclusions (
        barcode TEXT PRIMARY KEY,
        reason TEXT,
        added_at TEXT
    )
    """)
    conn.commit()
    conn.close()


def add_coupang_price_exclusion(barcode: str, reason: str = "") -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn = db_conn.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO coupang_price_exclusions (barcode, reason, added_at) VALUES (?, ?, ?)
            ON CONFLICT(barcode) DO UPDATE SET reason = excluded.reason, added_at = excluded.added_at
            """,
            (barcode, reason, now),
        )
        conn.commit()
    finally:
        conn.close()


def _load_excluded_barcodes() -> set[str]:
    conn = db_conn.get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT barcode FROM coupang_price_exclusions")
        return {row[0] for row in cur.fetchall()}
    finally:
        conn.close()


def init_coupang_wholesale_costs_table() -> None:
    """쿠팡(is_coupang=1) 분류 상품인데 도매몰(도윤상사/mud5 등)에도 같은
    바코드가 있는 경우, catalog_auto_import.py가 실측한 진짜 매입가(박스가÷
    개수, 마진 적용 전)를 여기 저장해둔다 - compute_and_apply_coupang_price가
    쿠팡 검색 API로 어림짐작하는 것보다 이 값을 우선 쓴다(사용자 요청,
    2026-09-26: "쿠팡분류의 매입가 기준은 도윤상사에서 크롤링된 매입값을
    기준으로 해줘")."""
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS coupang_wholesale_costs (
        barcode TEXT PRIMARY KEY,
        vendor_name TEXT,
        unit_cost INTEGER,
        updated_at TEXT
    )
    """)
    conn.commit()
    conn.close()


def save_coupang_wholesale_cost(barcode: str, vendor_name: str, unit_cost: int) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn = db_conn.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO coupang_wholesale_costs (barcode, vendor_name, unit_cost, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(barcode) DO UPDATE SET
                vendor_name = excluded.vendor_name, unit_cost = excluded.unit_cost,
                updated_at = excluded.updated_at
            """,
            (barcode, vendor_name, unit_cost, now),
        )
        conn.commit()
    finally:
        conn.close()


def get_coupang_wholesale_cost(barcode: str) -> dict | None:
    conn = db_conn.get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT vendor_name, unit_cost FROM coupang_wholesale_costs WHERE barcode = ?", (barcode,))
        row = cur.fetchone()
        if not row:
            return None
        return {"vendor_name": row[0], "unit_cost": row[1]}
    finally:
        conn.close()


def init_cu_retail_prices_table() -> None:
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS cu_retail_prices (
        barcode TEXT PRIMARY KEY,
        item_name TEXT,
        price INTEGER,
        category TEXT,
        updated_at TEXT
    )
    """)
    conn.commit()
    conn.close()


def init_cu_price_overrides_table() -> None:
    """cu.bgfretail.com(본사 안내 사이트, 이 모듈이 크롤링하는 곳)이 실제 CU
    앱(픽업/배달 주문) 가격과 다른 경우가 실측으로 확인됐다(예: "크라운)꽃게랑
    마라맛" 8801111961431 - 안내 사이트는 2,000원인데 실제 앱은 1,700원).
    이런 바코드는 관리자가 여기 수동으로 정확한 값을 등록해두면, 이후
    crawl_all_categories/save_cu_retail_prices가 그 바코드는 크롤링 결과로
    덮어쓰지 않고 이 값을 그대로 유지한다(사용자 요청, 2026-09-26)."""
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS cu_price_overrides (
        barcode TEXT PRIMARY KEY,
        price INTEGER,
        reason TEXT,
        added_at TEXT
    )
    """)
    conn.commit()
    conn.close()


def set_cu_price_override(barcode: str, price: int, reason: str = "") -> None:
    """override를 등록하고, cu_retail_prices에도 즉시 반영한다(기존 행이
    있으면 이름/분류는 그대로 두고 가격만 바꾸고, 없으면 새로 만든다)."""
    now = datetime.now().isoformat(timespec="seconds")
    conn = db_conn.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO cu_price_overrides (barcode, price, reason, added_at) VALUES (?, ?, ?, ?)
            ON CONFLICT(barcode) DO UPDATE SET price = excluded.price, reason = excluded.reason, added_at = excluded.added_at
            """,
            (barcode, price, reason, now),
        )
        cur.execute("SELECT item_name, category FROM cu_retail_prices WHERE barcode = ?", (barcode,))
        row = cur.fetchone()
        item_name, category = (row[0], row[1]) if row else (None, None)
        cur.execute(
            """
            INSERT INTO cu_retail_prices (barcode, item_name, price, category, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(barcode) DO UPDATE SET price = excluded.price, updated_at = excluded.updated_at
            """,
            (barcode, item_name, price, category, now),
        )
        conn.commit()
    finally:
        conn.close()


def _load_cu_price_override_barcodes() -> set[str]:
    conn = db_conn.get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT barcode FROM cu_price_overrides")
        return {row[0] for row in cur.fetchall()}
    finally:
        conn.close()


def _extract_products(html: str) -> list[dict]:
    names = re.findall(r'<div class="name"[^>]*><p>(.*?)</p></div>', html)
    prices = re.findall(r'<strong>([\d,]+)</strong>', html)
    imgs = re.findall(r'/product/([\w\-\.]+)\.\w+"', html)
    products = []
    for name, price_text, img in zip(names, prices, imgs):
        barcode = re.sub(r"_\d+$", "", img)  # "8801234567890_1" 같은 접미사 제거
        if not _validate_barcode_checksum(barcode):
            continue
        products.append({
            "barcode": barcode,
            "name": name.strip(),
            "price": int(price_text.replace(",", "")),
        })
    return products


def _fetch_page(category_code: str, page: int) -> str:
    data = {
        "pageIndex": str(page), "searchMainCategory": category_code, "searchSubCategory": "",
        "listType": "1", "searchCondition": "setA", "searchUseYn": "", "gdIdx": "0",
        "codeParent": category_code, "user_id": "", "search1": "", "search2": "", "searchKeyword": "",
    }
    resp = requests.post(CU_AJAX_URL, headers=_HEADERS, data=data, timeout=20)
    resp.raise_for_status()
    return resp.text


def crawl_all_categories() -> dict[str, dict]:
    """전체 카테고리를 순회해서 바코드별 최신 상품명/가격을 돌려준다(같은
    바코드가 여러 카테고리에 겹쳐 나오면 나중 것으로 자연스럽게 덮어씀 -
    이름/가격이 실질적으로 같아 문제 없음)."""
    by_barcode: dict[str, dict] = {}
    for category_code, category_name in CU_CATEGORY_CODES.items():
        page_count = 0
        for page in range(1, CU_ITEMS_PER_PAGE_STOP + 1):
            html = _fetch_page(category_code, page)
            products = _extract_products(html)
            if not products:
                break
            page_count = page
            for p in products:
                by_barcode[p["barcode"]] = {
                    "barcode": p["barcode"], "item_name": p["name"],
                    "price": p["price"], "category": category_name,
                }
        print(f"[CU_PRICE_CRAWL] {category_name}({category_code}) 완료 - 페이지 {page_count}개")
    return by_barcode


def save_cu_retail_prices(by_barcode: dict[str, dict]) -> int:
    """crawl_all_categories() 결과를 저장한다. cu_price_overrides에 등록된
    바코드는 크롤링 결과가 뭐든 건드리지 않는다(본사 안내 사이트 가격이 실제
    앱 가격과 다르다고 확인된 예외 - init_cu_price_overrides_table 참고)."""
    overridden = _load_cu_price_override_barcodes()
    now = datetime.now().isoformat(timespec="seconds")
    saved = 0
    conn = db_conn.get_conn()
    try:
        cur = conn.cursor()
        for item in by_barcode.values():
            if item["barcode"] in overridden:
                continue
            cur.execute("""
            INSERT INTO cu_retail_prices (barcode, item_name, price, category, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(barcode) DO UPDATE SET
                item_name = excluded.item_name, price = excluded.price,
                category = excluded.category, updated_at = excluded.updated_at
            """, (item["barcode"], item["item_name"], item["price"], item["category"], now))
            saved += 1
        conn.commit()
        return saved
    finally:
        conn.close()


def refresh_cu_retail_prices() -> int:
    """CU 카탈로그 전체를 다시 크롤링해서 cu_retail_prices를 최신화한다.
    main.py 스케줄러에서 주기 호출한다."""
    by_barcode = crawl_all_categories()
    return save_cu_retail_prices(by_barcode)


def get_cu_price(barcode: str) -> dict | None:
    conn = db_conn.get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT item_name, price, category FROM cu_retail_prices WHERE barcode = ?", (barcode,))
        row = cur.fetchone()
        if not row:
            return None
        return {"item_name": row[0], "price": row[1], "category": row[2]}
    finally:
        conn.close()


def _apply_coupang_price(barcode: str, price: int, notes: str) -> None:
    """가격인상안내(바코드 사이트)용 - 덮어쓰기 전에 기존 가격을 먼저 봐서
    실제로 오른 경우만 mapping.record_price_change가 기록한다(대부분은
    오적용을 바로잡는 하락일 것이라 조용히 넘어감)."""
    now = datetime.now().isoformat(timespec="seconds")
    conn = db_conn.get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT recommended_price FROM catalog_items WHERE barcode = ?", (barcode,))
        row = cur.fetchone()
        old_price = row[0] if row else None
        cur.execute(
            "UPDATE catalog_items SET recommended_price = ?, notes = ?, updated_at = ? WHERE barcode = ?",
            (price, notes, now, barcode),
        )
        conn.commit()
    finally:
        conn.close()
    mapping.record_price_change(barcode, old_price, price)


def _margin_pct(price: float, unit_cost: float) -> float:
    return (price - unit_cost) / price * 100


def compute_and_apply_coupang_price(barcode: str, menu_name: str, search_keyword: str | None) -> dict:
    """쿠팡(is_coupang=1) 분류 상품 하나의 추천판매가를 계산한다(사용자 확인된
    로직, 2026-09-26 최종 확정):
      1차 - 매입가를 구한다. coupang_wholesale_costs(도매몰 실측 매입가 -
            도윤상사/mud5 등 크롤링 중 이 바코드를 발견했을 때 저장됨)에
            값이 있으면 그걸 우선 쓴다(사용자 요청: "쿠팡분류의 매입가
            기준은 도윤상사에서 크롤링된 매입값을 기준으로 해줘" - 쿠팡
            검색 API로 어림짐작하는 것보다 실제 도매 매입가가 신뢰도가
            높다). 없으면 쿠팡 검색 API로 대체 추정한다.
      2차 - CU 편의점판매가가 있으면 그 값을 기준으로 확정한다("그냥 현재
            가격을 유지하라는 뜻이 아니다" - 항상 아래 규칙으로 값을 정한다):
              - 편의점가 그대로 썼을 때 마진이 19% 이하 -> 편의점가와 동일.
              - 편의점가보다 100원 낮췄을 때도 마진이 20% 이상 유지 ->
                편의점가 - 100원.
              - 그 외(편의점가 마진은 19% 넘지만 100원 낮추면 20% 밑으로
                떨어짐) -> 편의점가와 동일(100원 낮추지 않음).
            매입가 추정이 실제로 잘못됐어도(예: 검색 키워드가 지저분해서
            엉뚱한 대용량 상품에 매칭 - 마이구미포도/드림카카오72 등에서
            실측 확인된 사례) 마진이 극단적으로 마이너스가 나와 자연히
            "편의점가와 동일"로 수렴하므로, 최종값이 편의점가를 넘는 사고로는
            이어지지 않는다.
      CU 매칭 자체가 없는 상품(편의점에서 안 파는 상품으로 추정)은 비교 기준이
      없어 1차 계산값(매입가 기준 20~30% 마진, COUPANG_MARGIN_RANGE)을 그대로
      쓴다 - 단, 매입가 출처가 도매몰 실측이 아니라 "쿠팡 검색"뿐이면 이마저도
      적용하지 않고 "확인 필요"로만 보고한다(실측 확인, 2026-09-26: 편의점가
      교차검증이 없으면 검색 키워드가 멀쩡해 보이는 상품도 쿠팡 검색이 엉뚱한
      대용량 상품에 매칭돼 매입가가 10배 넘게 부풀려지는 사고를 걸러낼 방법이
      없다 - 실제로 142개 상품이 이 경로로 잘못 반영됐다가 전부 되돌림).
      매입가를 아예 구할 수 없는 상품(도매몰에도 없고 쿠팡 검색 결과도 없음)도
      "확인 필요"로 보고하고 미반영한다."""
    wholesale_cost = get_coupang_wholesale_cost(barcode)
    cost_source = None
    if wholesale_cost:
        unit_cost = wholesale_cost["unit_cost"]
        cost_source = wholesale_cost["vendor_name"]
    else:
        keyword = search_keyword or menu_name
        try:
            candidate = product_ranking.search_coupang_product(keyword)
        except product_ranking.CoupangRateLimitError:
            return {"barcode": barcode, "name": menu_name, "ok": False, "reason": "RATE_LIMIT"}
        if not candidate or not candidate.get("price"):
            return {"barcode": barcode, "name": menu_name, "ok": False, "reason": "쿠팡 검색 결과 없음"}
        pack_qty = product_ranking._extract_coupang_pack_qty(candidate.get("product_name") or "") or 1
        unit_cost = candidate["price"] / pack_qty if pack_qty > 1 else candidate["price"]
        cost_source = "쿠팡 검색"
    try:
        margin_price, margin_pct = catalog_margin.compute_recommended_price(
            unit_cost, ROUND_UNIT, *COUPANG_MARGIN_RANGE,
        )
    except ValueError:
        return {"barcode": barcode, "name": menu_name, "ok": False, "reason": f"매입가 계산 실패(unit_cost={unit_cost})"}

    today = datetime.now().date().isoformat()
    cu = get_cu_price(barcode)
    if not cu:
        # 편의점가로 교차검증할 수 없는 상태에서, 매입가 출처가 "쿠팡 검색"
        # (도매몰 실측이 아님)이면 절대 적용하지 않는다 - 실측으로 확인됨:
        # 검색 키워드가 멀쩡해 보여도(예: "맥콜", "레쓰비") 쿠팡 검색이
        # 대용량/엉뚱한 상품에 매칭돼 매입가가 10배 넘게 부풀려지는 사고가
        # 편의점가 교차검증 없이는 걸러지지 않는다는 게 실측으로 확인됐다
        # (2026-09-26 - 142개 상품에 이 경로로 잘못된 값이 반영됐다가 전부
        # 되돌림). 도매몰에서 실측한 매입가(cost_source가 벤더명)는 신뢰도가
        # 달라 편의점가 없이도 그대로 적용한다.
        if cost_source == "쿠팡 검색":
            return {
                "barcode": barcode, "name": menu_name, "ok": False,
                "reason": f"편의점 미매칭 + 매입가 출처가 쿠팡 검색뿐이라 검증 불가(매입가 {round(unit_cost)}원)",
                "unit_cost": round(unit_cost), "cost_source": cost_source,
            }
        notes = f"매입가 {round(unit_cost)}원({cost_source}) 기준 {margin_pct}% 마진 계산(편의점 미매칭) · {today}"
        _apply_coupang_price(barcode, margin_price, notes)
        return {
            "barcode": barcode, "name": menu_name, "ok": True,
            "unit_cost": round(unit_cost), "cost_source": cost_source, "margin_pct": margin_pct,
            "cu_price": None, "final_price": margin_price, "capped": False,
        }

    cu_price = cu["price"]
    margin_at_cu = _margin_pct(cu_price, unit_cost)
    if margin_at_cu <= 19:
        final_price = cu_price
    else:
        lower = cu_price - 100
        margin_at_lower = _margin_pct(lower, unit_cost) if lower > 0 else -999.0
        final_price = lower if margin_at_lower >= 20 else cu_price
    final_margin = round(_margin_pct(final_price, unit_cost), 1)
    capped = final_price < cu_price

    notes = (
        f"매입가 {round(unit_cost)}원({cost_source}), 편의점가 {cu_price}원 기준 산정"
        f"(적용가 {final_price}원, 마진 {final_margin}%) · {today}"
    )
    _apply_coupang_price(barcode, final_price, notes)
    return {
        "barcode": barcode, "name": menu_name, "ok": True,
        "unit_cost": round(unit_cost), "cost_source": cost_source, "margin_pct": final_margin,
        "cu_price": cu_price, "final_price": final_price, "capped": capped,
    }


def fix_coupang_prices() -> list[dict]:
    """추천판매가가 비어있거나(신규) 편의점판매가보다 비싸게 잘못 들어간
    (오적용/과거 사고) 쿠팡 분류 상품을 전부 찾아 compute_and_apply_coupang_price로
    바로잡는다. main.py 스케줄러가 주기 호출한다.

    coupang_price_exclusions에 등록된 바코드는 대상에서 아예 뺀다(사용자
    요청, 2026-09-26 - "모구모구 요거트향"이 CU 크롤링 바코드 오매칭으로
    190원이라는 말이 안 되는 값이 반영된 사례 - add_coupang_price_exclusion
    참고)."""
    excluded = _load_excluded_barcodes()

    conn = db_conn.get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
        SELECT c.barcode, c.menu_name, c.search_keyword
        FROM catalog_items c
        LEFT JOIN cu_retail_prices cu ON cu.barcode = c.barcode
        WHERE c.is_coupang = 1
          AND (
              c.recommended_price IS NULL OR c.recommended_price = 0
              OR (cu.price IS NOT NULL AND c.recommended_price > cu.price)
          )
        """)
        rows = cur.fetchall()
    finally:
        conn.close()

    results = []
    for barcode, menu_name, search_keyword in rows:
        if barcode in excluded:
            continue
        result = compute_and_apply_coupang_price(barcode, menu_name, search_keyword)
        results.append(result)
        if result.get("reason") == "RATE_LIMIT":
            break
    return results
