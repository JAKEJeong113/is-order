# main.py
"""아이스크림 바코드 조회 - is-order 본체에서 가장 자주 쓰이던 기능 하나만
떼어내 별도 배포하는 가벼운 사이트. 로그인 없이 누구나 바코드/상품명으로
추천판매가를 조회할 수 있다(무인매장 발주 자동화 서비스를 알리는 무료
도구 역할도 겸함 - 결과 하단에 본 서비스(i's ORDER) 안내를 붙인다).

DB는 본체(is-order)와 같은 Postgres(DATABASE_URL)를 공유하되, 이 사이트는
catalog_items 테이블을 읽기만 한다 - 테이블 생성/쓰기는 전혀 하지 않는다
(본체가 이미 그 테이블의 유일한 소유자)."""
import os
import sys
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

import db_conn

app = FastAPI(title="아이스크림 바코드 조회")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

MAIN_SITE_URL = os.getenv("MAIN_SITE_URL", "https://www.is-cream.co.kr")


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(
        "index.html", {"request": request, "main_site_url": MAIN_SITE_URL},
    )


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
