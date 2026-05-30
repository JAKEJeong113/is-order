# parser.py
import math
import pandas as pd
from datetime import date

COLUMN_ALIASES = {
    "메뉴코드": ["메뉴코드", "메뉴 코드", "상품코드", "상품 코드", "CODE", "Code"],
    "바코드번호": ["바코드번호", "바코드 번호", "BARCODE", "Barcode"],
    "메뉴명": ["메뉴명", "상품명", "품명", "제품명", "NAME", "Name"],
    "판매수량": ["판매수량", "판매건수", "거래건수", "수량", "QTY", "Qty"],
}

def _norm(x) -> str:
    if x is None:
        return ""
    return str(x).strip().replace("\n", " ").replace("\r", " ")

def _find_header_row(df_raw: pd.DataFrame, search_rows: int = 60) -> int | None:
    max_r = min(search_rows, len(df_raw))
    for r in range(max_r):
        row = [_norm(v) for v in df_raw.iloc[r].tolist()]
        hit = 0
        for logical, aliases in COLUMN_ALIASES.items():
            if any(a in row for a in aliases):
                hit += 1
        if hit >= 3:
            return r
    return None

def _map_columns(cols: list[str]) -> dict[str, str]:
    mapped = {}
    for logical, aliases in COLUMN_ALIASES.items():
        for a in aliases:
            if a in cols:
                mapped[logical] = a
                break
    return mapped

def _safe_str_cell(x) -> str:
    # pandas NaN / None 안전 처리
    try:
        if pd.isna(x):
            return ""
    except Exception:
        pass
    s = str(x).strip()
    if s.lower() in ("nan", "none", "null"):
        return ""
    return s

def _clean_records_for_json(records: list[dict]) -> list[dict]:
    # JSON 직렬화 문제 나는 값(NaN/inf)을 안전하게 정리
    cleaned = []
    for rec in records:
        out = {}
        for k, v in rec.items():
            if isinstance(v, float):
                if math.isnan(v) or math.isinf(v):
                    out[k] = None
                else:
                    out[k] = v
            else:
                # pandas NA 계열
                try:
                    if pd.isna(v):
                        out[k] = None
                    else:
                        out[k] = v
                except Exception:
                    out[k] = v
        cleaned.append(out)
    return cleaned

def parse_menu_sales_xlsx(xlsx_path: str, period_from: date, period_to: date):
    df_raw = pd.read_excel(xlsx_path, header=None, engine="openpyxl")

    header_row = _find_header_row(df_raw, search_rows=60)
    if header_row is None:
        preview = df_raw.head(20).fillna("").astype(str).values.tolist()
        raise ValueError("헤더 행을 찾지 못했습니다.\n" + str(preview))

    cols = [_norm(v) for v in df_raw.iloc[header_row].tolist()]
    df = df_raw.iloc[header_row + 1 :].copy()
    df.columns = cols
    df = df.loc[:, [c for c in df.columns if _norm(c) != ""]]

    mapped = _map_columns(list(df.columns))

    required = ["메뉴명", "판매수량"]
    missing = [k for k in required if k not in mapped]
    if missing:
        raise ValueError(
            f"필수 컬럼을 찾지 못했습니다: {missing}\n"
            f"현재 컬럼: {list(df.columns)}\n"
            f"탐지된 헤더 행(0-index): {header_row}\n"
            f"매핑 결과: {mapped}"
        )

    code_col = mapped.get("메뉴코드")
    bc_col = mapped.get("바코드번호")
    name_col = mapped["메뉴명"]
    qty_col = mapped["판매수량"]

    use_cols = [name_col, qty_col]
    if code_col:
        use_cols.insert(0, code_col)
    if bc_col:
        use_cols.insert(0, bc_col)

    out = df[use_cols].copy()

    if bc_col:
        out[bc_col] = out[bc_col].apply(_safe_str_cell)
    if code_col:
        out[code_col] = out[code_col].apply(_safe_str_cell)
    out[name_col] = out[name_col].apply(_safe_str_cell)

    # 수량
    out[qty_col] = pd.to_numeric(out[qty_col], errors="coerce").fillna(0)
    out[qty_col] = out[qty_col].astype(float)

    # ✅ SKU 키: 바코드 > 메뉴코드 > 메뉴명
    def make_key(row):
        bc = row.get(bc_col, "") if bc_col else ""
        code = row.get(code_col, "") if code_col else ""
        name = row.get(name_col, "")
        if bc:
            return f"BC:{bc}"
        if code:
            return f"CODE:{code}"
        return f"NAME:{name}"

    out["sku_key"] = out.apply(make_key, axis=1)

    # 집계
    if bc_col and code_col:
        agg = (
            out.groupby("sku_key", as_index=False)
               .agg(
                   바코드번호=(bc_col, "first"),
                   메뉴코드=(code_col, "first"),
                   메뉴명=(name_col, "first"),
                   판매수량=(qty_col, "sum"),
               )
        )
    elif bc_col and (not code_col):
        agg = (
            out.groupby("sku_key", as_index=False)
               .agg(
                   바코드번호=(bc_col, "first"),
                   메뉴코드=(name_col, "first"),
                   메뉴명=(name_col, "first"),
                   판매수량=(qty_col, "sum"),
               )
        )
    elif code_col and (not bc_col):
        agg = (
            out.groupby("sku_key", as_index=False)
               .agg(
                   바코드번호=(name_col, "first"),
                   메뉴코드=(code_col, "first"),
                   메뉴명=(name_col, "first"),
                   판매수량=(qty_col, "sum"),
               )
        )
    else:
        agg = (
            out.groupby("sku_key", as_index=False)
               .agg(
                   바코드번호=(name_col, "first"),
                   메뉴코드=(name_col, "first"),
                   메뉴명=(name_col, "first"),
                   판매수량=(qty_col, "sum"),
               )
        )

    agg["판매수량"] = agg["판매수량"].round(0).astype(int)
    agg = agg.sort_values("판매수량", ascending=False)

    days = max((period_to - period_from).days + 1, 1)
    agg["일평균"] = agg["판매수량"] / days
    agg["7일예상수량"] = agg["일평균"].apply(lambda x: int(math.ceil(x * 7)))

    # ✅ 응답 전 NaN 방지(문자열 컬럼도 혹시 모를 NA 제거)
    for c in ["바코드번호", "메뉴코드", "메뉴명"]:
        if c in agg.columns:
            agg[c] = agg[c].apply(_safe_str_cell)

    summary = {
        "period_from": period_from.isoformat(),
        "period_to": period_to.isoformat(),
        "days": days,
        "총판매수량": int(agg["판매수량"].sum()),
        "상품종류수": int(len(agg)),
        "header_row_detected": int(header_row),
        "mapped_columns": mapped,
        "qty_source_column": qty_col,
        "sku_key_rule": "barcode>menu_code>menu_name",
    }

    top_items = agg.to_dict(orient="records")
    top_items = _clean_records_for_json(top_items)

    return agg, summary, top_items