# catalog_auto_import.py
"""도매몰 전체 상품을 바코드까지 크롤링해서 catalog_items에 자동 등록한다.
현재 지원: 고도몰 계열(과자생각/ccdome, 삼봉몰/3bong, 현동몰/hdinter -
godomall_bot 사용), 자체제작 플랫폼(야미몰/yamimall, 또요몰/douyou -
yamimall_bot 사용). 플랫폼마다 바코드가 저장된 필드명과 크롤러 시그니처가
달라서 _crawl_vendor_products에서 vendor_id로 분기한다.

추천판매가 결정 규칙(우선순위 순):
  1) 상품명 맨 앞에 "(1500)상품명" 식으로 판매가가 이미 박혀있으면 그 값을
     그대로 쓴다(야미몰 계열 상품명 관례).
  2) 도매처가 상세페이지에 이미 "권장소비자가"를 제공하면 그 값을 그대로
     쓴다(실측: 또요몰 상품 상당수에 이미 채워져 있음 - 우리가 추정하는
     것보다 신뢰도 높은 실제 값이라 최우선으로 대접해야 함).
  3) 둘 다 없으면 "총판매가(구매 단위 가격) ÷ 1타 개수"로 낱개 원가를 구하고,
     catalog_margin.compute_recommended_price로 40~50% 마진 범위에서 가장
     깔끔하게 반올림되는 값을 추천판매가로 쓴다.

기존 카탈로그와의 병합 규칙(사용자 확인됨):
  - DB에 아예 없는 바코드 -> 새로 추가 (is_coupang=2 "도매몰" 카테고리)
  - 이미 있는데 recommended_price가 비어있음(0/NULL) -> 그 값만 채움
    (상품명/카테고리 등 관리자가 이미 손댔을 수 있는 다른 필드는 안 건드림)
  - 이미 있고 recommended_price가 채워져 있음 -> 절대 건드리지 않고 건너뜀

실행: python catalog_auto_import.py ccdome [--limit 20]
"""
import argparse
import re
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

import db_conn
import godomall_bot
import vendors
import yamimall_bot
from catalog_margin import compute_recommended_price

ROUND_UNIT = 100
MARGIN_RANGE = (40, 50)

_GODOMALL_VENDORS = ("ccdome", "3bong", "hdinter")
_CUSTOM_PLATFORM_VENDORS = ("yamimall", "douyou")


def _crawl_vendor_products(vendor_id: str, meta: dict, login_id: str, login_pwd: str, limit: int | None) -> list[dict]:
    if vendor_id in _GODOMALL_VENDORS:
        return godomall_bot.crawl_catalog_with_barcode(
            meta["base_url"], login_id, login_pwd, meta["catalog_category_code"], detail_limit=limit,
        )
    if vendor_id in _CUSTOM_PLATFORM_VENDORS:
        return yamimall_bot.crawl_catalog_with_barcode(
            login_id, login_pwd, base_url=meta["base_url"],
            category_codes=meta.get("catalog_category_code"), detail_limit=limit,
        )
    raise ValueError(f"{vendor_id}는 아직 바코드 자동등록을 지원하지 않습니다")

# 야미몰 스타일 "(1500)상품명" - 상품명 맨 앞에 이미 판매가가 박혀있는 경우.
_EXPLICIT_PRICE_RE = re.compile(r"^\((\d{3,6})\)")
# 상품명 끝에 붙는 "(1타 20개입)"/"(1박스 46개입)" 류 포장단위 표기 - 관리용
# pack_qty 컬럼에 이미 구조화해서 저장하므로 화면에 보이는 menu_name에서는
# 지운다("박스?"로 godomall_bot._extract_unit_qty와 동일한 표기 두 가지를
# 다 잡는다 - 안 그러면 "박스" 표기 상품만 이 문구가 이름에 그대로 남는다).
_PACK_SUFFIX_RE = re.compile(r"\s*\(1(?:타|묶음|박스?)\s*\d+\s*개입\)\s*$")

# 이 스크립트가 만들거나 채운 상품은 전부 "도매몰" 카테고리(main.py의
# is_coupang 필드 정의: 0=아이스크림,1=쿠팡,2=도매몰,3=문구완구,99=미분류).
CATEGORY_WHOLESALE = 2


def _clean_menu_name(raw_name: str) -> str:
    return _PACK_SUFFIX_RE.sub("", raw_name or "").strip()


