import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pandas as pd

import db_conn

BASE_DIR = Path(__file__).resolve().parent
SEED_CATALOG_XLSX_PATH = BASE_DIR / "coupang_catalog_sample_2.xlsx"


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


def _parse_catalog_dataframe(df) -> dict:
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


def load_coupang_catalog_xlsx(path: str) -> dict:
    """
    쿠팡 카탈로그 엑셀 로드
    barcode 기준 dictionary 생성
    """
    df = pd.read_excel(path, engine="openpyxl")
    return _parse_catalog_dataframe(df)


def parse_catalog_xlsx_bytes(file_bytes: bytes) -> list["CoupangCatalogItem"]:
    """관리자가 업로드한 엑셀 바이트를 파싱하고 최소한의 유효성 검사를 한다
    (카탈로그 업로드 API에서 씀 - 잘못된 파일로 전체 카탈로그를 실수로
    날려먹지 않도록 방어)."""
    df = pd.read_excel(io.BytesIO(file_bytes), engine="openpyxl")
    required_cols = {"barcode", "menu_name", "is_coupang"}
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"필수 컬럼이 없습니다: {', '.join(sorted(missing))}")

    catalog = _parse_catalog_dataframe(df)
    if len(catalog) < 50:
        raise ValueError(f"유효한 상품이 {len(catalog)}개뿐입니다 - 잘못된 파일일 수 있어 반영하지 않았습니다.")
    return list(catalog.values())


# --- DB 기반 카탈로그: 이제 이게 실제 사용되는 유일한 소스다. 엑셀 파일은
# 최초 시드(seed)용으로만 쓰인다 - Render는 배포/재시작마다 로컬 파일을
# git 저장소 내용으로 되돌리므로(ephemeral filesystem), 관리자가 웹에서
# 수정/업로드한 내용이 남아있으려면 DB(영구 저장소)에 있어야 한다. ---

_CATALOG_DB_COLUMNS = [
    "barcode", "menu_name", "search_keyword", "fixed_url", "pack_qty",
    "min_order", "notes", "is_coupang", "icecream_box_qty", "category",
    "menu_code", "recommended_price",
]


def init_catalog_table() -> None:
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS catalog_items (
        barcode TEXT PRIMARY KEY,
        menu_name TEXT,
        search_keyword TEXT,
        fixed_url TEXT,
        pack_qty INTEGER,
        min_order INTEGER,
        notes TEXT,
        is_coupang INTEGER,
        icecream_box_qty INTEGER,
        category TEXT,
        menu_code TEXT,
        recommended_price INTEGER,
        updated_at TEXT
    )
    """)
    conn.commit()

    cur.execute("SELECT COUNT(*) FROM catalog_items")
    count = cur.fetchone()[0]
    conn.close()

    if count == 0 and SEED_CATALOG_XLSX_PATH.exists():
        seed_catalog = load_coupang_catalog_xlsx(str(SEED_CATALOG_XLSX_PATH))
        if seed_catalog:
            replace_catalog_from_items(list(seed_catalog.values()))
            print(f"[MAPPING] 카탈로그 DB 최초 시드 완료: {len(seed_catalog)}개")


# --- 미분류 대기열: 지점(수동/자동 리포트 어디서든)에서 카탈로그에 없는
# 바코드를 만나면 여기 쌓인다 - 전 지점 공용 하나로 모아서, 관리자가
# /admin/barcode-catalog에서 한 번에 분류할 수 있게 한다. ---

def init_unclassified_queue_table() -> None:
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS unclassified_queue (
        barcode TEXT PRIMARY KEY,
        item_name TEXT,
        store_ids TEXT,
        occurrence_count INTEGER NOT NULL DEFAULT 1,
        first_seen_at TEXT,
        last_seen_at TEXT
    )
    """)
    conn.commit()
    conn.close()


