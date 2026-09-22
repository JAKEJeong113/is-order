# main.py
"""무인매장 바코드 조회 - is-order 본체에서 가장 자주 쓰이던 기능 하나만
떼어내 별도 배포하는 가벼운 사이트. 과자·음료·문구류 등 카탈로그에 있는
모든 상품을 대상으로, 로그인 없이 누구나 바코드/상품명으로 추천판매가를
조회할 수 있다(무인매장 발주 자동화 서비스를 알리는 무료 도구 역할도
겸함 - 결과 하단에 본 서비스(i's ORDER) 안내를 붙인다).

DB는 본체(is-order)와 같은 Postgres(DATABASE_URL)를 공유하되, 이 사이트는
catalog_items 테이블을 읽기만 한다 - 테이블 생성/쓰기는 전혀 하지 않는다
(본체가 이미 그 테이블의 유일한 소유자)."""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# 이 파일이 어느 작업 디렉터리에서 실행되든(로컬 개발/Render의 Root Directory
# 배포 등) 항상 이 폴더의 db_conn.py를 쓰고 templates/를 정확히 찾도록,
# 상대 경로 대신 이 파일 위치를 기준으로 절대 경로를 쓴다 - 본체 프로젝트
# 루트에도 같은 이름의 db_conn.py가 있어서, 상대 import에 의존하면 실행
# 위치에 따라 엉뚱한(하지만 겉보기엔 비슷하게 동작하는) 파일을 잘못 가져올
# 위험이 있다.
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

import db_conn

app = FastAPI(title="무인매장 바코드 조회")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

MAIN_SITE_URL = os.getenv("MAIN_SITE_URL", "https://www.is-cream.co.kr")

# 카탈로그 검수 대기(pending_catalog_submissions)는 본체(is-order)가 소유한
# 테이블이 아니라 이 사이트에서 새로 만든 테이블이라(catalog_items와 달리
# 본체와 공유하는 기존 테이블이 아님) 여기서 직접 생성한다 - 본체 main.py도
# 같은 CREATE TABLE IF NOT EXISTS를 갖고 있어(mapping.py) 어느 쪽이 먼저
# 떠도 안전하다.
_conn = db_conn.get_conn()
_conn.cursor().execute("""
CREATE TABLE IF NOT EXISTS pending_catalog_submissions (
    barcode TEXT PRIMARY KEY,
    menu_name TEXT NOT NULL,
    is_coupang INTEGER NOT NULL DEFAULT 99,
    recommended_price INTEGER,
    submitted_at TEXT,
    updated_at TEXT
)
""")
_conn.commit()
_conn.close()

# 가격인상안내용 - 본체(mapping.py/catalog_auto_import.py)가 추천판매가가
# 실제로 오를 때만 기록하는 테이블. 이 사이트는 읽기만 하지만, 배포 순서와
# 무관하게 안전하도록(pending_catalog_submissions와 같은 이유) 여기서도
# CREATE TABLE IF NOT EXISTS로 만들어둔다.
_conn = db_conn.get_conn()
_conn.cursor().execute("""
CREATE TABLE IF NOT EXISTS catalog_price_changes (
    id SERIAL PRIMARY KEY,
    barcode TEXT NOT NULL,
    old_price INTEGER NOT NULL,
    new_price INTEGER NOT NULL,
    changed_at TEXT NOT NULL
)
""")
_conn.commit()
_conn.close()

# 이 앱(무인 바코드 검색기) 전용 패치노트 - 본체(main.py, barcode_app_patch_notes.py)가
# 쓰는 것과 같은 테이블을 여기서도(배포 순서 무관하게) 만들어두고 읽기만 한다.
_conn = db_conn.get_conn()
# 이 파일(barcode_site)의 db_conn.py는 본체 것과 달리 SQLite 문법 번역
# 계층이 없다(원래 catalog_items 읽기 전용이라 필요 없었음) - 여기서는
# Postgres 네이티브 문법(SERIAL)을 바로 쓴다.
_conn.cursor().execute("""
CREATE TABLE IF NOT EXISTS barcode_app_patch_notes (
    id SERIAL PRIMARY KEY,
    version TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL
)
""")
_conn.commit()
_conn.close()


