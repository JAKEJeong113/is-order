# main.py
from __future__ import annotations

import math
import re
import uuid
import hmac
import hashlib
import json
import os
from dotenv import load_dotenv
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import pandas as pd
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import secrets

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from orderqueen_bot import download_orderqueen_xlsx
from parser import parse_menu_sales_xlsx
from mapping import load_coupang_catalog_xlsx, select_representative_item
from db import init_db, get_inventory, upsert_inventory, change_stock

from yamimall_bot import add_yamimall_cart
import catalog_cache
import catalog_crawler
import godomall_bot
import popularity
import telegram_bot
import telegram_store
import vendors
import price_compare
import web_auth
import yamimall_bot

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
EXPORT_DIR = BASE_DIR / "exports"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
EXPORT_DIR.mkdir(parents=True, exist_ok=True)

COUPANG_CATALOG_XLSX_PATH = BASE_DIR / "coupang_catalog_sample_2.xlsx"

# 쿠팡 파트너스 API 환경변수
CP_ACCESS_KEY = os.getenv("CP_ACCESS_KEY", "")
CP_SECRET_KEY = os.getenv("CP_SECRET_KEY", "")

CP_DOMAIN = "https://api-gateway.coupang.com"
CP_METHOD = "POST"
CP_PATH = "/v2/providers/affiliate_open_api/apis/openapi/deeplink"

app = FastAPI(title="OrderQueen Sales Importer", version="0.6.0")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

admin_security = HTTPBasic()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")


