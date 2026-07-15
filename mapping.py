from dataclasses import dataclass
import pandas as pd


@dataclass
class CoupangCatalogItem:
    barcode: str
    menu_name: str
    search_keyword: str
    fixed_url: str
    pack_qty: int
    min_order: int
    notes: str
    is_coupang: int
    icecream_box_qty: int = 1
    category: str = ""
    menu_code: str = ""
    recommended_price: int = 0


def _safe_str(v) -> str:
    if pd.isna(v):
        return ""
    s = str(v).strip()
    if s.lower() in ("nan", "none", "null"):
        return ""
    return s


def _normalize_barcode(v) -> str:
    """
    엑셀에서 숫자로 읽힌 바코드를 문자열 숫자로 정규화
    예:
    8801104123280.0 -> 8801104123280
    '8801104123280' -> 8801104123280
    """
    if pd.isna(v):
        return ""

    s = str(v).strip()
    if not s:
        return ""

    # 12345.0 같은 형태 제거
    if s.endswith(".0"):
        s = s[:-2]

    # 공백 제거
    s = s.replace(" ", "")

    return s


def _safe_int(v, default=0) -> int:
    try:
        if pd.isna(v):
            return default
        return int(float(v))
    except Exception:
        return default


def load_coupang_catalog_xlsx(path: str) -> dict:
    """
    쿠팡 카탈로그 엑셀 로드
    barcode 기준 dictionary 생성
    """
    df = pd.read_excel(path, engine="openpyxl")

    catalog = {}

    for _, row in df.iterrows():
        barcode = _normalize_barcode(row.get("barcode"))

        if not barcode:
            continue

        catalog[barcode] = CoupangCatalogItem(
            barcode=barcode,
            menu_name=_safe_str(row.get("menu_name")),
            search_keyword=_safe_str(row.get("search_keyword")),
            fixed_url=_safe_str(row.get("fixed_url")),
            pack_qty=max(_safe_int(row.get("pack_qty"), 1), 1),
            min_order=max(_safe_int(row.get("min_order"), 1), 1),
            notes=_safe_str(row.get("notes")),
            is_coupang=_safe_int(row.get("is_coupang"), 0),
            icecream_box_qty=_safe_int(row.get("icecream_box_qty"), 0),
            category=_safe_str(row.get("category")),
            menu_code=_safe_str(row.get("menu_code")),
            recommended_price=_safe_int(row.get("recommended_price"), 0),
        )

    return catalog


def select_representative_item(order_items: list, catalog: dict):
    """
    대표상품 선택 기준:
    1. 쿠팡상품
    2. search_keyword 존재
    3. 추천발주량 큰 상품
    """
    candidates = []

    for item in order_items:
        barcode = _normalize_barcode(item.get("바코드번호", ""))

        if barcode not in catalog:
            continue

        cat = catalog[barcode]

        if cat.is_coupang != 1:
            continue

        if not cat.search_keyword:
            continue

        qty = int(item.get("추천발주량_포장반영", item.get("추천발주량", 0)) or 0)

        candidates.append((qty, cat))

    if not candidates:
        return None

    candidates.sort(reverse=True, key=lambda x: x[0])
    return candidates[0][1]