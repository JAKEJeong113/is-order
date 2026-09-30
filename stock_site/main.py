# main.py
"""거래명세서 사진으로 입고를 자동 반영하는 독립 서비스.

본체(is-order)와 완전히 분리된 별도 앱으로 만들었다(사용자 요청 - 나중에
괜찮으면 합칠 수도 있음). DB는 본체와 같은 Postgres(DATABASE_URL)를
공유하되, 이 앱이 직접 소유하는 테이블(stock_levels/inbound_documents/
inbound_line_items/vendor_business_registry)만 여기서 만들고 쓴다.
`vendor_product_index`는 본체의 catalog_auto_import.py가 도매처 카탈로그를
크롤링하면서 채워주는 테이블을 읽기만 한다 - "이 도매처의 이 상품명은
이 바코드다"라는 매핑이 이미 거기 있어서, 거래명세서 품목명을 그 도매처
범위 안에서만 비교하면 이름만으로도 정확하게 매칭할 수 있다(서로 다른
시스템끼리 이름을 비교하는 게 아니라, 같은 도매처가 찍어낸 이름끼리
비교하는 것이라 훨씬 신뢰도가 높음).

바코드가 확실하지 않은 품목(매칭 후보가 0개 또는 여러 개)은 재고에 바로
반영하지 않고 "확인 대기"로 남겨서, 관리자가 화면에서 직접 골라 확정해야
반영되게 한다."""
import base64
import json
import os
import re
import secrets
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# load_dotenv()를 인자 없이 쓰면 실행 위치에 따라 본체(Python/.env)의 값을
# 잘못 집어올 수 있다(barcode_site와 같은 이유) - 항상 이 파일 위치 기준
# .env를 명시해서 이 앱만의 설정을 쓰도록 고정한다.
BASE_DIR = Path(__file__).resolve().parent
# override=True: 이 프로세스가 다른 서비스(본체 등)와 같은 셸/부모 프로세스에서
# 떠서 ADMIN_PASSWORD 같은 이름이 이미 환경변수로 남아있어도(실측으로 겪음),
# 이 앱 자신의 .env 값을 항상 우선시킨다.
load_dotenv(BASE_DIR / ".env", override=True)

import anthropic
from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import db_conn

app = FastAPI(title="입출고 관리")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

admin_security = HTTPBasic()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")


def require_admin(credentials: HTTPBasicCredentials = Depends(admin_security)) -> bool:
    if not ADMIN_PASSWORD or not secrets.compare_digest(credentials.password, ADMIN_PASSWORD):
        raise HTTPException(status_code=401, detail="비밀번호가 올바르지 않습니다.", headers={"WWW-Authenticate": "Basic"})
    return True


_anthropic_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY")) if os.getenv("ANTHROPIC_API_KEY") else None

# 아직 사업자등록번호를 못 배운 도매처도 화면 드롭다운에 보여주기 위한
# 기본 이름 목록 - 본체 vendors.py의 이름과 맞춰둔다(자격증명은 이 앱이
# 다룰 필요가 없어 여기 다시 안 둠).
KNOWN_VENDORS = {
    "yamimall": "야미몰", "ccdome": "과자생각", "3bong": "삼봉몰",
    "hdinter": "현동몰", "douyou": "또요몰", "mud5": "도윤상사",
}

_PACK_SUFFIX_RE = re.compile(r"\s*\(1(?:타|묶음|박스?)\s*\d+\s*개입\)\s*$")


def _clean_name(name: str) -> str:
    s = _PACK_SUFFIX_RE.sub("", (name or "").strip())
    return re.sub(r"\s+", " ", s).strip()