def require_admin(credentials: HTTPBasicCredentials = Depends(admin_security)):
    if not ADMIN_PASSWORD or not secrets.compare_digest(credentials.password, ADMIN_PASSWORD):
        raise HTTPException(
            status_code=401,
            detail="비밀번호가 올바르지 않습니다.",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True


def get_current_web_user(request: Request) -> dict | None:
    token = request.cookies.get(web_auth.SESSION_COOKIE_NAME)
    return web_auth.get_user_from_session(token)


def require_web_user(request: Request) -> dict:
    user = get_current_web_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    return user

init_db()
vendors.init_vendor_table()
catalog_cache.init_catalog_table()
popularity.init_popularity_table()
telegram_store.init_telegram_tables()
vendors.init_store_vendor_table()
web_auth.init_web_auth_tables()

scheduler = BackgroundScheduler(timezone="Asia/Seoul")
scheduler.add_job(
    catalog_crawler.crawl_all_enabled,
    trigger=CronTrigger(hour=4, minute=0),
    id="daily_catalog_refresh",
    replace_existing=True,
)
scheduler.start()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _ceil_to_pack(qty: int, pack_qty: int) -> int:
    if pack_qty <= 1:
        return qty
    return int(math.ceil(qty / pack_qty) * pack_qty)


def build_coupang_search_url(keyword: str) -> str:
    q = quote(keyword)
    return f"https://www.coupang.com/np/search?component=&q={q}&channel=user"


def make_signed_date() -> str:
    return datetime.now(timezone.utc).strftime("%y%m%dT%H%M%SZ")


def make_coupang_authorization(method: str, path: str, query: str, access_key: str, secret_key: str) -> str:
    signed_date = make_signed_date()
    message = f"{signed_date}{method}{path}{query}"
    signature = hmac.new(
        secret_key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return (
        f"CEA algorithm=HmacSHA256, "
        f"access-key={access_key}, "
        f"signed-date={signed_date}, "
        f"signature={signature}"
    )


def create_partners_link_from_search_keyword(keyword: str) -> str:
    if not CP_ACCESS_KEY or not CP_SECRET_KEY:
        raise RuntimeError("CP_ACCESS_KEY / CP_SECRET_KEY 환경변수가 설정되지 않았습니다.")

    source_url = build_coupang_search_url(keyword)
    authorization = make_coupang_authorization(
        method=CP_METHOD,
        path=CP_PATH,
        query="",
        access_key=CP_ACCESS_KEY,
        secret_key=CP_SECRET_KEY,
    )

    url = f"{CP_DOMAIN}{CP_PATH}"
    payload = {"coupangUrls": [source_url]}

    resp = requests.post(
        url,
        headers={
            "Authorization": authorization,
            "Content-Type": "application/json",
        },
        data=json.dumps(payload),
        timeout=30,
    )
    resp.raise_for_status()

    result = resp.json()
    if result.get("rCode") != "0":
        raise RuntimeError(f"파트너스 링크 생성 실패: {result}")

    data = result.get("data", [])
    if not data:
        raise RuntimeError(f"파트너스 링크 결과 없음: {result}")

    shorten_url = data[0].get("shortenUrl")
    if not shorten_url:
        raise RuntimeError(f"shortenUrl 없음: {result}")

    return shorten_url


class OrderQueenImportRequest(BaseModel):
    login_id: str
    login_pw: str
    period_from: date
    period_to: date
    safety_stock: int = Field(0, ge=0, le=9999, description="전 품목 공통 안전재고")
    export_xlsx: bool = Field(True, description="true면 발주 엑셀을 생성합니다.")


class OrderQueenImportResponse(BaseModel):
    job_id: str
    xlsx_path: str
    summary: dict
    top_items: list[dict]
    export_path: Optional[str] = None
    representative_product: Optional[dict] = None
    representative_partner_link: Optional[str] = None

class YamimallCartRequest(BaseModel):
    username: str
    password: str
    items: list[dict]


@app.post("/api/yamimall/cart")
def yamimall_cart(req: YamimallCartRequest):
    yamimall_items = [
        item for item in req.items
        if int(item.get("is_coupang", item.get("is_coupang_type", 0)) or 0) == 2
    ]

    if not yamimall_items:
        return {
            "ok": False,
            "message": "야미몰 장바구니 대상 품목이 없습니다.",
            "success": [],
            "failed": []
        }
    print(f"[YAMIMALL API] filtered items = {len(yamimall_items)}")

    yamimall_items = yamimall_items[:30]

    print(f"[YAMIMALL API] test items = {len(yamimall_items)}")
    
    result = add_yamimall_cart(
        username=req.username,
        password=req.password,
        items=yamimall_items
    )

    return result


class VendorCredentialsRequest(BaseModel):
    login_id: str
    login_pwd: str


class VendorEnabledRequest(BaseModel):
    enabled: bool


@app.get("/vendors", response_class=HTMLResponse)
def vendors_page(request: Request):
    return templates.TemplateResponse("vendors.html", {"request": request})


@app.get("/api/vendors")
def api_list_vendors():
    return {"vendors": vendors.list_vendors()}


@app.post("/api/vendors/{vendor_id}/credentials")
def api_set_vendor_credentials(vendor_id: str, req: VendorCredentialsRequest):
    try:
        vendors.set_vendor_credentials(vendor_id, req.login_id, req.login_pwd)
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    return {"ok": True}


@app.post("/api/vendors/{vendor_id}/toggle")
def api_toggle_vendor(vendor_id: str, req: VendorEnabledRequest):
    try:
        vendors.set_vendor_enabled(vendor_id, req.enabled)
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    return {"ok": True}


class SignupRequest(BaseModel):
    email: str
    password: str
    display_name: str


class LoginRequest(BaseModel):
    email: str
    password: str


@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request):
    return templates.TemplateResponse("signup.html", {"request": request})


@app.post("/api/auth/signup")
def api_signup(req: SignupRequest, response: Response):
    ok, message = web_auth.signup(req.email, req.password, req.display_name)
    if not ok:
        return {"ok": False, "message": message}

    user_id = web_auth.verify_login(req.email, req.password)
    token = web_auth.create_session(user_id)
    response.set_cookie(
        web_auth.SESSION_COOKIE_NAME, token,
        max_age=web_auth.SESSION_TTL_DAYS * 86400, httponly=True, samesite="lax",
    )
    return {"ok": True}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/api/auth/login")
def api_login(req: LoginRequest, response: Response):
    user_id = web_auth.verify_login(req.email, req.password)
    if not user_id:
        return {"ok": False, "message": "이메일 또는 비밀번호가 올바르지 않습니다."}

    token = web_auth.create_session(user_id)
    response.set_cookie(
        web_auth.SESSION_COOKIE_NAME, token,
        max_age=web_auth.SESSION_TTL_DAYS * 86400, httponly=True, samesite="lax",
    )
    return {"ok": True}


@app.post("/api/auth/logout")
def api_logout(request: Request, response: Response):
    token = request.cookies.get(web_auth.SESSION_COOKIE_NAME)
    if token:
        web_auth.destroy_session(token)
    response.delete_cookie(web_auth.SESSION_COOKIE_NAME)
    return {"ok": True}


@app.get("/api/auth/me")
def api_me(request: Request):
    user = get_current_web_user(request)
    if not user:
        return {"logged_in": False}
    return {"logged_in": True, **user}


class PriceCompareRequest(BaseModel):
    keyword: str


@app.get("/compare", response_class=HTMLResponse)
def compare_page(request: Request):
    if not get_current_web_user(request):
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("compare.html", {"request": request, "active_page": "compare"})


@app.post("/api/price-compare")
def api_price_compare(req: PriceCompareRequest, _: dict = Depends(require_web_user)):
    keyword = req.keyword.strip()
    if not keyword:
        return {"keyword": "", "vendors": [], "groups": []}

    result = price_compare.compare(keyword)
    return {"keyword": keyword, **result}


@app.get("/api/catalog/status")
def api_catalog_status():
    return {"status": catalog_cache.get_refresh_status()}


@app.post("/api/catalog/refresh")
def api_catalog_refresh():
    # FastAPI BackgroundTasks가 Playwright 호출 도중 조용히 멈추는 문제가 있어(텔레그램 봇에서도
    # 동일 증상 확인), 이미 안정적으로 동작 중인 APScheduler 스레드로 즉시 실행 작업을 넘긴다.
    scheduler.add_job(catalog_crawler.crawl_all_enabled, id="manual_catalog_refresh", replace_existing=True)
    return {"ok": True, "message": "백그라운드에서 크롤링을 시작했습니다. /api/catalog/status로 진행상황을 확인하세요."}


class CartAddRequest(BaseModel):
    vendor_id: str
    product_url: str
    qty: int = Field(1, ge=1, le=99)
    item_name: str = ""
    item_key: str = ""


@app.post("/api/cart-add")
def api_cart_add(req: CartAddRequest, user: dict = Depends(require_web_user)):
    store_id = f"web:{user['email']}"

    creds = vendors.get_store_vendor_credentials(store_id, req.vendor_id)
    if not creds:
        vendor_name = vendors.VENDORS.get(req.vendor_id, {}).get("name", req.vendor_id)
        return {"ok": False, "reason": f"{vendor_name} 계정이 등록되어 있지 않습니다. '내 도매처 계정'에서 먼저 등록해주세요."}
    login_id, login_pwd = creds

    if req.vendor_id == "yamimall":
        result = yamimall_bot.add_to_cart(login_id, login_pwd, req.product_url, req.qty)
    elif req.vendor_id in ("ccdome", "3bong"):
        base_url = vendors.VENDORS[req.vendor_id]["base_url"]
        goods_no_match = re.search(r"goodsNo=(\d+)", req.product_url or "")
        if not goods_no_match:
            return {"ok": False, "reason": f"상품 번호 추출 실패: {req.product_url}"}
        result = godomall_bot.add_to_cart(base_url, login_id, login_pwd, goods_no_match.group(1), req.qty)
    else:
        return {"ok": False, "reason": f"{req.vendor_id}는 아직 자동 담기를 지원하지 않습니다."}

    if result.get("ok"):
        popularity.log_event(
            store_id, "wholesale",
            req.item_key or req.product_url, req.item_name, req.qty,
        )

    return result


@app.get("/my-vendors", response_class=HTMLResponse)
def my_vendors_page(request: Request):
    if not get_current_web_user(request):
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("my_vendors.html", {"request": request, "active_page": "vendors"})


@app.get("/api/my-vendors")
def api_my_vendors_status(user: dict = Depends(require_web_user)):
    store_id = f"web:{user['email']}"
    return {"vendors": vendors.list_store_vendor_status(store_id)}


class MyVendorCredentialsRequest(BaseModel):
    login_id: str
    login_pwd: str


@app.post("/api/my-vendors/{vendor_id}/credentials")
def api_my_vendors_save(vendor_id: str, req: MyVendorCredentialsRequest, user: dict = Depends(require_web_user)):
    store_id = f"web:{user['email']}"
    try:
        vendors.set_store_vendor_credentials(store_id, vendor_id, req.login_id, req.login_pwd)
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    return {"ok": True}


@app.get("/popular", response_class=HTMLResponse)
def popular_page(request: Request):
    if not get_current_web_user(request):
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("popular.html", {"request": request, "active_page": "popular"})


@app.get("/home", response_class=HTMLResponse)
def home_page(request: Request):
    if not get_current_web_user(request):
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("home.html", {"request": request, "active_page": "home"})


@app.get("/api/popular")
def api_popular(category: str = Query(...), limit: int = Query(30, ge=1, le=100)):
    if category not in popularity.CATEGORIES:
        return {"items": []}
    return {"items": popularity.get_top_items(category, limit=limit)}


@app.post("/telegram/webhook")
def telegram_webhook(update: dict):
    try:
        telegram_bot.handle_update(update)
    except Exception as e:
        print("[TELEGRAM] webhook 처리 실패:", e)
    return {"ok": True}


@app.get("/telegram/admin", response_class=HTMLResponse)
def telegram_admin_page(request: Request, _: bool = Depends(require_admin)):
    return templates.TemplateResponse("telegram_admin.html", {"request": request})


@app.get("/api/telegram/stores")
def api_telegram_stores(_: bool = Depends(require_admin)):
    return {"stores": telegram_store.list_stores()}


class TelegramApproveRequest(BaseModel):
    store_name: str


@app.post("/api/telegram/stores/{chat_id}/approve")
def api_telegram_approve(chat_id: str, req: TelegramApproveRequest, _: bool = Depends(require_admin)):
    telegram_store.approve_store(chat_id, req.store_name)
    return {"ok": True}


@app.post("/api/telegram/stores/{chat_id}/revoke")
def api_telegram_revoke(chat_id: str, _: bool = Depends(require_admin)):
    telegram_store.revoke_store(chat_id)
    return {"ok": True}


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request, _: bool = Depends(require_admin)):
    return templates.TemplateResponse("admin_users.html", {"request": request})