def queue_unclassified_item(barcode: str, item_name: str, store_id: str) -> None:
    """카탈로그에 없는 바코드를 리포트 생성 중 만나면 호출한다. 이미 대기열에
    있으면 발생 횟수/최근 발견 시각/지점 목록만 갱신하고(중복 안 쌓임),
    처음이면 새로 추가한다."""
    barcode = (barcode or "").strip()
    if not barcode:
        return
    now = datetime.now().isoformat(timespec="seconds")
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute("SELECT store_ids, occurrence_count FROM unclassified_queue WHERE barcode = ?", (barcode,))
    row = cur.fetchone()
    if row:
        stores = {s for s in (row[0] or "").split(",") if s}
        stores.add(store_id)
        cur.execute("""
        UPDATE unclassified_queue SET item_name = ?, store_ids = ?, occurrence_count = ?, last_seen_at = ?
        WHERE barcode = ?
        """, (item_name or "", ",".join(sorted(stores)), row[1] + 1, now, barcode))
    else:
        cur.execute("""
        INSERT INTO unclassified_queue (barcode, item_name, store_ids, occurrence_count, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, 1, ?, ?)
        """, (barcode, item_name or "", store_id, now, now))
    conn.commit()
    conn.close()


def list_unclassified_queue(limit: int = 200) -> list[dict]:
    """이미 카탈로그에 등록된(관리자가 분류 완료한) 바코드는 걸러서 안
    보여준다 - upsert_catalog_item이 대기열에서 지워주긴 하지만, 혹시 놓친
    경우를 대비해 조회 시점에도 한 번 더 확인한다."""
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT barcode, item_name, store_ids, occurrence_count, first_seen_at, last_seen_at
    FROM unclassified_queue ORDER BY occurrence_count DESC, last_seen_at DESC LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()

    catalog = load_catalog()
    result = []
    for barcode, item_name, store_ids, occurrence_count, first_seen_at, last_seen_at in rows:
        if barcode in catalog:
            continue
        store_count = len([s for s in (store_ids or "").split(",") if s])
        result.append({
            "barcode": barcode, "item_name": item_name, "store_count": store_count,
            "occurrence_count": occurrence_count, "first_seen_at": first_seen_at,
            "last_seen_at": last_seen_at,
        })
    return result


def dismiss_unclassified_item(barcode: str) -> bool:
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM unclassified_queue WHERE barcode = ?", (barcode,))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def _item_values(item: "CoupangCatalogItem", now: str) -> tuple:
    return (
        item.barcode, item.menu_name, item.search_keyword, item.fixed_url,
        item.pack_qty, item.min_order, item.notes, item.is_coupang,
        item.icecream_box_qty, item.category, item.menu_code,
        item.recommended_price, now,
    )


def replace_catalog_from_items(items: list["CoupangCatalogItem"]) -> int:
    """카탈로그 전체를 통째로 교체한다(엑셀 업로드 반영용) - 지우고 새로
    넣는 걸 한 트랜잭션으로 묶어서, 중간에 실패해도 절반만 반영되지 않게 한다."""
    now = datetime.now().isoformat(timespec="seconds")
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM catalog_items")
    for item in items:
        cur.execute(f"""
        INSERT INTO catalog_items ({", ".join(_CATALOG_DB_COLUMNS)}, updated_at)
        VALUES ({", ".join(["?"] * (len(_CATALOG_DB_COLUMNS) + 1))})
        """, _item_values(item, now))
    conn.commit()
    conn.close()
    return len(items)


def upsert_catalog_item(item: "CoupangCatalogItem") -> None:
    """한 상품만 추가/수정한다(관리자 폼에서 한 줄 편집)."""
    now = datetime.now().isoformat(timespec="seconds")
    conn = db_conn.get_conn()
    cur = conn.cursor()
    set_clause = ", ".join(f"{c}=excluded.{c}" for c in _CATALOG_DB_COLUMNS if c != "barcode")
    cur.execute(f"""
    INSERT INTO catalog_items ({", ".join(_CATALOG_DB_COLUMNS)}, updated_at)
    VALUES ({", ".join(["?"] * (len(_CATALOG_DB_COLUMNS) + 1))})
    ON CONFLICT(barcode) DO UPDATE SET {set_clause}, updated_at=excluded.updated_at
    """, _item_values(item, now))
    conn.commit()
    conn.close()
    if item.is_coupang != 99:
        dismiss_unclassified_item(item.barcode)