def init_tables() -> None:
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS vendor_business_registry (
        vendor_id TEXT PRIMARY KEY,
        vendor_name TEXT NOT NULL,
        business_reg_no TEXT UNIQUE
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS stock_levels (
        barcode TEXT PRIMARY KEY,
        item_name TEXT,
        qty INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS inbound_documents (
        id SERIAL PRIMARY KEY,
        vendor_id TEXT,
        vendor_name_raw TEXT,
        business_reg_no_raw TEXT,
        doc_date TEXT,
        total_amount INTEGER,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS inbound_line_items (
        id SERIAL PRIMARY KEY,
        document_id INTEGER NOT NULL REFERENCES inbound_documents(id),
        raw_name TEXT,
        pack_note TEXT,
        expiry_date TEXT,
        qty_ordered INTEGER,
        unit_price INTEGER,
        supply_amount INTEGER,
        barcode TEXT,
        unit_qty INTEGER,
        applied_qty INTEGER,
        match_status TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)
    for vendor_id, name in KNOWN_VENDORS.items():
        cur.execute(
            "INSERT INTO vendor_business_registry (vendor_id, vendor_name) VALUES (?, ?) "
            "ON CONFLICT(vendor_id) DO NOTHING",
            (vendor_id, name),
        )
    conn.commit()
    conn.close()


init_tables()


# --- 명세서 이미지 -> 구조화 데이터 (Claude Vision) ---

_EXTRACT_PROMPT = """이 이미지는 도매몰에서 받은 거래명세서(세금계산서)입니다.
표에 있는 품목을 전부 읽어서 아래 JSON 형식으로만 답하세요. 다른 설명은 절대 붙이지 마세요.
"이하 여백" 같은 빈 줄이나 소계/합계 행은 items에 넣지 마세요.

{
  "vendor_name": "공급자(공급하는자) 상호명",
  "business_reg_no": "공급자 등록번호(숫자와 하이픈만, 예: 290-86-01213)",
  "doc_date": "일자 (YYYY-MM-DD)",
  "total_amount": 합계금액(숫자만),
  "items": [
    {
      "name": "상품의 핵심 이름(브랜드+상품명+용량/g 포함). 포장단위 표기(예: 1타10개입)와 유효기간은 절대 포함하지 말 것",
      "pack_note": "품목명에 포함돼 있던 포장단위 표기 원문(예: 1타10개입). 없으면 빈 문자열",
      "expiry_date": "품목명에 포함돼 있던 유효기간(YYYY-MM-DD). 없으면 빈 문자열",
      "qty": 수량(숫자만),
      "unit_price": 단가(숫자만),
      "supply_amount": 공급가액(숫자만)
    }
  ]
}"""


def _extract_json(text: str) -> dict:
    # 모델이 코드블록(```json ... ```)으로 감싸는 경우를 대비해 중괄호 구간만 뽑는다.
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("응답에서 JSON을 찾지 못했습니다: " + text[:200])
    return json.loads(match.group(0))


def parse_invoice_image(image_bytes: bytes, media_type: str) -> dict:
    if _anthropic_client is None:
        raise RuntimeError("ANTHROPIC_API_KEY가 설정되지 않았습니다.")
    b64 = base64.b64encode(image_bytes).decode("ascii")
    resp = _anthropic_client.messages.create(
        model="claude-sonnet-5",
        max_tokens=4096,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
                {"type": "text", "text": _EXTRACT_PROMPT},
            ],
        }],
    )
    text = "".join(block.text for block in resp.content if block.type == "text")
    return _extract_json(text)