@app.get("/api/admin/web-users")
def api_admin_web_users(_: bool = Depends(require_admin)):
    approved_stores = [s["store_name"] for s in telegram_store.list_stores() if s["approved"]]
    return {"users": web_auth.list_users(), "approved_stores": approved_stores}


class LinkStoreRequest(BaseModel):
    store_name: str | None = None


@app.post("/api/admin/web-users/{user_id}/link")
def api_admin_link_store(user_id: int, req: LinkStoreRequest, _: bool = Depends(require_admin)):
    web_auth.link_store(user_id, req.store_name)
    return {"ok": True}


class InventoryUpdateRequest(BaseModel):
    barcode: str
    menu_name: str
    current_stock: int
    box_qty: int = 1


@app.post("/api/inventory/update")
def update_inventory(req: InventoryUpdateRequest):
    upsert_inventory(
        barcode=req.barcode,
        menu_name=req.menu_name,
        current_stock=req.current_stock,
        box_qty=req.box_qty,
        change_type="ADJUST",
        memo="화면에서 현재재고 수동 입력"
    )

    return {"ok": True}

def make_sales_qty_map(items: list[dict]) -> dict:
    result = {}

    for item in items:
        barcode = str(item.get("바코드번호", "") or "").strip().replace(".0", "")
        if not barcode:
            continue

        result[barcode] = int(item.get("판매수량", 0) or 0)

    return result