def delete_catalog_item(barcode: str) -> bool:
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM catalog_items WHERE barcode = ?", (barcode,))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def list_catalog_items(limit: int = 20) -> list[dict]:
    """관리 페이지 기본 목록용 - 최근 수정된 순으로 일부만(전체 카탈로그가
    1000개 넘게 쌓여도 목록이 무거워지지 않게)."""
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute(f"""
    SELECT {", ".join(_CATALOG_DB_COLUMNS)} FROM catalog_items
    ORDER BY updated_at DESC LIMIT ?
    """, (limit,))
    rows = cur.fetchall()
    conn.close()
    return [dict(zip(_CATALOG_DB_COLUMNS, r)) for r in rows]


def catalog_item_count() -> int:
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM catalog_items")
    count = cur.fetchone()[0]
    conn.close()
    return count


def load_catalog() -> dict:
    """DB에서 카탈로그를 읽는다 - store_reports/main.py의 발주 분류, 바코드
    검색 등 이제 전부 이 함수 하나만 쓴다(엑셀 파일 직접 읽기는 최초 시드
    때만 쓰이고 그 뒤로는 안 쓰임)."""
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute(f"SELECT {', '.join(_CATALOG_DB_COLUMNS)} FROM catalog_items")
    rows = cur.fetchall()
    conn.close()

    result = {}
    for r in rows:
        d = dict(zip(_CATALOG_DB_COLUMNS, r))
        # NULL 방어 - 업로드/단건편집 어느 경로든 항상 값을 채우지만, 혹시
        # 모를 NULL이 있으면 apply_case_rule 등 하위 로직이 int 나눗셈에서
        # 죽지 않게 여기서 한 번 더 정규화한다.
        d["menu_name"] = d["menu_name"] or ""
        d["search_keyword"] = d["search_keyword"] or ""
        d["fixed_url"] = d["fixed_url"] or ""
        d["pack_qty"] = d["pack_qty"] or 1
        d["min_order"] = d["min_order"] or 1
        d["notes"] = d["notes"] or ""
        d["is_coupang"] = d["is_coupang"] if d["is_coupang"] is not None else 0
        d["icecream_box_qty"] = d["icecream_box_qty"] or 0
        d["category"] = d["category"] or ""
        d["menu_code"] = d["menu_code"] or ""
        d["recommended_price"] = d["recommended_price"] or 0
        result[d["barcode"]] = CoupangCatalogItem(**d)
    return result


def export_catalog_to_xlsx_bytes() -> bytes:
    """현재 DB 카탈로그를 엑셀 바이트로 만든다(관리자 다운로드용)."""
    catalog = load_catalog()
    df = pd.DataFrame([
        {col: getattr(item, col) for col in _CATALOG_DB_COLUMNS}
        for item in catalog.values()
    ])
    buf = io.BytesIO()
    df.to_excel(buf, index=False, engine="openpyxl")
    return buf.getvalue()


def search_catalog_full(query: str, limit: int = 20) -> list[dict]:
    """관리자 페이지에서 기존 상품을 불러와 편집할 때 쓴다 - search_catalog()와
    달리 구분(is_coupang)/1타 개수 등 전체 필드를 돌려준다(수정 폼에 미리
    채워 넣기 위함)."""
    query = query.strip()
    if not query:
        return []
    catalog = load_catalog()
    query_lower = query.lower()

    matched = []
    for e in catalog.values():
        if not (
            query in e.barcode
            or query_lower in (e.menu_name or "").lower()
            or query_lower in (e.search_keyword or "").lower()
        ):
            continue
        matched.append({col: getattr(e, col) for col in _CATALOG_DB_COLUMNS})

    exact = [e for e in matched if e["barcode"] == query]
    chosen = exact or matched
    return chosen[:limit]


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