def _lookup_vendor_id(business_reg_no: str) -> str | None:
    if not business_reg_no:
        return None
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute("SELECT vendor_id FROM vendor_business_registry WHERE business_reg_no = ?", (business_reg_no,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else None


def _match_item(vendor_id: str | None, name: str) -> tuple[str | None, int | None, str]:
    """(바코드, 개당수량, 상태) - 상태는 'auto'(정확히 하나 일치) 또는 'pending'."""
    if not vendor_id:
        return None, None, "pending"
    clean = _clean_name(name)
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT barcode, unit_qty FROM vendor_product_index WHERE vendor_id = ? AND clean_name = ?",
        (vendor_id, clean),
    )
    rows = cur.fetchall()
    conn.close()
    if len(rows) == 1:
        return rows[0][0], rows[0][1], "auto"
    return None, None, "pending"


def _apply_stock(barcode: str, item_name: str, delta_qty: int) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO stock_levels (barcode, item_name, qty, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(barcode) DO UPDATE SET
            qty = stock_levels.qty + excluded.qty,
            item_name = excluded.item_name,
            updated_at = excluded.updated_at
        """,
        (barcode, item_name, delta_qty, now),
    )
    conn.commit()
    conn.close()


# --- 라우트 ---

@app.get("/", response_class=HTMLResponse)
def index_page(request: Request, _: bool = Depends(require_admin)):
    return templates.TemplateResponse("index.html", {"request": request})


@app.post("/api/invoices")
async def api_upload_invoice(file: UploadFile = File(...), _: bool = Depends(require_admin)):
    image_bytes = await file.read()
    media_type = file.content_type or "image/jpeg"
    try:
        parsed = parse_invoice_image(image_bytes, media_type)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"명세서 인식 실패: {e}")

    vendor_name_raw = parsed.get("vendor_name") or ""
    business_reg_no_raw = (parsed.get("business_reg_no") or "").strip()
    vendor_id = _lookup_vendor_id(business_reg_no_raw)
    now = datetime.now().isoformat(timespec="seconds")

    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO inbound_documents
            (vendor_id, vendor_name_raw, business_reg_no_raw, doc_date, total_amount, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            vendor_id, vendor_name_raw, business_reg_no_raw, parsed.get("doc_date"),
            parsed.get("total_amount"), "needs_vendor" if not vendor_id else "processing", now,
        ),
    )
    cur.execute("SELECT lastval()")
    doc_id = cur.fetchone()[0]

    auto_count = 0
    pending_count = 0
    for item in parsed.get("items", []):
        name = item.get("name") or ""
        qty_ordered = item.get("qty")
        barcode, unit_qty, status = (None, None, "pending") if not vendor_id else _match_item(vendor_id, name)
        applied_qty = None
        if status == "auto" and isinstance(qty_ordered, int):
            applied_qty = qty_ordered * (unit_qty or 1)
        cur.execute(
            """
            INSERT INTO inbound_line_items
                (document_id, raw_name, pack_note, expiry_date, qty_ordered, unit_price, supply_amount,
                 barcode, unit_qty, applied_qty, match_status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                doc_id, name, item.get("pack_note"), item.get("expiry_date"), qty_ordered,
                item.get("unit_price"), item.get("supply_amount"), barcode, unit_qty, applied_qty, status, now,
            ),
        )
        if status == "auto":
            auto_count += 1
            _apply_stock(barcode, name, applied_qty)
        else:
            pending_count += 1

    if vendor_id:
        cur.execute(
            "UPDATE inbound_documents SET status = ? WHERE id = ?",
            ("needs_review" if pending_count else "completed", doc_id),
        )
    conn.commit()
    conn.close()

    return {"ok": True, "document_id": doc_id, "vendor_id": vendor_id, "auto_matched": auto_count, "pending": pending_count}


@app.get("/api/invoices")
def api_list_invoices(_: bool = Depends(require_admin)):
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT id, vendor_id, vendor_name_raw, doc_date, total_amount, status, created_at
    FROM inbound_documents ORDER BY id DESC LIMIT 100
    """)
    rows = cur.fetchall()
    conn.close()
    return {"items": [
        {
            "id": r[0], "vendor_id": r[1], "vendor_name_raw": r[2], "doc_date": r[3],
            "total_amount": r[4], "status": r[5], "created_at": r[6],
        }
        for r in rows
    ]}


@app.get("/api/invoices/{doc_id}")
def api_get_invoice(doc_id: int, _: bool = Depends(require_admin)):
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT id, vendor_id, vendor_name_raw, business_reg_no_raw, doc_date, total_amount, status, created_at
    FROM inbound_documents WHERE id = ?
    """, (doc_id,))
    doc = cur.fetchone()
    if not doc:
        conn.close()
        raise HTTPException(status_code=404, detail="문서를 찾을 수 없습니다.")
    cur.execute("""
    SELECT id, raw_name, pack_note, expiry_date, qty_ordered, unit_price, supply_amount,
           barcode, unit_qty, applied_qty, match_status
    FROM inbound_line_items WHERE document_id = ? ORDER BY id
    """, (doc_id,))
    items = cur.fetchall()
    conn.close()
    return {
        "id": doc[0], "vendor_id": doc[1], "vendor_name_raw": doc[2], "business_reg_no_raw": doc[3],
        "doc_date": doc[4], "total_amount": doc[5], "status": doc[6], "created_at": doc[7],
        "items": [
            {
                "id": r[0], "raw_name": r[1], "pack_note": r[2], "expiry_date": r[3], "qty_ordered": r[4],
                "unit_price": r[5], "supply_amount": r[6], "barcode": r[7], "unit_qty": r[8],
                "applied_qty": r[9], "match_status": r[10],
            }
            for r in items
        ],
    }


class ResolveVendorRequest(BaseModel):
    vendor_id: str