def load_coupang_catalog_for_search() -> pd.DataFrame:
    if not COUPANG_CATALOG_XLSX_PATH.exists():
        raise FileNotFoundError(f"Catalog file not found: {COUPANG_CATALOG_XLSX_PATH}")

    df = pd.read_excel(COUPANG_CATALOG_XLSX_PATH)

    # 컬럼 없을 때 대비
    expected_columns = [
        "version", "updated_at", "barcode", "menu_code", "menu_name",
        "is_coupang", "recommended_price", "search_keyword", "fixed_url",
        "pack_qty", "min_order", "priority", "notes", "category"
    ]

    for col in expected_columns:
        if col not in df.columns:
            df[col] = ""

    # 검색용 문자열 컬럼 정리
    for col in ["barcode", "menu_code", "menu_name", "search_keyword", "fixed_url", "notes"]:
        df[col] = df[col].fillna("").astype(str)

    return df

@app.get("/", response_class=HTMLResponse)
def order_page(request: Request):
    return templates.TemplateResponse("order.html", {"request": request})

@app.get("/api/products/search")
def search_products(q: str = Query(..., min_length=1)):
    keyword = q.strip().lower()

    if not keyword:
        return {"items": []}

    df = load_coupang_catalog_for_search()

    matched = df[
        df["menu_name"].str.lower().str.contains(keyword, na=False)
        | df["barcode"].str.lower().str.contains(keyword, na=False)
        | df["search_keyword"].str.lower().str.contains(keyword, na=False)
        | df["menu_code"].str.lower().str.contains(keyword, na=False)
    ].copy()

    # 우선순위 정렬: priority 낮은 숫자가 우선이라고 가정
    if "priority" in matched.columns:
        try:
            matched["priority_num"] = pd.to_numeric(matched["priority"], errors="coerce")
            matched = matched.sort_values(["priority_num", "menu_name"], na_position="last")
        except Exception:
            matched = matched.sort_values(["menu_name"])
    else:
        matched = matched.sort_values(["menu_name"])

    # NaN -> None 또는 공란 처리
    def clean_value(v):
        if pd.isna(v):
            return ""
        return v

    items = []
    for _, row in matched.head(50).iterrows():
        items.append({
            "version": clean_value(row.get("version")),
            "updated_at": clean_value(row.get("updated_at")),
            "barcode": clean_value(row.get("barcode")),
            "menu_code": clean_value(row.get("menu_code")),
            "menu_name": clean_value(row.get("menu_name")),
            "is_coupang": clean_value(row.get("is_coupang")),
            "recommended_price": clean_value(row.get("recommended_price")),
            "search_keyword": clean_value(row.get("search_keyword")),
            "fixed_url": clean_value(row.get("fixed_url")),
            "pack_qty": clean_value(row.get("pack_qty")),
            "min_order": clean_value(row.get("min_order")),
            "priority": clean_value(row.get("priority")),
            "notes": clean_value(row.get("notes")),
            "category": clean_value(row.get("category")),
        })

    return {"items": items}

