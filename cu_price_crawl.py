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
    카테고리 끝.
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
import product_ranking
from godomall_bot import _validate_barcode_checksum

CU_CATEGORY_CODES = {
    "10": "간편식사", "20": "즉석조리", "30": "과자류",
    "40": "아이스크림", "50": "식품", "60": "음료", "70": "생활용품",
}
CU_AJAX_URL = "https://cu.bgfretail.com/product/productAjax.do"
CU_ITEMS_PER_PAGE_STOP = 60  # 이 값(넉넉한 상한)까지 갔는데도 안 끝나면 이상 상황으로 보고 중단

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://cu.bgfretail.com/product/product.do?category=product&depth2=4&sf=N",
    "X-Requested-With": "XMLHttpRequest",
}

# 마진 계산은 도매몰(catalog_auto_import.py)과 동일한 기준으로 통일한다.
ROUND_UNIT = 100
MARGIN_RANGE = (40, 50)


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


def find_unpriced_coupang_candidates() -> list[dict]:
    """catalog_items 중 쿠팡(is_coupang=1) 분류인데 추천판매가가 비어있는
    상품을 찾아, 쿠팡 검색 API로 현재 매입가를 구하고 도매몰과 동일한 마진
    계산(catalog_margin.compute_recommended_price)으로 추천판매가 후보를
    만든다. 확정 반영은 하지 않고(catalog_items에 절대 쓰지 않음) CU
    편의점판매가와 비교한 결과만 돌려준다 - 최종 반영 여부는 관리자가
    텔레그램 보고를 보고 직접 판단한다(아이스모아 가격 차이 보고와 동일한
    철학)."""
    conn = db_conn.get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
        SELECT barcode, menu_name, search_keyword FROM catalog_items
        WHERE is_coupang = 1 AND (recommended_price IS NULL OR recommended_price = 0)
        """)
        rows = cur.fetchall()
    finally:
        conn.close()

    results = []
    for barcode, menu_name, search_keyword in rows:
        keyword = search_keyword or menu_name
        try:
            candidate = product_ranking.search_coupang_product(keyword)
        except product_ranking.CoupangRateLimitError:
            break
        if not candidate or not candidate.get("price"):
            results.append({
                "barcode": barcode, "name": menu_name, "ok": False,
                "reason": "쿠팡 검색 결과 없음",
            })
            continue

        pack_qty = product_ranking._extract_coupang_pack_qty(candidate.get("product_name") or "") or 1
        unit_cost = candidate["price"] / pack_qty if pack_qty > 1 else candidate["price"]
        try:
            recommended_price, margin_pct = catalog_margin.compute_recommended_price(
                unit_cost, ROUND_UNIT, *MARGIN_RANGE,
            )
        except ValueError:
            results.append({
                "barcode": barcode, "name": menu_name, "ok": False,
                "reason": f"매입가 계산 실패(unit_cost={unit_cost})",
            })
            continue

        cu = get_cu_price(barcode)
        results.append({
            "barcode": barcode, "name": menu_name,
            "ok": True,
            "unit_cost": round(unit_cost), "margin_pct": margin_pct,
            "computed_price": recommended_price,
            "cu_item_name": cu["item_name"] if cu else None,
            "cu_price": cu["price"] if cu else None,
            "valid": (cu["price"] >= recommended_price) if cu else None,
        })
    return results