@app.get("/api/patch-notes")
def api_patch_notes(limit: int = Query(50, ge=1, le=200)):
    """앱 메인화면의 메뉴 > 패치노트 목록 - 로그인 없이 누구나 조회 가능."""
    conn = db_conn.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, version, title, detail, created_at FROM barcode_app_patch_notes ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    items = [
        {"id": r[0], "version": r[1], "title": r[2], "detail": r[3], "created_at": r[4]}
        for r in rows
    ]
    return {"items": items}


class PendingSubmissionRequest(BaseModel):
    barcode: str = Field(..., min_length=4, max_length=32)
    menu_name: str = Field(..., min_length=1, max_length=200)
    is_coupang: int = Field(99, ge=0, le=99)
    recommended_price: int | None = Field(None, ge=0)


@app.post("/api/pending-submission")
def api_pending_submission(req: PendingSubmissionRequest):
    """카탈로그에 없는 상품을 점주가 이 사이트에서 직접 입력해 오더퀸에
    바로 등록할 때("카탈로그에 없는 상품입니다" 화면의 등록 폼), 그 값을
    검수 대기 목록에 남긴다 - 아무나 입력한 값이 카탈로그에 바로 반영되지
    않고, 관리자가 www.is-cream.co.kr/admin에서 확인한 뒤에만 정식
    반영된다. 오더퀸 등록 자체는 이 사이트가 아니라 안드로이드 앱의 네이티브
    브리지(AndroidOrderQueen)가 처리하므로 여기서는 기록만 한다."""
    barcode = req.barcode.strip()
    menu_name = req.menu_name.strip()
    now = datetime.now().isoformat(timespec="seconds")
    conn = db_conn.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO pending_catalog_submissions
                (barcode, menu_name, is_coupang, recommended_price, submitted_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (barcode) DO UPDATE SET
                menu_name = excluded.menu_name,
                is_coupang = excluded.is_coupang,
                recommended_price = excluded.recommended_price,
                updated_at = excluded.updated_at
            """,
            (barcode, menu_name, req.is_coupang, req.recommended_price, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    # Starlette/Jinja2 최신 버전 조합에서 예전 방식 TemplateResponse(name, {"request":
    # request, ...})가 "TypeError: cannot use 'tuple' as a dict key"로 깨지는 걸
    # Render 배포에서 실측 확인(로컬에 깔려있던 구버전 조합에서는 재현 안 됐음) -
    # request를 첫 인자로 넘기는 현재 권장 방식으로 고정한다.
    response = templates.TemplateResponse(
        request, "index.html", {"main_site_url": MAIN_SITE_URL},
    )
    # CSS/JS가 전부 이 HTML 하나에 인라인으로 들어있어서(별도 정적 파일
    # 없음), 이 문서 자체가 캐시되면 배포한 새 기능(예: 즉석 등록 폼)이
    # 안드로이드 WebView에서 한참 지나서야(앱을 완전히 껐다 켜야) 반영되는
    # 문제가 실측 확인됐다 - 매번 서버에서 새로 받아오게 강제한다.
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@app.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request):
    # 구글 플레이 스토어 등록 시 카메라 권한을 쓰는 앱은 개인정보처리방침
    # URL 등록이 필수라서 만든 페이지 - 앱/웹 공용으로 쓴다.
    return templates.TemplateResponse(request, "privacy.html", {})


@app.get("/api/search")
def api_search(q: str = Query(..., min_length=1, max_length=100), limit: int = Query(20, ge=1, le=50)):
    query = q.strip()
    if not query:
        return {"items": []}

    # LIKE/ILIKE 특수문자(%, _, \)를 리터럴로 취급하도록 이스케이프한다 -
    # 안 하면 "50%" 같은 검색어가 와일드카드로 해석돼 엉뚱하게 매칭된다.
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = f"%{escaped}%"

    conn = db_conn.get_conn()
    try:
        cur = conn.cursor()
        # 바코드가 정확히 일치하는 상품을 최상단에 올린다(본체 product_ranking.
        # search_catalog와 동일한 우선순위 규칙) - Postgres는 boolean을
        # false=0/true=1로 정렬하므로 (barcode = ?) DESC 하나로 충분하다.
        cur.execute(
            """
            SELECT barcode, menu_name, recommended_price
            FROM catalog_items
            WHERE barcode LIKE ? ESCAPE '\\'
               OR menu_name ILIKE ? ESCAPE '\\'
               OR search_keyword ILIKE ? ESCAPE '\\'
            ORDER BY (barcode = ?) DESC, menu_name ASC
            LIMIT ?
            """,
            (like, like, like, query, limit),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    items = [
        {
            "barcode": barcode,
            "menu_name": menu_name or "(이름 없음)",
            "recommended_price": recommended_price or 0,
        }
        for barcode, menu_name, recommended_price in rows
    ]
    return {"items": items}


@app.get("/api/new-products")
def api_new_products(
    category: str | None = Query(None, description="본체 is_coupang 값(0=아이스크림,2=도매몰 등). 안 주면 전체."),
    limit: int = Query(100, ge=1, le=300),
):
    """"신제품 안내" 목록 - 도매몰 자동 크롤링(catalog_auto_import.py)이나
    관리자가 수동으로 새로 등록/수정한 상품을 최근 것부터 보여준다.
    catalog_items에 별도 "등록일" 컬럼이 없어서 updated_at(마지막 수정 시각)을
    기준으로 쓴다 - 새로 추가되는 상품은 그 순간 updated_at이 찍히므로
    "신제품"의 근사치로 충분하지만, 기존 상품을 관리자가 단순히 편집만 해도
    같이 올라온다는 점은 감안해야 한다."""
    cutoff = (datetime.now() - timedelta(days=28)).isoformat(timespec="seconds")

    conn = db_conn.get_conn()
    try:
        cur = conn.cursor()
        if category:
            cur.execute(
                """
                SELECT barcode, menu_name, recommended_price
                FROM catalog_items
                WHERE updated_at >= ? AND is_coupang = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (cutoff, int(category), limit),
            )
        else:
            cur.execute(
                """
                SELECT barcode, menu_name, recommended_price
                FROM catalog_items
                WHERE updated_at >= ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (cutoff, limit),
            )
        rows = cur.fetchall()
    finally:
        conn.close()

    items = [
        {
            "barcode": barcode,
            "menu_name": menu_name or "(이름 없음)",
            "recommended_price": recommended_price or 0,
        }
        for barcode, menu_name, recommended_price in rows
    ]
    return {"items": items}


@app.get("/api/price-increases")
def api_price_increases(
    category: str | None = Query(None, description="본체 is_coupang 값(0=아이스크림,1=쿠팡,2=도매몰). 안 주면 전체."),
    limit: int = Query(100, ge=1, le=300),
):
    """"가격인상 안내" 목록 - 최근(4주 이내) 추천판매가가 오른 상품을
    보여주고, 점주가 "오더퀸 등록"으로 바뀐 가격을 바로 반영할 수 있게
    한다. catalog_price_changes(본체가 가격이 실제로 오를 때만 기록)에서
    바코드당 가장 최근 변경 1건만 골라 보여준다 - 같은 상품이 기간 안에
    여러 번 올랐어도 목록엔 한 줄만 뜨고, 표시되는 이전가는 그 마지막
    인상 직전 가격이다."""
    cutoff = (datetime.now() - timedelta(days=28)).isoformat(timespec="seconds")

    conn = db_conn.get_conn()
    try:
        cur = conn.cursor()
        if category:
            cur.execute(
                """
                WITH latest_changes AS (
                    SELECT DISTINCT ON (p.barcode)
                        p.barcode, c.menu_name, c.is_coupang, p.old_price, p.new_price, p.changed_at
                    FROM catalog_price_changes p
                    JOIN catalog_items c ON c.barcode = p.barcode
                    WHERE p.changed_at >= ?
                    ORDER BY p.barcode, p.changed_at DESC
                )
                SELECT barcode, menu_name, old_price, new_price
                FROM latest_changes
                WHERE is_coupang = ?
                ORDER BY changed_at DESC
                LIMIT ?
                """,
                (cutoff, int(category), limit),
            )
        else:
            cur.execute(
                """
                WITH latest_changes AS (
                    SELECT DISTINCT ON (p.barcode)
                        p.barcode, c.menu_name, p.old_price, p.new_price, p.changed_at
                    FROM catalog_price_changes p
                    JOIN catalog_items c ON c.barcode = p.barcode
                    WHERE p.changed_at >= ?
                    ORDER BY p.barcode, p.changed_at DESC
                )
                SELECT barcode, menu_name, old_price, new_price
                FROM latest_changes
                ORDER BY changed_at DESC
                LIMIT ?
                """,
                (cutoff, limit),
            )
        rows = cur.fetchall()
    finally:
        conn.close()

    items = [
        {
            "barcode": barcode,
            "menu_name": menu_name or "(이름 없음)",
            "old_price": old_price or 0,
            "new_price": new_price or 0,
        }
        for barcode, menu_name, old_price, new_price in rows
    ]
    return {"items": items}


_SALES_RANKING_CATEGORIES = ("icecream", "coupang", "beverage", "wholesale")


@app.get("/api/sales-ranking")
def api_sales_ranking(
    category: str = Query(..., description="icecream|coupang|beverage|wholesale"),
    period: str = Query(..., description="week|month"),
    limit: int = Query(20, ge=1, le=50),
):
    """"인기상품 순위" 메뉴 - 판매 데이터 활용에 동의한 매장들의 오더퀸 실제
    판매량을 모아 판매처(아이스크림/쿠팡/음료/도매몰)별·기간(이번 주/이번 달)별로
    집계한 순위. 데이터는 본체(is-order)의 일 1회 배치(sales_ranking.py)가
    쌓아두는 oq_sales_events 테이블을 읽기만 한다(이 사이트가 소유한
    테이블이 아님 - catalog_items와 같은 패턴).

    store_vendor_credentials와 조인해서 "지금 이 순간 동의 상태인" 계정의
    데이터만 센다 - 수집 시점엔 동의했다가 나중에 동의를 철회한 매장의
    과거 데이터가 계속 순위에 남아있으면 안 되므로, 매 조회마다 현재
    동의 여부를 다시 확인한다(단순히 "앞으로 수집을 멈추는" 것과는 다름).

    "이번 주"는 월요일부터 오늘까지, "이번 달"은 1일부터 오늘까지다(본체와
    동일 기준 - sales_ranking._period_range 참고)."""
    if category not in _SALES_RANKING_CATEGORIES:
        return {"items": [], "period_from": None, "period_to": None}

    today = datetime.now().date()
    if period == "week":
        start = today - timedelta(days=today.weekday())
    elif period == "month":
        start = today.replace(day=1)
    else:
        return {"items": [], "period_from": None, "period_to": None}

    conn = db_conn.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT e.item_key, MAX(e.item_name) AS item_name, SUM(e.qty) AS total_qty
            FROM oq_sales_events e
            JOIN store_vendor_credentials c
                ON c.store_id = e.store_id AND c.id = e.account_id AND c.vendor_id = 'orderqueen'
            WHERE e.category = ? AND e.sale_date >= ? AND e.sale_date <= ? AND c.sales_data_consent = 1
            GROUP BY e.item_key
            ORDER BY total_qty DESC
            LIMIT ?
            """,
            (category, start.isoformat(), today.isoformat(), limit),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    items = [
        {"rank": i + 1, "item_name": r[1] or "(이름 없음)", "total_qty": r[2]}
        for i, r in enumerate(rows)
    ]
    return {"items": items, "period_from": start.isoformat(), "period_to": today.isoformat()}


@app.get("/healthz")
def healthz():
    """DB 연결/데이터 상태를 바로 확인하기 위한 진단용 엔드포인트 - "검색은
    되는데 결과가 항상 없음/오류남" 같은 증상이 나올 때, 이 사이트가 실제로
    바라보고 있는 DATABASE_URL이 본체와 같은 DB(같은 catalog_items 데이터)를
    가리키는지부터 확인할 수 있게 한다."""
    try:
        conn = db_conn.get_conn()
    except Exception as e:
        return {"db_connected": False, "error_type": type(e).__name__, "error": str(e)[:300]}

    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM catalog_items")
        count = cur.fetchone()[0]
        return {"db_connected": True, "catalog_items_count": count}
    except Exception as e:
        return {"db_connected": True, "query_ok": False, "error_type": type(e).__name__, "error": str(e)[:300]}
    finally:
        conn.close()