@app.post("/api/invoices/{doc_id}/vendor")
def api_resolve_vendor(doc_id: int, req: ResolveVendorRequest, _: bool = Depends(require_admin)):
    """문서의 사업자번호로 도매처를 못 찾았을 때, 관리자가 직접 골라서
    확정한다 - 이후 같은 사업자번호가 오면 자동으로 인식되도록 등록해둔다."""
    if req.vendor_id not in KNOWN_VENDORS:
        raise HTTPException(status_code=400, detail="알 수 없는 도매처입니다.")
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute("SELECT business_reg_no_raw FROM inbound_documents WHERE id = ?", (doc_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="문서를 찾을 수 없습니다.")
    business_reg_no = row[0]
    if business_reg_no:
        cur.execute(
            "UPDATE vendor_business_registry SET business_reg_no = ? WHERE vendor_id = ?",
            (business_reg_no, req.vendor_id),
        )
    cur.execute("UPDATE inbound_documents SET vendor_id = ? WHERE id = ?", (req.vendor_id, doc_id))

    # 이 문서의 미매칭 품목들을 이제 막 확정된 도매처 기준으로 다시 매칭해본다.
    cur.execute(
        "SELECT id, raw_name, qty_ordered FROM inbound_line_items WHERE document_id = ? AND match_status = 'pending'",
        (doc_id,),
    )
    pending_items = cur.fetchall()
    remaining_pending = 0
    for item_id, raw_name, qty_ordered in pending_items:
        barcode, unit_qty, status = _match_item(req.vendor_id, raw_name)
        if status == "auto":
            applied_qty = (qty_ordered or 0) * (unit_qty or 1)
            cur.execute(
                "UPDATE inbound_line_items SET barcode=?, unit_qty=?, applied_qty=?, match_status='auto' WHERE id=?",
                (barcode, unit_qty, applied_qty, item_id),
            )
            _apply_stock(barcode, raw_name, applied_qty)
        else:
            remaining_pending += 1
    cur.execute(
        "UPDATE inbound_documents SET status = ? WHERE id = ?",
        ("needs_review" if remaining_pending else "completed", doc_id),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "remaining_pending": remaining_pending}


class ResolveItemRequest(BaseModel):
    barcode: str


@app.post("/api/invoices/{doc_id}/items/{item_id}/resolve")
def api_resolve_item(doc_id: int, item_id: int, req: ResolveItemRequest, _: bool = Depends(require_admin)):
    """관리자가 후보 목록(또는 직접 입력)에서 바코드를 골라 미매칭 품목을
    확정한다. 확정 즉시 재고에 반영한다."""
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT raw_name, qty_ordered, match_status, document_id FROM inbound_line_items WHERE id = ?",
        (item_id,),
    )
    row = cur.fetchone()
    if not row or row[3] != doc_id:
        conn.close()
        raise HTTPException(status_code=404, detail="품목을 찾을 수 없습니다.")
    raw_name, qty_ordered, match_status, _doc_id = row
    if match_status == "auto":
        conn.close()
        raise HTTPException(status_code=400, detail="이미 확정된 품목입니다.")

    cur.execute(
        "SELECT unit_qty FROM vendor_product_index WHERE barcode = ? LIMIT 1",
        (req.barcode,),
    )
    idx_row = cur.fetchone()
    unit_qty = idx_row[0] if idx_row else 1
    applied_qty = (qty_ordered or 0) * (unit_qty or 1)

    cur.execute(
        "UPDATE inbound_line_items SET barcode=?, unit_qty=?, applied_qty=?, match_status='confirmed' WHERE id=?",
        (req.barcode, unit_qty, applied_qty, item_id),
    )
    cur.execute(
        "SELECT COUNT(*) FROM inbound_line_items WHERE document_id = ? AND match_status = 'pending'",
        (doc_id,),
    )
    remaining_pending = cur.fetchone()[0]
    cur.execute(
        "UPDATE inbound_documents SET status = ? WHERE id = ?",
        ("needs_review" if remaining_pending else "completed", doc_id),
    )
    conn.commit()
    conn.close()
    _apply_stock(req.barcode, raw_name, applied_qty)
    return {"ok": True, "applied_qty": applied_qty}


@app.get("/api/vendor-search")
def api_vendor_search(vendor_id: str, q: str, _: bool = Depends(require_admin)):
    """미매칭 품목을 관리자가 직접 검색해서 고를 수 있게, 그 도매처
    범위 안에서만 이름으로 찾는다."""
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT barcode, product_name, unit_qty FROM vendor_product_index "
        "WHERE vendor_id = ? AND clean_name ILIKE ? ORDER BY product_name LIMIT 20",
        (vendor_id, f"%{q}%"),
    )
    rows = cur.fetchall()
    conn.close()
    return {"items": [{"barcode": r[0], "product_name": r[1], "unit_qty": r[2]} for r in rows]}


@app.get("/api/stock")
def api_list_stock(_: bool = Depends(require_admin)):
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute("SELECT barcode, item_name, qty, updated_at FROM stock_levels ORDER BY updated_at DESC LIMIT 500")
    rows = cur.fetchall()
    conn.close()
    return {"items": [{"barcode": r[0], "item_name": r[1], "qty": r[2], "updated_at": r[3]} for r in rows]}


@app.get("/api/vendors")
def api_list_vendors(_: bool = Depends(require_admin)):
    return {"vendors": KNOWN_VENDORS}