def _get_existing_recommended_price(barcode: str) -> int | None:
    """바코드가 카탈로그에 없으면 None, 있으면 현재 recommended_price(0일 수도
    있음)를 반환한다 - "없음"과 "0원으로 저장돼있음"을 구분해야 한다."""
    conn = db_conn.get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT recommended_price FROM catalog_items WHERE barcode = ?", (barcode,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _insert_new_item(barcode: str, menu_name: str, pack_qty: int, recommended_price: int, notes: str, fixed_url: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn = db_conn.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO catalog_items
                (barcode, menu_name, search_keyword, fixed_url, pack_qty, min_order, notes,
                 is_coupang, icecream_box_qty, category, menu_code, recommended_price, updated_at)
            VALUES (?, ?, '', ?, ?, 1, ?, ?, 0, '', '', ?, ?)
            """,
            (barcode, menu_name, fixed_url, pack_qty, notes, CATEGORY_WHOLESALE, recommended_price, now),
        )
        conn.commit()
    finally:
        conn.close()


def _fill_empty_price(barcode: str, pack_qty: int, recommended_price: int, notes: str) -> None:
    """이미 있는 바코드인데 recommended_price가 비어있을 때만 그 필드들만 채운다.
    WHERE 조건에 재확인을 넣어서, 조회와 갱신 사이에 다른 경로(관리자 웹 수정
    등)로 값이 채워졌더라도 덮어쓰지 않는다."""
    now = datetime.now().isoformat(timespec="seconds")
    conn = db_conn.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE catalog_items
            SET recommended_price = ?, pack_qty = ?, notes = ?, updated_at = ?
            WHERE barcode = ? AND (recommended_price IS NULL OR recommended_price = 0)
            """,
            (recommended_price, pack_qty, notes, now, barcode),
        )
        conn.commit()
    finally:
        conn.close()


def import_vendor_catalog(vendor_id: str, limit: int | None = None) -> dict:
    meta = vendors.VENDORS[vendor_id]
    creds = vendors.get_vendor_credentials(vendor_id)
    if not creds:
        raise RuntimeError(f"{vendor_id} 로그인 정보가 없습니다 (vendor_credentials에 등록 필요)")
    login_id, login_pwd = creds

    print(f"[CATALOG_IMPORT] {meta['name']} 크롤링 시작 - 상품마다 상세페이지를 열어야 해서 오래 걸립니다.")
    products = _crawl_vendor_products(vendor_id, meta, login_id, login_pwd, limit)
    print(f"[CATALOG_IMPORT] {meta['name']} 유효한 바코드가 확인된 상품 {len(products)}개 수집 완료")

    summary: dict[str, list[dict]] = {
        "added": [], "updated": [], "skipped_existing_price": [], "skipped_parse_fail": [],
    }

    for p in products:
        barcode = p["barcode"]
        name = p.get("name") or ""
        case_price = p.get("case_price")
        unit_qty = p.get("unit_qty")

        vendor_recommended = p.get("recommended_price")
        explicit = _EXPLICIT_PRICE_RE.match(name.strip())
        if explicit:
            recommended_price = int(explicit.group(1))
            margin_note = "상품명에 명시된 판매가 사용"
        elif vendor_recommended:
            recommended_price = int(vendor_recommended)
            margin_note = "도매처가 제공한 권장소비자가 사용"
        elif case_price and unit_qty and unit_qty > 0:
            unit_cost = case_price / unit_qty
            try:
                recommended_price, margin_pct = compute_recommended_price(unit_cost, ROUND_UNIT, *MARGIN_RANGE)
            except ValueError as e:
                summary["skipped_parse_fail"].append({"barcode": barcode, "name": name, "reason": str(e)})
                continue
            margin_note = f"박스가 {case_price}원÷{unit_qty}개, 마진 {margin_pct}% 자동적용"
        else:
            summary["skipped_parse_fail"].append({
                "barcode": barcode, "name": name,
                "reason": f"case_price={case_price}, unit_qty={unit_qty}",
            })
            continue

        clean_name = _clean_menu_name(name)
        notes = f"{meta['name']} 자동크롤링 · {margin_note} · {datetime.now().date().isoformat()}"

        existing_price = _get_existing_recommended_price(barcode)
        if existing_price is None:
            _insert_new_item(barcode, clean_name, unit_qty or 1, recommended_price, notes, p.get("product_url") or "")
            summary["added"].append({"barcode": barcode, "name": clean_name, "recommended_price": recommended_price})
        elif existing_price == 0:
            _fill_empty_price(barcode, unit_qty or 1, recommended_price, notes)
            summary["updated"].append({"barcode": barcode, "name": clean_name, "recommended_price": recommended_price})
        else:
            summary["skipped_existing_price"].append({
                "barcode": barcode, "name": clean_name, "existing_price": existing_price,
            })

    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="도매몰 카탈로그 자동 등록")
    parser.add_argument("vendor_id", help="예: ccdome")
    parser.add_argument("--limit", type=int, default=None, help="테스트용 상품 수 제한")
    args = parser.parse_args()

    result = import_vendor_catalog(args.vendor_id, limit=args.limit)
    print(f"\n=== {args.vendor_id} 카탈로그 자동 등록 결과 ===")
    print(f"신규 추가: {len(result['added'])}개")
    print(f"빈 값 채움: {len(result['updated'])}개")
    print(f"기존 값 있어 건너뜀: {len(result['skipped_existing_price'])}개")
    print(f"파싱 실패로 건너뜀: {len(result['skipped_parse_fail'])}개")