@app.get("/docs-link")
def docs_link():
    return {"docs": "http://127.0.0.1:8000/docs"}


@app.get("/export/{job_id}")
def download_export(job_id: str):
    candidates = sorted(
        EXPORT_DIR.glob(f"{job_id}_*.xlsx"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return {"ok": False, "message": "export file not found"}

    return FileResponse(
        path=str(candidates[0]),
        filename=candidates[0].name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.post("/import/orderqueen", response_model=OrderQueenImportResponse)
def import_from_orderqueen(req: OrderQueenImportRequest):
    job_id = uuid.uuid4().hex[:8]
    sales_xlsx_path = str(DOWNLOAD_DIR / f"아이즈크림 오산세교_{job_id}.xlsx")

    # 1) 주문퀸 다운로드
    download_orderqueen_xlsx(
        login_id=req.login_id,
        login_pw=req.login_pw,
        period_from=req.period_from,
        period_to=req.period_to,
        save_path=sales_xlsx_path,
    )

    # 2) 판매 데이터 파싱
    _, summary, top_items = parse_menu_sales_xlsx(
        xlsx_path=sales_xlsx_path,
        period_from=req.period_from,
        period_to=req.period_to,
    )

    # 추가: 2주/3주/4주 판매 데이터 다운로드 및 파싱
    period_to = req.period_to

    period_from_2w = period_to - timedelta(days=13)
    period_from_3w = period_to - timedelta(days=20)
    period_from_4w = period_to - timedelta(days=27)

    sales_xlsx_path_2w = str(DOWNLOAD_DIR / f"sales_{job_id}_2w.xlsx")
    sales_xlsx_path_3w = str(DOWNLOAD_DIR / f"sales_{job_id}_3w.xlsx")
    sales_xlsx_path_4w = str(DOWNLOAD_DIR / f"sales_{job_id}_4w.xlsx")

    download_orderqueen_xlsx(
        login_id=req.login_id,
        login_pw=req.login_pw,
        period_from=period_from_2w,
        period_to=period_to,
        save_path=sales_xlsx_path_2w,
    )

    download_orderqueen_xlsx(
        login_id=req.login_id,
        login_pw=req.login_pw,
        period_from=period_from_3w,
        period_to=period_to,
        save_path=sales_xlsx_path_3w,
    )

    download_orderqueen_xlsx(
        login_id=req.login_id,
        login_pw=req.login_pw,
        period_from=period_from_4w,
        period_to=period_to,
        save_path=sales_xlsx_path_4w,
    )

    _, _, top_items_2w = parse_menu_sales_xlsx(
        xlsx_path=sales_xlsx_path_2w,
        period_from=period_from_2w,
        period_to=period_to,
    )

    _, _, top_items_3w = parse_menu_sales_xlsx(
        xlsx_path=sales_xlsx_path_3w,
        period_from=period_from_3w,
        period_to=period_to,
    )

    _, _, top_items_4w = parse_menu_sales_xlsx(
        xlsx_path=sales_xlsx_path_4w,
        period_from=period_from_4w,
        period_to=period_to,
    )

    sales_qty_map_2w = make_sales_qty_map(top_items_2w)
    sales_qty_map_3w = make_sales_qty_map(top_items_3w)
    sales_qty_map_4w = make_sales_qty_map(top_items_4w)


    # 3) 안전재고 반영
    safety = int(req.safety_stock or 0)
    for item in top_items:
        seven = int(item.get("7일예상수량", 0) or 0)
        item["안전재고"] = safety
        item["추천발주량"] = max(0, seven + safety)

    # 4) 쿠팡 카탈로그 로드
    catalog = load_coupang_catalog_xlsx(COUPANG_CATALOG_XLSX_PATH)

    for item in top_items:
        barcode = str(item.get("바코드번호", "") or "").strip().replace(".0", "")
        cat = catalog.get(barcode) if barcode else None

        item["2주판매수량"] = sales_qty_map_2w.get(barcode, 0)
        item["3주판매수량"] = sales_qty_map_3w.get(barcode, 0)
        item["4주판매수량"] = sales_qty_map_4w.get(barcode, 0)

        if cat:
            order_type = int(cat.is_coupang or 0)

            item["is_coupang_item"] = order_type == 1
            item["is_coupang"] = order_type
            item["catalog_menu_name"] = cat.menu_name
            item["catalog_search_keyword"] = cat.search_keyword
            item["catalog_fixed_url"] = cat.fixed_url
            item["pack_qty"] = int(cat.pack_qty or 1)
            item["min_order"] = int(cat.min_order or 1)
            item["notes"] = cat.notes
            item["category"] = cat.category

            base_qty = max(int(item["추천발주량"]), int(cat.min_order or 1))
            item["추천발주량_포장반영"] = _ceil_to_pack(base_qty, int(cat.pack_qty or 1))

            if order_type == 1 and cat.search_keyword:
                item["coupang_url"] = build_coupang_search_url(cat.search_keyword)
                item["coupang_link_type"] = "search_keyword"
            else:
                item["coupang_url"] = ""
                item["coupang_link_type"] = ""

        else:
            order_type = 99

            item["is_coupang_item"] = False
            item["is_coupang"] = 99
            item["catalog_menu_name"] = ""
            item["catalog_search_keyword"] = ""
            item["catalog_fixed_url"] = ""
            item["pack_qty"] = 1
            item["min_order"] = 1
            item["notes"] = ""
            item["추천발주량_포장반영"] = int(item["추천발주량"])
            item["coupang_url"] = ""
            item["coupang_link_type"] = ""

        if order_type == 1:
            item["발주구분"] = "쿠팡"
        elif order_type == 2:
            item["발주구분"] = "도매몰"
        elif order_type == 3:
            item["발주구분"] = "문구/완구"
        elif order_type == 0:
            item["발주구분"] = "아이스크림"
        else:
            item["발주구분"] = "미분류"


    # 아이스크림 재고/박스 발주 계산
        if item["is_coupang"] == 0 and cat:
            item["low_rotation"] = False
            order_reason = ""
            inventory = get_inventory(barcode)

            current_stock = inventory["current_stock"] if inventory else 0
            box_qty = int(getattr(cat, "icecream_box_qty", 0) or 0) if cat else 0
            expected_7d = int(item.get("7일예상수량", 0) or 0)

            need_qty = max(0, expected_7d - current_stock)

            if box_qty <= 0:
                item["추천박스수"] = "#N/A"
                item["포장반영"] = "#N/A"
                item["추천발주량_포장반영"] = "#N/A"
                item["주문표시"] = "#N/A"
                continue

            raw_box_ratio = need_qty / box_qty
            recommended_boxes = round(raw_box_ratio)


            if recommended_boxes == 0:
                # TODO: 추후 지난주 발주 이력 DB 연동 예정
                last_week_boxes = 0

                if last_week_boxes >= 1:
                    recommended_boxes = 0
                    order_reason = "지난주 발주 이력 있음 → 0박스 유지"

                else:

                    # TODO:
                    # 추후 2~4주 판매량 집계 연동 예정
                    two_week_sold_qty = sales_qty_map_2w.get(barcode, 0)
                    three_week_sold_qty = sales_qty_map_3w.get(barcode, 0)
                    four_week_sold_qty = sales_qty_map_4w.get(barcode, 0)
            
                    if two_week_sold_qty > box_qty * 0.5:
                        recommended_boxes = 1
                        order_reason = "2주 판매수량 0.5박스 초과 → 1박스 보정"

                    elif three_week_sold_qty > box_qty * 0.5:
                        recommended_boxes = 1
                        order_reason = "3주 판매수량 0.5박스 초과 → 1박스 보정"

                    elif four_week_sold_qty > box_qty * 0.5:
                        recommended_boxes = 1
                        order_reason = "4주 판매수량 0.5박스 초과 → 1박스 보정"

                    else:
                        recommended_boxes = 0
                        order_reason = "4주 판매수량도 0.5박스 이하 → 0박스 유지"
                        item["low_rotation"] = True


            # 최소 1박스가 된 경우 실제 비율 표시용
            if recommended_boxes == 1 and raw_box_ratio < 1:
                box_display = f"1박스 ({raw_box_ratio:.2f})"
            else:
                box_display = f"{recommended_boxes}박스"

            recommended_qty = recommended_boxes * box_qty

            item["현재재고"] = current_stock
            item["박스입수"] = box_qty
            item["필요수량"] = need_qty
            item["추천박스수"] = box_display
            item["포장반영"] = recommended_boxes
            item["추천발주량_포장반영"] = recommended_qty
            item["주문표시"] = box_display
            item["발주판단사유"] = order_reason


    # 5) 대표 상품 선택
    representative_product = None
    representative_partner_link = None

    coupang_candidates = [x for x in top_items if x.get("is_coupang") == 1]

    if coupang_candidates:
        representative_item = max(
            coupang_candidates,
            key=lambda x: int(x.get("판매수량", 0) or 0)
        )

        representative_product = {
            "barcode": representative_item.get("바코드번호", ""),
            "menu_code": representative_item.get("메뉴코드", ""),
            "menu_name": representative_item.get("메뉴명", ""),
            "fixed_url": representative_item.get("catalog_fixed_url", ""),
            "search_keyword": representative_item.get("catalog_search_keyword", ""),
        }

        rep_keyword = representative_item.get("catalog_search_keyword", "") or representative_item.get("메뉴명", "")

        if rep_keyword:
            try:
                representative_partner_link = create_partners_link_from_search_keyword(rep_keyword)
            except Exception as e:
                print("쿠팡 파트너스 링크 생성 실패:", e)
                representative_partner_link = ""


    # 6) 요약
    summary = dict(summary)
    summary["safety_stock"] = safety
    summary["recommend_rule"] = "추천발주량 = 7일예상수량 + safety_stock"
    summary["catalog_path"] = str(COUPANG_CATALOG_XLSX_PATH)

    summary["아이스크림품목수"] = int(sum(1 for x in top_items if x.get("is_coupang") == 0))
    summary["쿠팡품목수"] = int(sum(1 for x in top_items if x.get("is_coupang") == 1))
    summary["도매몰품목수"] = int(sum(1 for x in top_items if x.get("is_coupang") == 2))
    summary["문구완구품목수"] = int(sum(1 for x in top_items if x.get("is_coupang") == 3))

    # 인기상품 집계용 이력 기록 (전 가맹점 합산 TOP30 계산에 사용)
    for item in top_items:
        qty = int(item.get("판매수량", 0) or 0)
        if qty <= 0:
            continue
        barcode = str(item.get("바코드번호", "") or "").strip()
        name = str(item.get("메뉴명", "") or "")
        if item.get("is_coupang") == 0:
            popularity.log_event(req.login_id, "icecream", barcode or name, name, qty)
        elif item.get("is_coupang") == 1:
            popularity.log_event(req.login_id, "coupang", barcode or name, name, qty)

    # 7) 엑셀 생성
    export_path = None
    if req.export_xlsx:
        df_all = pd.DataFrame(top_items)

        preferred_cols = [
            "sku_key", "메뉴코드", "바코드번호", "메뉴명",
            "판매수량", "7일예상수량", "안전재고", "추천발주량",
            "pack_qty", "min_order", "추천발주량_포장반영",
            "is_coupang_item", "catalog_search_keyword", "catalog_fixed_url",
            "coupang_url", "coupang_link_type", "notes",
        ]
        cols = [c for c in preferred_cols if c in df_all.columns] + [c for c in df_all.columns if c not in preferred_cols]
        df_all = df_all[cols]

        df_coupang = df_all[df_all["is_coupang_item"] == True].copy()
        df_other = df_all[df_all["is_coupang_item"] == False].copy()

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        export_path = str(EXPORT_DIR / f"{job_id}_발주추천_{ts}.xlsx")

        with pd.ExcelWriter(export_path, engine="openpyxl") as w:
            df_coupang.to_excel(w, index=False, sheet_name="쿠팡발주")
            df_other.to_excel(w, index=False, sheet_name="비쿠팡")
            pd.DataFrame([summary]).to_excel(w, index=False, sheet_name="요약")
            if representative_product:
                pd.DataFrame([representative_product]).to_excel(w, index=False, sheet_name="대표상품")

    return {
        "job_id": job_id,
        "xlsx_path": sales_xlsx_path,
        "summary": summary,
        "top_items": top_items,
        "export_path": export_path,
        "representative_product": representative_product,
        "representative_partner_link": representative_partner_link,
    }