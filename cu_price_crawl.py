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
    now = datetime.now().isoformat(timespec="seconds")
    conn = db_conn.get_conn()
    try:
        cur = conn.cursor()
        for item in by_barcode.values():
            cur.execute("""
            INSERT INTO cu_retail_prices (barcode, item_name, price, category, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(barcode) DO UPDATE SET
                item_name = excluded.item_name, price = excluded.price,
                category = excluded.category, updated_at = excluded.updated_at
            """, (item["barcode"], item["item_name"], item["price"], item["category"], now))
        conn.commit()
        return len(by_barcode)
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


def compute_and_apply_coupang_price(barcode: str, menu_name: str, search_keyword: str | None) -> dict:
    """쿠팡(is_coupang=1) 분류 상품 하나의 추천판매가를 계산한다(사용자 확인된
    2단계 로직, 2026-09-26):
      1차 - 쿠팡 검색 API로 현재 매입가를 구해 20~30% 마진을 적용한다
            (catalog_margin.compute_recommended_price, 도매몰보다 낮은 마진 -
            모듈 상단 COUPANG_MARGIN_RANGE 설명 참고).
      2차 - CU 편의점판매가로 상한을 씌운다(1차 계산값보다 낮으면 편의점가로
            낮춤 - "편의점판매가 <= 추천판매가는 있을 수 없다"는 전제).

    CU 편의점가로 검증(2차)할 수 있을 때만 실제로 catalog_items에 반영한다.
    검색 키워드가 지저분한 상품(예: menu_name에 "1300", "24" 같은 가격/수량이
    섞여 있는 경우 - search_keyword가 비어있어 이런 menu_name을 그대로 검색어로
    쓰게 됨)은 쿠팡 검색이 완전히 엉뚱한 상품(매입가가 몇 배 부풀려진 대용량
    묶음 등)에 매칭될 수 있다는 게 실측으로 확인됐다(마이구미포도가 매입가
    4,000원으로, 드림카카오72가 19,080원으로 잡히는 등 - 원래 판매가의 몇 배).
    이때 CU 편의점가도 매입가보다 훨씬 싸게 나와(cu_price < unit_cost) 검증
    자체가 불가능해지므로, 이 경우는 반영하지 않고 "확인 필요"로만 보고한다
    - 검증 안 된 1차 계산값만으로 실제 가격을 덮어쓰는 게 이번에 사고로
    이어졌기 때문에, 반드시 CU 값으로 교차검증된 경우에만 적용한다."""
    keyword = search_keyword or menu_name
    try:
        candidate = product_ranking.search_coupang_product(keyword)
    except product_ranking.CoupangRateLimitError:
        return {"barcode": barcode, "name": menu_name, "ok": False, "reason": "RATE_LIMIT"}
    if not candidate or not candidate.get("price"):
        return {"barcode": barcode, "name": menu_name, "ok": False, "reason": "쿠팡 검색 결과 없음"}

    pack_qty = product_ranking._extract_coupang_pack_qty(candidate.get("product_name") or "") or 1
    unit_cost = candidate["price"] / pack_qty if pack_qty > 1 else candidate["price"]
    try:
        margin_price, margin_pct = catalog_margin.compute_recommended_price(
            unit_cost, ROUND_UNIT, *COUPANG_MARGIN_RANGE,
        )
    except ValueError:
        return {"barcode": barcode, "name": menu_name, "ok": False, "reason": f"매입가 계산 실패(unit_cost={unit_cost})"}

    cu = get_cu_price(barcode)
    if not cu:
        return {
            "barcode": barcode, "name": menu_name, "ok": False,
            "reason": "CU 매칭 없음 - 검증 불가",
            "unit_cost": round(unit_cost), "margin_price": margin_price,
        }
    if cu["price"] < unit_cost:
        return {
            "barcode": barcode, "name": menu_name, "ok": False,
            "reason": f"편의점가({cu['price']}원)가 매입가({round(unit_cost)}원)보다 낮음 - 매칭 의심",
            "unit_cost": round(unit_cost), "margin_price": margin_price, "cu_price": cu["price"],
        }

    final_price = min(margin_price, cu["price"])
    capped = final_price < margin_price
    today = datetime.now().date().isoformat()
    notes = f"쿠팡 매입가 {round(unit_cost)}원 기준 {margin_pct}% 마진 계산"
    if capped:
        notes += f" · 편의점가 {cu['price']}원으로 상한 적용"
    notes += f" · {today}"

    _apply_coupang_price(barcode, final_price, notes)
    return {
        "barcode": barcode, "name": menu_name, "ok": True,
        "unit_cost": round(unit_cost), "margin_pct": margin_pct, "margin_price": margin_price,
        "cu_price": cu["price"], "final_price": final_price, "capped": capped,
    }


def fix_coupang_prices() -> list[dict]:
    """추천판매가가 비어있거나(신규) 편의점판매가보다 비싸게 잘못 들어간
    (오적용/과거 사고) 쿠팡 분류 상품을 전부 찾아 compute_and_apply_coupang_price로
    바로잡는다. main.py 스케줄러가 주기 호출한다."""
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
        result = compute_and_apply_coupang_price(barcode, menu_name, search_keyword)
        results.append(result)
        if result.get("reason") == "RATE_LIMIT":
            break
    return results
