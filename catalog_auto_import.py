# catalog_auto_import.py
"""도매몰 전체 상품을 바코드까지 크롤링해서 catalog_items에 자동 등록한다.
현재 지원: 고도몰 계열(과자생각/ccdome, 삼봉몰/3bong, 현동몰/hdinter -
godomall_bot 사용), 자체제작 플랫폼(야미몰/yamimall, 또요몰/douyou -
yamimall_bot 사용). 플랫폼마다 바코드가 저장된 필드명과 크롤러 시그니처가
달라서 _crawl_vendor_products에서 vendor_id로 분기한다.

무마켓(moomarket)은 상품 상세페이지에 바코드/모델명 정보 자체가 없어서(실측
확인 - 숨겨진 tab-panel까지 다 뒤져도 없고, 고객 Q&A 게시판에 "바코드
문의"라는 글까지 있음) 이 방식으로는 지원 불가능하다. DEFAULT_VENDORS에서
제외한다.

추천판매가 결정 규칙(한 바코드 상품 기준, 우선순위 순):
  1) 상품명 맨 앞에 "(1500)상품명" 식으로 판매가가 이미 박혀있으면 그 값을
     그대로 쓴다(야미몰 계열 상품명 관례) - "명시적(explicit)" 값으로 취급.
  2) 도매처가 상세페이지에 이미 "권장소비자가"를 제공하면 그 값을 그대로
     쓴다(실측: 또요몰 상품 상당수에 이미 채워져 있음) - 이것도 "명시적" 값.
  3) 위 둘 다 없으면 "총판매가(구매 단위 가격) ÷ 1타 개수"로 낱개 원가를
     구하고, catalog_margin.compute_recommended_price로 40~50% 마진 범위에서
     가장 깔끔하게 반올림되는 값을 쓴다 - "계산값(computed)"으로 취급.

같은 바코드 상품이 여러 도매몰에 동시에 있을 경우(사용자 확인된 정책):
  - "명시적" 값이 하나라도 있으면 명시적 값들끼리만 비교하고, 계산값은
    전부 무시한다(도매처가 직접 준 값이 우리 추정치보다 신뢰도가 높음).
  - 후보가 여러 개(명시적끼리, 또는 명시적이 하나도 없어 계산값끼리) 남으면
    더 비싼 금액을 채택한다.

기존 카탈로그와의 병합 규칙(사용자 확인됨):
  - DB에 아예 없는 바코드 -> 새로 추가 (is_coupang=2 "도매몰" 카테고리)
  - 이미 있는데 recommended_price가 비어있음(0/NULL) -> 그 값만 채움
    (상품명/카테고리 등 관리자가 이미 손댔을 수 있는 다른 필드는 안 건드림)
  - 이미 있고 값이 채워져 있는데, 이번 크롤링 결과가 도매처가 "명시"한
    추천판매가(권장소비자가/상품명에 박힌 가격)라면 -> 그 명시가로 덮어쓴다.
    도매처가 직접 준 값이 우리 계산값보다 신뢰도가 높다는 판단(사용자
    요청). 기존 값이 우리 계산 로직으로 과다하게 들어간 경우를 바로잡기
    위함이다. 명시가끼리 여러 개면 위 규칙대로 최고가를 쓴다.
  - 이미 있고 값이 채워져 있는데, 이번 결과가 "계산값"뿐이라면 -> 안 건드리고
    건너뛴다(관리자가 손댔을 수 있는 값을 추정치로 덮지 않는다).

실행:
  python catalog_auto_import.py ccdome [--limit 20]   # 도매처 하나만
  python catalog_auto_import.py --all [--limit 20]    # DEFAULT_VENDORS 전체
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

# 주기적(예: 주 1회) 자동등록 대상 - 바코드를 확인할 수 있는 도매처만.
# 과자생각(ccdome)/삼봉몰(3bong)/또요몰(douyou)/야미몰(yamimall).
# moomarket은 바코드 정보 자체가 없어서 제외(위 모듈 설명 참고). hdinter
# (현동몰)는 아직 요청받지 않아 기본 목록엔 안 넣되 CLI 개별 실행은 가능.
DEFAULT_VENDORS = ("ccdome", "3bong", "douyou", "yamimall")

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


def _clean_menu_name(raw_name: str) -> str:
    return _PACK_SUFFIX_RE.sub("", raw_name or "").strip()


def _resolve_candidate(product: dict, vendor_name: str) -> dict | None:
    """상품 하나를 (추천판매가, 명시적 여부, 근거 메모) 후보로 정리한다.
    가격을 구할 수 없으면(포장 정보 파싱 실패 등) None."""
    name = product.get("name") or ""
    case_price = product.get("case_price")
    unit_qty = product.get("unit_qty")
    vendor_recommended = product.get("recommended_price")

    explicit_match = _EXPLICIT_PRICE_RE.match(name.strip())
    if explicit_match:
        return {
            "price": int(explicit_match.group(1)),
            "is_explicit": True,
            "reason": "상품명에 명시된 판매가",
        }
    if vendor_recommended:
        return {
            "price": int(vendor_recommended),
            "is_explicit": True,
            "reason": "도매처가 제공한 권장소비자가",
        }
    if case_price and unit_qty and unit_qty and unit_qty > 0:
        unit_cost = case_price / unit_qty
        try:
            price, margin_pct = compute_recommended_price(unit_cost, ROUND_UNIT, *MARGIN_RANGE)
        except ValueError:
            return None
        return {
            "price": price,
            "is_explicit": False,
            "reason": f"박스가 {case_price}원÷{unit_qty}개, 마진 {margin_pct}% 자동적용",
        }
    return None


def _pick_winner(candidates: list[dict]) -> dict:
    """같은 바코드에 후보가 여럿이면(여러 도매몰) 명시적 값 우선, 그중/혹은
    계산값끼리는 더 비싼 쪽을 채택한다(사용자 확인된 정책)."""
    explicit = [c for c in candidates if c["is_explicit"]]
    pool = explicit if explicit else candidates
    return max(pool, key=lambda c: c["price"])


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


def _overwrite_price(barcode: str, recommended_price: int, notes: str) -> None:
    """이미 값이 있는 바코드를, 도매처가 "명시"한 추천판매가로 덮어쓴다.
    recommended_price와 notes만 갱신하고 pack_qty/상품명 등 관리자가 손댔을
    수 있는 다른 필드는 건드리지 않는다."""
    now = datetime.now().isoformat(timespec="seconds")
    conn = db_conn.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE catalog_items SET recommended_price = ?, notes = ?, updated_at = ? WHERE barcode = ?",
            (recommended_price, notes, now, barcode),
        )
        conn.commit()
    finally:
        conn.close()


def _collect_vendor_candidates(vendor_id: str, limit: int | None) -> list[dict]:
    """도매처 하나를 크롤링해서, 바코드별 후보 dict 리스트를 반환한다(아직 DB에
    쓰지 않음 - 다른 도매처 결과와 합쳐서 비교해야 하므로)."""
    meta = vendors.VENDORS[vendor_id]
    creds = vendors.get_vendor_credentials(vendor_id)
    if not creds:
        raise RuntimeError(f"{vendor_id} 로그인 정보가 없습니다 (vendor_credentials에 등록 필요)")
    login_id, login_pwd = creds

    print(f"[CATALOG_IMPORT] {meta['name']} 크롤링 시작 - 상품마다 상세페이지를 열어야 해서 오래 걸립니다.")
    products = _crawl_vendor_products(vendor_id, meta, login_id, login_pwd, limit)
    print(f"[CATALOG_IMPORT] {meta['name']} 유효한 바코드가 확인된 상품 {len(products)}개 수집 완료")

    results = []
    for p in products:
        resolved = _resolve_candidate(p, meta["name"])
        if resolved is None:
            results.append({
                "barcode": p["barcode"], "name": p.get("name") or "", "vendor_name": meta["name"],
                "unresolved": True,
                "reason": f"case_price={p.get('case_price')}, unit_qty={p.get('unit_qty')}",
            })
            continue
        results.append({
            "barcode": p["barcode"],
            "name": p.get("name") or "",
            "vendor_name": meta["name"],
            "unit_qty": p.get("unit_qty") or 1,
            "product_url": p.get("product_url") or "",
            "price": resolved["price"],
            "is_explicit": resolved["is_explicit"],
            "reason": resolved["reason"],
        })
    return results


def _write_winners(by_barcode: dict[str, list[dict]]) -> dict:
    summary: dict[str, list[dict]] = {
        "added": [], "updated": [], "overwritten": [],
        "skipped_existing_price": [], "skipped_parse_fail": [],
    }

    for barcode, candidates in by_barcode.items():
        resolvable = [c for c in candidates if not c.get("unresolved")]
        if not resolvable:
            first = candidates[0]
            summary["skipped_parse_fail"].append({
                "barcode": barcode, "name": first["name"], "reason": first["reason"],
            })
            continue

        winner = _pick_winner(resolvable)
        clean_name = _clean_menu_name(winner["name"])

        if len(resolvable) > 1:
            others = ", ".join(
                f"{c['vendor_name']} {c['price']}원{'(명시)' if c['is_explicit'] else ''}"
                for c in resolvable if c is not winner
            )
            compare_note = f" · 비교 대상: {others} 중 최고가 채택"
        else:
            compare_note = ""

        notes = (
            f"{winner['vendor_name']} 자동크롤링 · {winner['reason']}"
            f"{compare_note} · {datetime.now().date().isoformat()}"
        )

        existing_price = _get_existing_recommended_price(barcode)
        if existing_price is None:
            _insert_new_item(
                barcode, clean_name, winner.get("unit_qty", 1), winner["price"], notes,
                winner.get("product_url", ""),
            )
            summary["added"].append({"barcode": barcode, "name": clean_name, "recommended_price": winner["price"]})
        elif existing_price == 0:
            _fill_empty_price(barcode, winner.get("unit_qty", 1), winner["price"], notes)
            summary["updated"].append({"barcode": barcode, "name": clean_name, "recommended_price": winner["price"]})
        elif winner["is_explicit"] and winner["price"] != existing_price:
            # 이미 값이 있어도, 도매처가 "명시"한 추천판매가면 그 값으로 덮어쓴다
            # (계산값이면 아래 else로 빠져서 건너뜀).
            overwrite_notes = (
                f"{winner['vendor_name']} 도매 명시 추천판매가로 갱신"
                f"(기존 {existing_price}원 → {winner['price']}원) · {winner['reason']}"
                f"{compare_note} · {datetime.now().date().isoformat()}"
            )
            _overwrite_price(barcode, winner["price"], overwrite_notes)
            summary["overwritten"].append({
                "barcode": barcode, "name": clean_name,
                "old_price": existing_price, "recommended_price": winner["price"],
                "vendor": winner["vendor_name"],
            })
        else:
            summary["skipped_existing_price"].append({
                "barcode": barcode, "name": clean_name, "existing_price": existing_price,
                "would_be": winner["price"], "is_explicit": winner["is_explicit"],
            })

    return summary


def import_all_vendors(vendor_ids: tuple[str, ...] = DEFAULT_VENDORS, limit: int | None = None) -> dict:
    """도매처 여러 곳을 전부 크롤링해서, 바코드가 겹치는 상품은(같은 바코드를
    본 도매처들끼리) 비교해서(명시적 값 우선 -> 더 비싼 쪽) 결정한 값만 DB에
    반영한다.

    도매처 하나가 끝날 때마다 그 도매처가 새로 가져온 바코드만 바로 DB에
    반영(체크포인트)하고, 도매처 하나가 실패해도(크롤링 오류, DB 연결 끊김
    등) 남은 도매처는 계속 진행한다 - 예전에는 전체 도매처가 다 끝나야
    한 번에 DB에 썼기 때문에, 마지막 도매처에서 죽으면 몇 시간짜리 크롤링
    결과가 통째로 날아갔다(실제로 발생한 사고)."""
    by_barcode: dict[str, list[dict]] = {}
    summary: dict[str, list[dict]] = {
        "added": [], "updated": [], "overwritten": [],
        "skipped_existing_price": [], "skipped_parse_fail": [], "failed_vendors": [],
    }

    for vendor_id in vendor_ids:
        try:
            candidates = _collect_vendor_candidates(vendor_id, limit)
        except Exception as e:
            print(f"[CATALOG_IMPORT] {vendor_id} 크롤링 실패 - 건너뛰고 나머지 도매처를 계속 진행합니다: {e}")
            summary["failed_vendors"].append({"vendor_id": vendor_id, "error": str(e)})
            continue

        touched_barcodes = set()
        for candidate in candidates:
            by_barcode.setdefault(candidate["barcode"], []).append(candidate)
            touched_barcodes.add(candidate["barcode"])

        # 이번 도매처가 새로 건드린 바코드만 넘긴다(그 바코드를 먼저 본 다른
        # 도매처의 후보도 by_barcode에 이미 같이 들어있어 비교는 그대로 됨).
        # 이미 끝난 바코드를 매번 다시 쓰지 않아 불필요한 DB 재작성도 없다.
        touched = {b: by_barcode[b] for b in touched_barcodes}
        try:
            vendor_summary = _write_winners(touched)
        except Exception as e:
            print(f"[CATALOG_IMPORT] {vendor_id} 결과 DB 반영 실패 - 이 도매처 결과가 유실됐을 수 있습니다: {e}")
            summary["failed_vendors"].append({"vendor_id": vendor_id, "error": f"DB 반영 실패: {e}"})
            continue

        for key in ("added", "updated", "overwritten", "skipped_existing_price", "skipped_parse_fail"):
            summary[key].extend(vendor_summary[key])

    return summary


def import_vendor_catalog(vendor_id: str, limit: int | None = None) -> dict:
    """도매처 하나만 대상으로 한다(수동 테스트/단독 실행용) - 내부적으로는
    import_all_vendors와 완전히 같은 경로를 탄다."""
    return import_all_vendors((vendor_id,), limit=limit)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="도매몰 카탈로그 자동 등록")
    parser.add_argument("vendor_id", nargs="?", help="예: ccdome (생략하고 --all 쓰면 DEFAULT_VENDORS 전체)")
    parser.add_argument("--all", action="store_true", help=f"DEFAULT_VENDORS({', '.join(DEFAULT_VENDORS)}) 전체 실행")
    parser.add_argument(
        "--vendors", type=str, default=None,
        help="콤마로 구분한 도매처 목록만 실행 (예: --vendors douyou,yamimall) - "
             "특정 도매처가 일시적으로 막혀있을 때 그것만 빼고 돌리는 용도.",
    )
    parser.add_argument("--limit", type=int, default=None, help="테스트용 상품 수 제한(도매처별)")
    args = parser.parse_args()

    if args.vendors:
        vendor_ids = tuple(v.strip() for v in args.vendors.split(",") if v.strip())
        result = import_all_vendors(vendor_ids, limit=args.limit)
        label = ", ".join(vendor_ids)
    elif args.all:
        result = import_all_vendors(limit=args.limit)
        label = "전체(" + ", ".join(DEFAULT_VENDORS) + ")"
    elif args.vendor_id:
        result = import_vendor_catalog(args.vendor_id, limit=args.limit)
        label = args.vendor_id
    else:
        parser.error("vendor_id를 지정하거나 --all을 붙여주세요")

    print(f"\n=== {label} 카탈로그 자동 등록 결과 ===")
    print(f"신규 추가: {len(result['added'])}개")
    print(f"빈 값 채움: {len(result['updated'])}개")
    print(f"도매 명시가로 덮어씀: {len(result['overwritten'])}개")
    for o in result["overwritten"]:
        print(f"   {o['barcode']} {o['name']}: {o['old_price']}원 -> {o['recommended_price']}원 ({o['vendor']})")
    if result.get("failed_vendors"):
        print(f"실패한 도매처: {len(result['failed_vendors'])}개 (그전까지 완료된 도매처 결과는 위에 반영됨)")
        for f in result["failed_vendors"]:
            print(f"   {f['vendor_id']}: {f['error']}")
    print(f"기존 값 있어 건너뜀(계산값): {len(result['skipped_existing_price'])}개")
    print(f"파싱 실패로 건너뜀: {len(result['skipped_parse_fail'])}개")
