# main.py
from __future__ import annotations

import os
from dotenv import load_dotenv

# 프로젝트 내부 모듈들(product_ranking, telegram_bot 등)이 import 시점에
# os.getenv로 API 키를 읽기 때문에, load_dotenv()는 그 import보다 먼저 실행돼야
# 한다 - 순서가 뒤바뀌면 로컬 개발 환경(.env 파일)에서는 항상 빈 값으로 읽힌다
# (Render 배포 환경은 .env 없이 실제 환경변수를 바로 주입하므로 이 순서 문제와
# 무관하게 정상 동작해서 지금까지 드러나지 않았음).
load_dotenv()

import functools
import math
import re
import traceback
import uuid
import hmac
import hashlib
import json
from datetime import date, datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import pandas as pd
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
import secrets

from fastapi import BackgroundTasks, Depends, FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from orderqueen_bot import download_orderqueen_xlsx
from parser import parse_menu_sales_xlsx
import mapping
from mapping import select_representative_item
from db import init_db, get_inventory, upsert_inventory, change_stock

from yamimall_bot import add_yamimall_cart
import biz_tools
import browser_limit
import cafe24_bot
import cart_add_logic
import cart_jobs
import catalog_cache
import catalog_crawler
import consumables
import godomall_bot
import patch_notes
import popularity
import product_ranking
import store_reports
import telegram_bot
import telegram_store
import vendors
import price_compare
import web_auth
import web_cart
import yamimall_bot

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
EXPORT_DIR = BASE_DIR / "exports"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
EXPORT_DIR.mkdir(parents=True, exist_ok=True)


# 쿠팡 파트너스 API 환경변수
CP_ACCESS_KEY = os.getenv("CP_ACCESS_KEY", "")
CP_SECRET_KEY = os.getenv("CP_SECRET_KEY", "")

CP_DOMAIN = "https://api-gateway.coupang.com"
CP_METHOD = "POST"
CP_PATH = "/v2/providers/affiliate_open_api/apis/openapi/deeplink"

app = FastAPI(title="OrderQueen Sales Importer", version="0.6.0")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/images", StaticFiles(directory=str(BASE_DIR / "templates" / "images")), name="images")

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
vendors.init_session_table()
vendors.init_store_vendor_prefs_table()
web_auth.init_web_auth_tables()
product_ranking.init_table(product_ranking.BEVERAGE)
product_ranking.init_table(product_ranking.SNACK)
product_ranking.init_price_tracking_tables()
product_ranking.init_search_api_rate_limit_table()
biz_tools.init_table()
consumables.init_table()
mapping.init_catalog_table()
mapping.init_unclassified_queue_table()
patch_notes.init_patch_notes_table()
web_cart.init_web_cart_table()
cart_jobs.init_cart_jobs_table()
store_reports.init_store_report_tables()
store_reports.init_manual_report_table()

scheduler = BackgroundScheduler(timezone="Asia/Seoul")
# CronTrigger를 직접 만들어서 trigger=로 넘기면 scheduler의 timezone을 자동으로
# 물려받지 않고 CronTrigger 자체의 기본값(서버의 로컬 타임존)을 쓴다 - Render
# 서버가 UTC라 "새벽 4시"로 짠 작업들이 실제로는 UTC 4시(=한국시간 오후 1시,
# 한창 영업시간)에 돌고 있었다(음료 추천 백필이 한 번도 안 된 원인). 각
# CronTrigger에 timezone을 명시해서 실제로 한국시간 새벽 4시에 돌도록 고친다.
KST = "Asia/Seoul"
scheduler.add_job(
    catalog_crawler.crawl_all_enabled,
    trigger=CronTrigger(hour=4, minute=0, timezone=KST),
    id="daily_catalog_refresh",
    replace_existing=True,
)
# 쿠팡 상품검색 API로 아직 기준 URL이 없는 상품만 채운다. 파트너스 링크는
# 만료되지 않는 고정 링크라 한 번 채워지면(또는 사람이 수동 고정하면)
# 다시 검색하지 않는다 - 카탈로그가 그대로면 둘째 날부터는 호출이 0에
# 수렴한다. 이 API는 시간당 호출 한도가 엄격해서(초과 시 최대 24시간
# 잠기고 3회 누적되면 계정 자체가 제한됨) 자주 돌리면 안 되지만, 위와
# 같은 이유로 매일 돌려도 안전하다.
#
# 상품검색 결과의 productUrl 자체가 이미 파트너스 추적 태그가 붙은 링크라
# (link.coupang.com/re/AFFSDP?lptag=... 형태) 여기서 바로 partners_link로도
# 저장한다 - 별도 딥링크 변환 API를 부를 필요가 없다(오히려 이미 변환된
# 링크를 다시 변환하려 하면 "url convert failed"로 실패한다는 걸 실측으로
# 확인함).
scheduler.add_job(
    functools.partial(product_ranking.refresh_products, product_ranking.BEVERAGE),
    trigger=CronTrigger(hour=4, minute=0, timezone=KST),
    id="daily_beverage_product_backfill",
    replace_existing=True,
)
scheduler.add_job(
    functools.partial(product_ranking.refresh_products, product_ranking.SNACK),
    trigger=CronTrigger(hour=4, minute=10, timezone=KST),
    id="daily_snack_product_backfill",
    replace_existing=True,
)

# 가격 추이 트래킹: 이미 매칭된 상품들의 오늘자 가격을 순환 조회해서 쌓는다.
# 226개(음료 149 + 과자 77) 전체를 한 번에 조회하면 시간당 호출 한도를
# 넘으므로, 15개씩 30분 간격으로 나눠 돈다 - 시간당 30건(실측 한도 약
# 90건/시간 대비 3배 여유), 전체 카탈로그 한 바퀴 도는 데 약 8시간이라
# 하루 2~3회 자연스럽게 갱신된다.
#
# 쿠팡 가격은 실시간으로 바뀌기 때문에, 신규 최저가는 하루치를 모았다가
# 한 번에 보내는 게 아니라 스캔 주기(30분)마다 감지 즉시 대표님 개인
# 텔레그램으로 보낸다(전체 가맹점 발송 여부는 대표님이 직접 결정 -
# telegram_bot.py의 관리자 응답 처리에서 "전체발송"/"생략"으로 확정).
def _notify_price_alerts() -> None:
    alerts = product_ranking.list_notifiable_alerts()
    if not alerts or not telegram_bot.ADMIN_CHAT_ID:
        return
    lines = ["🎉 신규 최저가 감지!\n"]
    for a in alerts:
        old_low_text = f"{a['old_low']:,}원 → " if a["old_low"] else ""
        lines.append(f"• {a['item_name']} {old_low_text}{a['new_price']:,}원")
    lines.append("\n전체 매장에 보내려면 '전체발송', 넘어가려면 '생략'이라고 답장해주세요.")
    telegram_bot.send_message(telegram_bot.ADMIN_CHAT_ID, "\n".join(lines))
    product_ranking.mark_alerts_notified([a["id"] for a in alerts])


def _run_price_snapshot_and_notify(pt: product_ranking.ProductType) -> None:
    try:
        product_ranking.snapshot_prices(pt, limit=15)
        _notify_price_alerts()
    except Exception as e:
        telegram_bot.alert_admin(f"가격 스냅샷/알림 작업 실패 ({pt.table_name}): {e}")
        raise


scheduler.add_job(
    functools.partial(_run_price_snapshot_and_notify, product_ranking.BEVERAGE),
    trigger=IntervalTrigger(minutes=30),
    id="price_snapshot_beverage",
    replace_existing=True,
)
scheduler.add_job(
    functools.partial(_run_price_snapshot_and_notify, product_ranking.SNACK),
    trigger=IntervalTrigger(minutes=30),
    id="price_snapshot_snack",
    replace_existing=True,
)


# 자동 발주 리포트 예약: 지점마다 요일/시각이 달라 개별 add_job 대신 15분마다
# 도는 틱 하나가 store_report_schedules를 훑어 지금 쏴야 할 예약을 찾는다.
# (앱이 재시작돼도 DB만 보고 판단하므로 안전 - 인메모리 job 등록 상태에
# 의존하지 않는다.)
def _resolve_chat_id_for_store(store_id: str) -> str | None:
    if not store_id.startswith("web:"):
        return None
    email = store_id[len("web:"):]
    linked_store_name = web_auth.get_linked_store_name_by_email(email)
    if not linked_store_name:
        return None
    matches = [
        s for s in telegram_store.list_stores()
        if s["approved"] and s["store_name"] == linked_store_name
    ]
    if len(matches) != 1:
        return None
    return matches[0]["chat_id"]


def _report_branch_label(report: dict) -> str:
    return f" [{report['account_nickname']}]" if report.get("account_nickname") else ""


def _format_wholesale_report_message(report: dict) -> str:
    """도매처 메시지는 담을 게 없어도 항상 보낸다 - 여기서 안 보내면 쿠팡/
    아이스크림만 오고 도매처만 조용히 빠져서, 정상적으로 60% 미달이라 아무것도
    없는 건지 뭔가 고장난 건지 사용자가 구분할 수 없다."""
    wholesale = report["wholesale_items"]
    unknown = report.get("unknown_pack_items") or []

    lines = [f"📦{_report_branch_label(report)} 도매처 자동 발주 리포트 ({report['period_from']} ~ {report['period_to']} 집계)\n"]

    if wholesale:
        lines.append("[도매처 담기 대상]")
        for idx, it in enumerate(wholesale, start=1):
            lines.append(f"{idx}. {it['name']} {it['cases']}타 ({it['vendor_name']}, {it['sold_qty']}개 판매)")
        lines.append("")

    if unknown:
        lines.append("[1타 수량 미확인 - 참고용, 담기 대상 아님]")
        for it in unknown:
            lines.append(f"- {it['name']} ({it['vendor_name']}, {it['sold_qty']}개 판매)")
        lines.append("")

    if wholesale:
        lines.append("전체 담기: '확인' / 이번엔 넘어가기: '스킵'")
        lines.append("특정 항목 빼기: '3 빼줘' / 수량 수정: '1 4타로'")
        lines.append("(취소하려면 '취소')")
    else:
        lines.append("이번 집계에서는 1타(구매 단위) 대비 60% 이상 팔린 도매처 품목이 없습니다.")
        lines.append("판매량은 이월되어 다음 집계에 이어서 반영됩니다.")

    return "\n".join(lines)


def _format_coupang_report_message(report: dict) -> str | None:
    coupang = report["coupang_items"]
    if not coupang:
        return None

    lines = [f"🛒{_report_branch_label(report)} 쿠팡 구매 링크 ({report['period_from']} ~ {report['period_to']} 집계)\n"]
    lines.append("*본 링크를 통하여 구매를 진행하실 경우 쿠팡 파트너스 활동의 일환으로 그에 따른 일정액의 수수료를 제공받습니다.\n")
    for idx, it in enumerate(coupang, start=1):
        price_text = f" ({it['price']:,}원)" if it.get("price") else ""
        link = it.get("partners_link") or ""
        lines.append(f"{idx}. {it['name']}{price_text} ({it['sold_qty']}개 판매)\n{link}")

    return "\n".join(lines)


def _format_icecream_report_message(report: dict) -> str | None:
    icecream = report["icecream_items"]
    if not icecream:
        return None

    lines = [f"🍦{_report_branch_label(report)} 아이스크림 참고 판매량 ({report['period_from']} ~ {report['period_to']} 집계)\n"]
    for it in icecream:
        lines.append(f"- {it['name']} {it['cases']}박스 (판매기준)")

    return "\n".join(lines)


def _run_store_report_tick() -> None:
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    due = store_reports.list_due_schedules(now)
    for sched in due:
        store_id = sched["store_id"]
        schedule_id = sched["id"]
        account_id = sched.get("account_id")
        try:
            chat_id = _resolve_chat_id_for_store(store_id)
            if not chat_id:
                telegram_bot.alert_admin(f"자동 발주 리포트: {store_id}에 연결된 텔레그램 지점을 찾지 못했습니다.")
                continue

            report = store_reports.generate_report(store_id, account_id)
            if not report.get("ok"):
                telegram_bot.send_message(chat_id, f"자동 발주 리포트 생성에 실패했습니다.\n\n{report.get('reason')}")
                continue

            # 도매처/쿠팡/아이스크림을 3개 메시지로 나눠 보낸다 - 확인/스킵/수정
            # 안내는 실제로 담기 대상인 도매처 메시지에만 붙는다.
            sent_any = False
            for builder in (_format_wholesale_report_message, _format_coupang_report_message, _format_icecream_report_message):
                msg = builder(report)
                if msg:
                    telegram_bot.send_message(chat_id, msg)
                    sent_any = True

            if not sent_any:
                telegram_bot.send_message(
                    chat_id,
                    f"📦{_report_branch_label(report)} 자동 발주 리포트 ({report['period_from']} ~ {report['period_to']} 집계)\n\n"
                    "이번 집계에서는 발주 대상 상품이 없습니다.",
                )

            if report["wholesale_items"]:
                telegram_store.set_disambig_state(chat_id, {
                    "mode": "report_confirm",
                    "current": True,
                    "store_id": store_id,
                    "report_key": report["report_key"],
                    "wholesale_items": report["wholesale_items"],
                })
        except Exception as e:
            telegram_bot.alert_admin(f"자동 발주 리포트 처리 실패 (store_id={store_id}): {e}")
        finally:
            store_reports.mark_schedule_fired(schedule_id, now.date().isoformat())


scheduler.add_job(
    _run_store_report_tick,
    trigger=IntervalTrigger(minutes=15),
    id="store_report_tick",
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


@app.exception_handler(Exception)
async def _alert_admin_on_unhandled_exception(request: Request, exc: Exception):
    """예상 못한 예외(진짜 버그)만 대표님 텔레그램으로 즉시 알린다. HTTPException
    등 이미 의도적으로 처리되는 예외는 FastAPI가 이 핸들러보다 먼저 자체
    처리하므로 여기까지 안 온다."""
    tb = traceback.format_exc()
    telegram_bot.alert_admin(
        f"웹 서비스에서 처리되지 않은 예외\n{request.method} {request.url.path}\n\n"
        f"{type(exc).__name__}: {exc}\n\n{tb[-1500:]}"
    )
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


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
    period_from: date
    period_to: date
    safety_stock: int = Field(0, ge=0, le=9999, description="전 품목 공통 안전재고")
    export_xlsx: bool = Field(True, description="true면 발주 엑셀을 생성합니다.")
    account_id: Optional[int] = Field(None, description="다점포 점주가 지점(오더퀸 계정)을 지정할 때. 안 주면 기본 계정.")


class OrderQueenImportResponse(BaseModel):
    job_id: str
    xlsx_path: str
    summary: dict
    top_items: list[dict]
    export_path: Optional[str] = None
    representative_product: Optional[dict] = None
    representative_partner_link: Optional[str] = None
    account_id: Optional[int] = None
    account_nickname: Optional[str] = None

class YamimallCartRequest(BaseModel):
    items: list[dict]


@app.post("/api/yamimall/cart")
def yamimall_cart(req: YamimallCartRequest, user: dict = Depends(require_web_user)):
    store_id = f"web:{user['email']}"
    creds = vendors.get_store_vendor_credentials(store_id, "yamimall")
    if not creds:
        return {
            "ok": False,
            "message": "야미몰 계정이 등록되어 있지 않습니다. '내 도매처 계정'에서 먼저 등록해주세요.",
            "success": [],
            "failed": []
        }
    username, password = creds

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
        username=username,
        password=password,
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
def api_price_compare(req: PriceCompareRequest, user: dict = Depends(require_web_user)):
    keyword = req.keyword.strip()
    if not keyword:
        return {"keyword": "", "vendors": [], "groups": []}

    store_id = f"web:{user['email']}"
    result = price_compare.compare(keyword)
    disabled_vendors, _ = vendors.get_store_vendor_prefs(store_id)
    groups = price_compare.filter_groups_for_store(result.get("groups", []), disabled_vendors)
    return {"keyword": keyword, "vendors": result.get("vendors", []), "groups": groups}


@app.get("/api/catalog/status")
def api_catalog_status():
    return {"status": catalog_cache.get_refresh_status(), "crawling": catalog_crawler.is_crawl_running()}


@app.post("/api/catalog/refresh")
def api_catalog_refresh():
    # FastAPI BackgroundTasks가 Playwright 호출 도중 조용히 멈추는 문제가 있어(텔레그램 봇에서도
    # 동일 증상 확인), 이미 안정적으로 동작 중인 APScheduler 스레드로 즉시 실행 작업을 넘긴다.
    if catalog_crawler.is_crawl_running():
        return {"ok": False, "message": "이미 크롤링이 진행 중입니다. 완료된 뒤 다시 시도해주세요."}
    scheduler.add_job(catalog_crawler.crawl_all_enabled, id="manual_catalog_refresh", replace_existing=True)
    return {"ok": True, "message": "백그라운드에서 크롤링을 시작했습니다. /api/catalog/status로 진행상황을 확인하세요."}


class CartAddRequest(BaseModel):
    vendor_id: str
    vendor_name: str = ""
    product_url: str
    qty: int = Field(1, ge=1, le=99)
    item_name: str = ""
    item_key: str = ""
    account_id: Optional[int] = None


@app.post("/api/cart-add")
def api_cart_add(req: CartAddRequest, user: dict = Depends(require_web_user)):
    """실제 담기는 별도 워커 프로세스가 처리한다 - 여기서는 큐에 등록만 하고
    job_id를 돌려주면, 프론트가 GET /api/cart-jobs/{job_id}를 폴링해서
    완료 결과를 받는다(자동 도매처 전환 없이 지정된 도매처로만 단발 시도)."""
    store_id = f"web:{user['email']}"

    if req.vendor_id not in vendors.CART_SUPPORTED_VENDORS:
        return {"ok": False, "reason": f"{req.vendor_id}는 아직 자동 담기를 지원하지 않습니다."}

    accounts = vendors.list_store_vendor_accounts(store_id, req.vendor_id)
    if not accounts:
        vendor_name = vendors.VENDORS.get(req.vendor_id, {}).get("name", req.vendor_id)
        return {"ok": False, "reason": f"{vendor_name} 계정이 등록되어 있지 않습니다. '내 도매처 계정'에서 먼저 등록해주세요."}
    if len(accounts) >= 2 and req.account_id is None:
        # 계정이 여러 개면 어떤 계정으로 담을지부터 골라야 한다 - 아직 실행하지
        # 않고 프론트에서 고른 뒤 account_id를 채워 다시 호출하게 한다.
        return {"ok": False, "needs_account_choice": True, "accounts": accounts}

    item = {
        "vendor_id": req.vendor_id, "vendor_name": req.vendor_name, "product_url": req.product_url,
        "item_key": req.item_key, "item_name": req.item_name, "qty": req.qty, "account_id": req.account_id,
    }
    job_id = cart_jobs.enqueue_web_item(store_id, None, item, with_fallback=False)
    return {"ok": True, "job_id": job_id}


@app.get("/api/cart-jobs/{job_id}")
def api_cart_job_status(job_id: int, user: dict = Depends(require_web_user)):
    """/api/cart-add, /api/isorder-cart/{id}/add-to-vendor가 등록한 작업의
    진행 상태를 폴링한다. 다른 매장의 job_id를 넘겨받아도 못 들여다보게
    store_id가 요청자 것과 일치하는지 확인한다."""
    store_id = f"web:{user['email']}"
    job = cart_jobs.get_job(job_id)
    if not job or job["store_id"] != store_id:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
    return {"status": job["status"], "result": job["result"]}


class IsorderCartAddRequest(BaseModel):
    vendor_id: str
    vendor_name: str = ""
    product_url: str
    item_key: str = ""
    item_name: str = ""
    price: Optional[int] = None
    qty: int = Field(1, ge=1, le=99)
    # 같은 상품의 가격비교 그룹 전체 offers(compare.html이 렌더링할 때 이미 갖고
    # 있음) - 품절 시 자동 전환할 대안(alt_offers)을 계산하는 데 쓰인다.
    all_offers: list[dict] = []


@app.post("/api/isorder-cart/add")
def api_isorder_cart_add(req: IsorderCartAddRequest, user: dict = Depends(require_web_user)):
    """compare 페이지의 1차 담기 - 실제 도매몰에 담지 않고 isorder 자체 장바구니에만
    빠르게 저장한다(DB insert만 하므로 즉시 응답). 실제 도매몰 담기는 /cart
    페이지에서 실행한다."""
    store_id = f"web:{user['email']}"
    alt_offers = cart_add_logic.build_alt_offers(req.vendor_id, req.all_offers)
    item_id = web_cart.add_item(
        store_id, req.item_name, req.vendor_id, req.vendor_name,
        req.product_url, req.item_key, req.price, req.qty, alt_offers,
    )
    return {"ok": True, "id": item_id}


@app.get("/api/isorder-cart")
def api_isorder_cart_list(user: dict = Depends(require_web_user)):
    store_id = f"web:{user['email']}"
    return {"items": web_cart.list_items(store_id)}


@app.delete("/api/isorder-cart")
def api_isorder_cart_delete_all(user: dict = Depends(require_web_user)):
    store_id = f"web:{user['email']}"
    count = web_cart.delete_all_items(store_id)
    return {"ok": True, "count": count}


class IsorderCartQtyRequest(BaseModel):
    qty: int = Field(..., ge=1, le=99)


@app.put("/api/isorder-cart/{item_id}")
def api_isorder_cart_update_qty(item_id: int, req: IsorderCartQtyRequest, user: dict = Depends(require_web_user)):
    store_id = f"web:{user['email']}"
    ok = web_cart.update_qty(store_id, item_id, req.qty)
    return {"ok": ok}


@app.delete("/api/isorder-cart/{item_id}")
def api_isorder_cart_delete(item_id: int, user: dict = Depends(require_web_user)):
    store_id = f"web:{user['email']}"
    ok = web_cart.delete_item(store_id, item_id)
    return {"ok": ok}


class IsorderCartAddToVendorRequest(BaseModel):
    account_id: Optional[int] = None


@app.post("/api/isorder-cart/{item_id}/add-to-vendor")
def api_isorder_cart_add_to_vendor(
    item_id: int, req: IsorderCartAddToVendorRequest = IsorderCartAddToVendorRequest(),
    user: dict = Depends(require_web_user),
):
    """장바구니에 담아둔 상품 하나를 실제 도매몰에 담는다(Playwright 자동화,
    시간이 걸림) - 별도 워커 프로세스가 처리하므로 여기서는 큐에 등록만 하고
    job_id를 돌려준다. 선택된 도매처가 품절이면, 지금 장바구니에 이미 담긴
    다른 상품들이 쓰는 도매처 중에서만 조용히 자동 전환을 시도한다(텔레그램
    봇과 동일한 로직 - cart_add_logic 공유, 배송을 최대한 한 도매처로 몰아주기
    위함). 성공하면 워커가 장바구니에서 자동으로 제거한다."""
    store_id = f"web:{user['email']}"
    item = web_cart.get_item(store_id, item_id)
    if not item:
        return {"ok": False, "reason": "장바구니에서 찾을 수 없습니다."}

    accounts = vendors.list_store_vendor_accounts(store_id, item["vendor_id"])
    if len(accounts) >= 2 and req.account_id is None:
        # 계정이 여러 개면 실행하지 않고 프론트에서 고를 목록만 돌려준다 - 실제
        # 담기는 사용자가 계정을 고른 뒤 account_id를 채워 다시 호출할 때 실행된다.
        return {"ok": False, "needs_account_choice": True, "accounts": accounts}

    cart_item = {
        "item_name": item["item_name"], "vendor_id": item["vendor_id"],
        "vendor_name": item["vendor_name"], "product_url": item["product_url"],
        "item_key": item["item_key"], "price": item["price"], "qty": item["qty"],
        "alt_offers": item["alt_offers"], "account_id": req.account_id,
    }
    job_id = cart_jobs.enqueue_web_item(store_id, item_id, cart_item, with_fallback=True)
    return {"ok": True, "job_id": job_id}


@app.get("/cart", response_class=HTMLResponse)
def cart_page(request: Request):
    if not get_current_web_user(request):
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("cart.html", {"request": request, "active_page": "cart"})


@app.post("/admin/debug-copy-vendor-cred/{vendor_id}/{store_id}")
def admin_debug_copy_vendor_cred(vendor_id: str, store_id: str, _: bool = Depends(require_admin)):
    """진단용 임시 엔드포인트: 관리자가 등록해둔 크롤링용 계정을 지정한 store_id의
    담기용 계정으로 그대로 복사한다. 서버 안에서 복호화->재암호화만 하고 평문은
    응답에 담지 않는다. 테스트 끝나면 제거할 것."""
    creds = vendors.get_vendor_credentials(vendor_id)
    if not creds:
        return {"ok": False, "reason": f"{vendor_id} 관리자 계정이 등록되어 있지 않습니다."}
    login_id, login_pwd = creds
    vendors.set_store_vendor_credentials(store_id, vendor_id, login_id, login_pwd)
    return {"ok": True}


@app.get("/admin/debug-yamimall-login-screenshot")
def admin_debug_yamimall_login_screenshot(_: bool = Depends(require_admin)):
    """진단용 임시 엔드포인트: 야미몰 로그인 실패 시 남긴 스크린샷을 확인한다."""
    if not yamimall_bot.DEBUG_LOGIN_SCREENSHOT_PATH.exists():
        raise HTTPException(status_code=404, detail="스크린샷이 아직 없습니다.")
    return FileResponse(str(yamimall_bot.DEBUG_LOGIN_SCREENSHOT_PATH))


@app.get("/admin/debug-yamimall-search-screenshot/{item_code}")
def admin_debug_yamimall_search_screenshot(item_code: str, _: bool = Depends(require_admin)):
    """진단용 임시 엔드포인트: 목록 방식 담기에서 상품(item_code)을 못 찾았을 때 남긴 스크린샷을 확인한다."""
    path = yamimall_bot.DATA_DIR / f"debug_yamimall_search_not_found_{item_code}.png"
    if not path.exists():
        raise HTTPException(status_code=404, detail="스크린샷이 아직 없습니다.")
    return FileResponse(str(path))


@app.get("/admin/debug-yamimall-search-html/{item_code}")
def admin_debug_yamimall_search_html(item_code: str, _: bool = Depends(require_admin)):
    """진단용 임시 엔드포인트: 목록 방식 담기에서 상품(item_code)을 못 찾았을 때 남긴 페이지 HTML을 확인한다."""
    path = yamimall_bot.DATA_DIR / f"debug_yamimall_search_not_found_{item_code}.html"
    if not path.exists():
        raise HTTPException(status_code=404, detail="HTML이 아직 없습니다.")
    return FileResponse(str(path), media_type="text/plain")


@app.get("/admin/debug-yamimall-search-summary/{item_code}")
def admin_debug_yamimall_search_summary(item_code: str, _: bool = Depends(require_admin)):
    """진단용 임시 엔드포인트: 목록 방식 담기 실패 요약(검색 결과 개수/샘플 상품명)을 바로 확인한다."""
    path = yamimall_bot.DATA_DIR / f"debug_yamimall_search_not_found_{item_code}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="요약이 아직 없습니다.")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/admin/debug-yamimall-qty/{item_code}")
def admin_debug_yamimall_qty(item_code: str, _: bool = Depends(require_admin)):
    """진단용 임시 엔드포인트: 목록 방식 담기에서 수량칸에 실제로 어떤 값이 들어갔는지 확인한다."""
    path = yamimall_bot.DATA_DIR / f"debug_yamimall_qty_{item_code}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="기록이 아직 없습니다.")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/admin/debug-yamimall-after-click-screenshot/{item_code}")
def admin_debug_yamimall_after_click_screenshot(item_code: str, _: bool = Depends(require_admin)):
    """진단용 임시 엔드포인트: 담기 버튼 클릭 직후 화면 스크린샷을 확인한다."""
    path = yamimall_bot.DATA_DIR / f"debug_yamimall_after_click_{item_code}.png"
    if not path.exists():
        raise HTTPException(status_code=404, detail="스크린샷이 아직 없습니다.")
    return FileResponse(str(path))


@app.get("/admin/debug-yamimall-after-click/{item_code}")
def admin_debug_yamimall_after_click(item_code: str, _: bool = Depends(require_admin)):
    """진단용 임시 엔드포인트: 담기 버튼 클릭 직후 상태(모달 존재 여부, 수량 등)를 확인한다."""
    path = yamimall_bot.DATA_DIR / f"debug_yamimall_after_click_{item_code}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="기록이 아직 없습니다.")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/admin/debug-godomall-qty/{vendor_id}/{goods_no}")
def admin_debug_godomall_qty(vendor_id: str, goods_no: str, _: bool = Depends(require_admin)):
    """진단용 임시 엔드포인트: 고도몰 계열 담기에서 수량칸 관련 상태를 확인한다."""
    path = godomall_bot.DATA_DIR / f"debug_godomall_qty_{vendor_id}_{goods_no}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="기록이 아직 없습니다.")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/admin/debug-godomall-qty-screenshot/{vendor_id}/{goods_no}")
def admin_debug_godomall_qty_screenshot(vendor_id: str, goods_no: str, _: bool = Depends(require_admin)):
    """진단용 임시 엔드포인트: 고도몰 계열 담기 시점의 화면 스크린샷을 확인한다."""
    path = godomall_bot.DATA_DIR / f"debug_godomall_qty_{vendor_id}_{goods_no}.png"
    if not path.exists():
        raise HTTPException(status_code=404, detail="스크린샷이 아직 없습니다.")
    return FileResponse(str(path))


@app.get("/admin/debug-godomall-nocart/{vendor_id}/{goods_no}")
def admin_debug_godomall_nocart(vendor_id: str, goods_no: str, _: bool = Depends(require_admin)):
    """진단용 임시 엔드포인트: 담기 버튼을 못 찾았을 때 실제로 어떤 상품 페이지가 떴는지 확인한다."""
    path = godomall_bot.DATA_DIR / f"debug_godomall_nocart_{vendor_id}_{goods_no}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="기록이 아직 없습니다.")
    return json.loads(path.read_text(encoding="utf-8"))


@app.get("/admin/debug-godomall-nocart-screenshot/{vendor_id}/{goods_no}")
def admin_debug_godomall_nocart_screenshot(vendor_id: str, goods_no: str, _: bool = Depends(require_admin)):
    """진단용 임시 엔드포인트: 담기 버튼을 못 찾았을 때 화면 스크린샷을 확인한다."""
    path = godomall_bot.DATA_DIR / f"debug_godomall_nocart_{vendor_id}_{goods_no}.png"
    if not path.exists():
        raise HTTPException(status_code=404, detail="스크린샷이 아직 없습니다.")
    return FileResponse(str(path))


@app.get("/admin/debug-godomall-isolated/{vendor_id}/{goods_no}")
def admin_debug_godomall_isolated(vendor_id: str, goods_no: str, _: bool = Depends(require_admin)):
    """진단용 임시 엔드포인트: 장바구니 담기 흐름과 완전히 분리된 별도 브라우저로
    로그인 + 상품페이지 접속만 단독 실행한다. 다른 요청(동시 로그인 등)과 절대
    겹치지 않는 상태에서도 상품페이지 대신 홈으로 리다이렉트되는지 확인해서,
    동시 세션 충돌 때문인지 아니면 이 흐름 자체의 문제인지 구분한다."""
    from playwright.sync_api import sync_playwright

    stores = telegram_store.list_stores()
    approved = [s for s in stores if s["approved"]]
    if not approved:
        return {"ok": False, "reason": "승인된 가맹점이 없습니다"}
    store_id = approved[0]["store_name"]

    creds = vendors.get_store_vendor_credentials(store_id, vendor_id)
    if not creds:
        return {"ok": False, "reason": f"{vendor_id} 계정 정보가 없습니다 (store_id={store_id})"}
    login_id, login_pwd = creds

    vendor_info = vendors.VENDORS.get(vendor_id, {})
    base_url = vendor_info.get("base_url", "")
    if not base_url:
        return {"ok": False, "reason": f"알 수 없는 vendor_id: {vendor_id}"}

    with browser_limit.browser_semaphore, sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-setuid-sandbox"])
        context = browser.new_context()
        page = context.new_page()
        try:
            godomall_bot.login_godomall(page, base_url, login_id, login_pwd)
            login_landed_url = page.url
            page.goto(f"{base_url}/goods/goods_view.php?goodsNo={goods_no}", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            return {
                "ok": True,
                "store_id": store_id,
                "login_landed_url": login_landed_url,
                "final_url": page.url,
                "cart_btn_count": page.locator("#cartBtn").count(),
                "body_sample": page.locator("body").inner_text()[:400],
            }
        except Exception as e:
            return {"ok": False, "reason": str(e)}
        finally:
            browser.close()


@app.get("/admin/debug-compare/{keyword}")
def admin_debug_compare(keyword: str, _: bool = Depends(require_admin)):
    """진단용 임시 엔드포인트: 특정 키워드로 price_compare.compare()가 실제로 몇 개의
    그룹을 만드는지(왜 모호한 상품이 자동으로 하나만 선택됐는지) 확인한다."""
    result = price_compare.compare(keyword)
    groups = result.get("groups", [])
    return {
        "keyword": keyword,
        "group_count": len(groups),
        "groups": [
            {
                "representative_name": g.get("representative_name"),
                "best_price": g.get("best_price"),
                "best_vendor_name": g.get("best_vendor_name"),
                "offers": [
                    {
                        "vendor_id": o.get("vendor_id"), "name": o.get("name"), "price": o.get("price"),
                        "match_score": o.get("match_score"), "goods_no": o.get("goods_no"), "product_url": o.get("product_url"),
                    }
                    for o in g.get("offers", [])
                ],
            }
            for g in groups
        ],
    }


class ProductConfirmRequest(BaseModel):
    item_key: str
    item_name: str
    image_url: str = ""
    price: Optional[int] = None
    reference_url: str
    category: Optional[str] = None


def register_product_routes(pt: product_ranking.ProductType, *, slug: str, page_template: str, admin_template: str):
    """음료 추천/과자 추천처럼 ProductType 하나에 필요한 페이지+API 전부를
    등록한다. slug는 URL 경로 조각(예: beverage, snack)으로 쓰인다. 두 상품군이
    거의 동일한 라우트를 갖기 때문에(compare 페이지 요청: "모든 기능 동일하게
    구성") 라우트 코드를 두 번 쓰지 않고 여기서 한 번만 정의해 pt별로 호출한다."""

    @app.get(f"/admin/debug-{slug}-status")
    def _debug_status(_: bool = Depends(require_admin)):
        """진단용(읽기 전용): 쿠팡 API를 전혀 호출하지 않고, 카탈로그의 해당 상품군
        항목 대비 현재 DB에 이미지/파트너스링크가 얼마나 채워졌는지만 확인한다."""
        try:
            catalog = mapping.load_catalog()
        except Exception as e:
            return {"ok": False, "error": f"카탈로그 로드 실패: {e}"}

        keys = {
            barcode for barcode, entry in catalog.items()
            if entry.category.strip() == pt.catalog_category
        }

        conn = product_ranking.get_conn()
        cur = conn.cursor()
        cur.execute(f"SELECT item_key, item_name, reference_url, partners_link, image_refreshed_at, link_refreshed_at FROM {pt.table_name}")
        rows = cur.fetchall()
        conn.close()

        by_key = {r[0]: r for r in rows}
        with_image = sum(1 for r in rows if r[2])
        with_link = sum(1 for r in rows if r[3])
        missing = [k for k in keys if k not in by_key]

        return {
            "ok": True,
            "catalog_count": len(keys),
            "backfilled_with_image_count": with_image,
            "backfilled_with_partners_link_count": with_link,
            "not_yet_attempted_count": len(missing),
            "last_image_refresh": max((r[4] for r in rows if r[4]), default=None),
            "last_link_refresh": max((r[5] for r in rows if r[5]), default=None),
            "sample_missing": missing[:10],
            "cp_access_key_set": bool(product_ranking.CP_ACCESS_KEY),
            "cp_secret_key_set": bool(product_ranking.CP_SECRET_KEY),
            "scheduler_running": scheduler.running,
            "scheduled_jobs": [
                {"id": j.id, "next_run_time": str(j.next_run_time)}
                for j in scheduler.get_jobs()
            ],
        }

    @app.get(f"/admin/{slug}s", response_class=HTMLResponse)
    def _admin_page(request: Request, _: bool = Depends(require_admin)):
        return templates.TemplateResponse(admin_template, {"request": request})

    @app.get(f"/admin/api/{slug}s")
    def _admin_list(_: bool = Depends(require_admin)):
        """카탈로그의 해당 상품군 + 관리 페이지에서 직접 추가한 상품(카탈로그엔
        없고 DB에만 있는 것) 전체를, 각각의 현재 DB 상태(이미지/가격/링크/수동고정
        여부)와 합쳐서 반환한다. 관리 페이지의 목록 렌더링용."""
        try:
            catalog = mapping.load_catalog()
        except Exception as e:
            return {"ok": False, "error": f"카탈로그 로드 실패: {e}"}

        catalog_names = {
            barcode: entry.menu_name for barcode, entry in catalog.items()
            if entry.category.strip() == pt.catalog_category
        }

        conn = product_ranking.get_conn()
        cur = conn.cursor()
        cur.execute(f"SELECT item_key, item_name, image_url, price, reference_url, manual_override, category, deleted FROM {pt.table_name}")
        db_rows = {
            r[0]: {
                "item_name": r[1], "image_url": r[2], "price": r[3], "reference_url": r[4],
                "manual_override": bool(r[5]), "category": r[6], "deleted": bool(r[7]),
            }
            for r in cur.fetchall()
        }
        conn.close()

        # 삭제 표시(deleted=1)된 항목은 카탈로그에 남아있어도 관리 페이지에
        # "미완료" 유령으로 다시 뜨지 않게 완전히 제외한다.
        deleted_keys = {k for k, v in db_rows.items() if v["deleted"]}

        # 카탈로그 원본(직접 추가한 이름은 없을 수 있음) + DB에만 있는 관리 페이지
        # 직접 추가분(카탈로그엔 없음)을 합집합으로 합친다.
        all_keys = (set(catalog_names) | set(db_rows)) - deleted_keys

        items = []
        for item_key in all_keys:
            state = db_rows.get(item_key, {})
            # 관리자가 직접 수정한 상품명(DB)이 있으면 카탈로그 원본 이름보다 우선한다 -
            # 카드에 노출되는 이름은 관리자가 마지막으로 저장한 값이어야 하기 때문.
            items.append({
                "item_key": item_key,
                "item_name": state.get("item_name") or catalog_names.get(item_key) or "",
                "image_url": state.get("image_url"),
                "price": state.get("price"),
                "reference_url": state.get("reference_url"),
                "manual_override": state.get("manual_override", False),
                "category": state.get("category") or pt.default_package_type,
            })
        items.sort(key=lambda it: it["item_name"])
        return {"ok": True, "items": items, "package_types": pt.package_types}

    @app.post(f"/admin/api/{slug}s/confirm")
    def _admin_confirm(req: ProductConfirmRequest, _: bool = Depends(require_admin)):
        """관리자가 직접 입력한 상품명/분류/이미지/링크를 확정 저장한다. 이후 자동
        검색 갱신에서 영구 제외된다(엉뚱한 상품으로 재매칭되는 걸 막기 위함)."""
        product_ranking.set_manual_link(
            pt, req.item_key, req.item_name, req.image_url, req.price, req.reference_url, req.category,
        )
        return {"ok": True}

    @app.delete(f"/admin/api/{slug}s/{{item_key}}")
    def _admin_delete(item_key: str, _: bool = Depends(require_admin)):
        """추천 목록(고객용 페이지)과 관리 페이지 양쪽에서 영구적으로 제거한다."""
        product_ranking.delete_product(pt, item_key)
        return {"ok": True}

    @app.get(f"/{slug}s", response_class=HTMLResponse)
    def _store_page(request: Request):
        if not get_current_web_user(request):
            return RedirectResponse(url="/login")
        return templates.TemplateResponse(page_template, {"request": request, "active_page": f"{slug}s"})

    @app.get(f"/api/{slug}-ranking")
    def _store_ranking(_: dict = Depends(require_web_user)):
        return {"items": product_ranking.get_rankings(pt)}

    @app.get(f"/api/{slug}-price-history/{{item_key}}")
    def _store_price_history(item_key: str, _: dict = Depends(require_web_user)):
        return {"points": product_ranking.get_price_history(pt, item_key)}

    @app.post(f"/api/{slug}-ranking/refresh-products")
    def _store_refresh(limit: int | None = Query(None, ge=1, le=200), _: bool = Depends(require_admin)):
        """카탈로그에 새로 추가된 상품의 이미지/가격을 쿠팡 상품검색으로 채운다.
        이 API는 시간당 호출 한도가 엄격해서 자주 누르면 안 되고, 매일 자동으로도
        한 번 돌아간다(카탈로그가 그대로면 처리할 게 없어 거의 즉시 끝남)."""
        return product_ranking.refresh_products(pt, limit=limit)

    @app.post(f"/api/{slug}-click/{{item_key}}")
    def _store_click(item_key: str, _: dict = Depends(require_web_user)):
        ok = product_ranking.record_click(pt, item_key)
        return {"ok": ok}


@app.get("/admin", response_class=HTMLResponse)
def admin_index_page(request: Request, _: bool = Depends(require_admin)):
    return templates.TemplateResponse("admin_index.html", {"request": request})


@app.get("/admin/patch-notes", response_class=HTMLResponse)
def admin_patch_notes_page(request: Request, _: bool = Depends(require_admin)):
    return templates.TemplateResponse("patch_notes_admin.html", {"request": request})


class PatchNoteCreateRequest(BaseModel):
    version: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)


@app.post("/admin/api/patch-notes")
def admin_api_patch_notes_create(req: PatchNoteCreateRequest, _: bool = Depends(require_admin)):
    note_id = patch_notes.add_patch_note(req.version, req.summary)
    return {"ok": True, "id": note_id}


@app.delete("/admin/api/patch-notes/{note_id}")
def admin_api_patch_notes_delete(note_id: int, _: bool = Depends(require_admin)):
    patch_notes.delete_patch_note(note_id)
    return {"ok": True}


register_product_routes(product_ranking.BEVERAGE, slug="beverage", page_template="beverages.html", admin_template="beverage_admin.html")
register_product_routes(product_ranking.SNACK, slug="snack", page_template="snacks.html", admin_template="snack_admin.html")


@app.get("/admin/biz-tools", response_class=HTMLResponse)
def admin_biz_tools_page(request: Request, _: bool = Depends(require_admin)):
    return templates.TemplateResponse("tool_admin.html", {"request": request})


@app.get("/admin/api/biz-tools")
def admin_api_biz_tools_list(_: bool = Depends(require_admin)):
    return {"ok": True, "items": biz_tools.list_tools()}


class BizToolRequest(BaseModel):
    item_name: str = Field(..., min_length=1)
    image_url: str = ""
    product_url: str = Field(..., min_length=1)


@app.post("/admin/api/biz-tools")
def admin_api_biz_tools_create(req: BizToolRequest, _: bool = Depends(require_admin)):
    new_id = biz_tools.add_tool(req.item_name, req.image_url, req.product_url)
    return {"ok": True, "id": new_id}


@app.put("/admin/api/biz-tools/{tool_id}")
def admin_api_biz_tools_update(tool_id: int, req: BizToolRequest, _: bool = Depends(require_admin)):
    ok = biz_tools.update_tool(tool_id, req.item_name, req.image_url, req.product_url)
    return {"ok": ok}


@app.delete("/admin/api/biz-tools/{tool_id}")
def admin_api_biz_tools_delete(tool_id: int, _: bool = Depends(require_admin)):
    ok = biz_tools.delete_tool(tool_id)
    return {"ok": ok}


@app.get("/admin/consumables", response_class=HTMLResponse)
def admin_consumables_page(request: Request, _: bool = Depends(require_admin)):
    return templates.TemplateResponse("consumable_admin.html", {"request": request})


@app.get("/admin/api/consumables")
def admin_api_consumables_list(_: bool = Depends(require_admin)):
    return {"ok": True, "items": consumables.list_items()}


class ConsumableRequest(BaseModel):
    item_name: str = Field(..., min_length=1)
    image_url: str = ""
    product_url: str = Field(..., min_length=1)


@app.post("/admin/api/consumables")
def admin_api_consumables_create(req: ConsumableRequest, _: bool = Depends(require_admin)):
    new_id = consumables.add_item(req.item_name, req.image_url, req.product_url)
    return {"ok": True, "id": new_id}


@app.put("/admin/api/consumables/{item_id}")
def admin_api_consumables_update(item_id: int, req: ConsumableRequest, _: bool = Depends(require_admin)):
    ok = consumables.update_item(item_id, req.item_name, req.image_url, req.product_url)
    return {"ok": ok}


@app.delete("/admin/api/consumables/{item_id}")
def admin_api_consumables_delete(item_id: int, _: bool = Depends(require_admin)):
    ok = consumables.delete_item(item_id)
    return {"ok": ok}


@app.get("/admin/barcode-catalog", response_class=HTMLResponse)
def admin_barcode_catalog_page(request: Request, _: bool = Depends(require_admin)):
    return templates.TemplateResponse("barcode_admin.html", {"request": request})


@app.get("/admin/api/barcode-catalog")
def admin_api_barcode_catalog_list(_: bool = Depends(require_admin)):
    """전체 카탈로그(현재 1000개 이상)를 다 내려주면 목록이 무거워지므로,
    기본으로는 최근 수정된 20개만 준다 - 특정 상품을 찾으려면 검색을 쓴다."""
    return {"ok": True, "items": mapping.list_catalog_items(limit=20), "total_count": mapping.catalog_item_count()}


@app.get("/admin/api/barcode-catalog/search")
def admin_api_barcode_catalog_search(q: str = Query(..., min_length=1), _: bool = Depends(require_admin)):
    """바코드 또는 제품명으로 찾아 수정 폼에 불러올 때 쓴다(구분/1타 개수 등
    현재 값을 미리 채워 넣기 위함) - 여러 개가 검색되면 전부 돌려줘서 나눠서
    고칠 수 있게 한다."""
    items = mapping.search_catalog_full(q, limit=20)
    return {"ok": True, "items": items}


class BarcodeCatalogRequest(BaseModel):
    barcode: str = Field(..., min_length=1)
    menu_name: str = Field(..., min_length=1)
    is_coupang: int = Field(99, ge=0, le=99, description="0=아이스크림,1=쿠팡,2=도매몰,3=문구완구,99=미분류")
    pack_qty: int = Field(1, ge=1, description="도매몰 1타 개수")
    icecream_box_qty: int = Field(0, ge=0, description="아이스크림 박스당 개수")
    search_keyword: str = ""
    recommended_price: int | None = None


@app.post("/admin/api/barcode-catalog")
def admin_api_barcode_catalog_create(req: BarcodeCatalogRequest, _: bool = Depends(require_admin)):
    existing = mapping.load_catalog().get(req.barcode)
    item = mapping.CoupangCatalogItem(
        barcode=req.barcode,
        menu_name=req.menu_name,
        search_keyword=req.search_keyword,
        fixed_url=existing.fixed_url if existing else "",
        pack_qty=req.pack_qty,
        min_order=existing.min_order if existing else 1,
        notes=existing.notes if existing else "",
        is_coupang=req.is_coupang,
        icecream_box_qty=req.icecream_box_qty,
        category=existing.category if existing else "",
        menu_code=existing.menu_code if existing else "",
        recommended_price=req.recommended_price or 0,
    )
    mapping.upsert_catalog_item(item)
    return {"ok": True}


@app.delete("/admin/api/barcode-catalog/{barcode}")
def admin_api_barcode_catalog_delete(barcode: str, _: bool = Depends(require_admin)):
    ok = mapping.delete_catalog_item(barcode)
    return {"ok": ok}


@app.get("/admin/api/unclassified-queue")
def admin_api_unclassified_queue_list(_: bool = Depends(require_admin)):
    return {"ok": True, "items": mapping.list_unclassified_queue()}


@app.delete("/admin/api/unclassified-queue/{barcode}")
def admin_api_unclassified_queue_dismiss(barcode: str, _: bool = Depends(require_admin)):
    ok = mapping.dismiss_unclassified_item(barcode)
    return {"ok": ok}


@app.get("/admin/api/catalog/download")
def admin_api_catalog_download(_: bool = Depends(require_admin)):
    """현재 DB 카탈로그 전체를 엑셀로 받는다 - 로컬에서 수정한 뒤 업로드로
    다시 반영하는 왕복 작업용."""
    content = mapping.export_catalog_to_xlsx_bytes()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="catalog_{ts}.xlsx"'},
    )


@app.post("/admin/api/catalog/upload")
async def admin_api_catalog_upload(file: UploadFile = File(...), _: bool = Depends(require_admin)):
    """업로드된 엑셀로 카탈로그 전체를 통째로 교체한다. 형식이 잘못됐거나
    유효한 행이 너무 적으면(사고성 업로드 의심) 반영하지 않고 거부한다."""
    content = await file.read()
    try:
        items = mapping.parse_catalog_xlsx_bytes(content)
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    except Exception as e:
        return {"ok": False, "message": f"엑셀 파일을 읽지 못했습니다: {e}"}

    count = mapping.replace_catalog_from_items(items)
    return {"ok": True, "message": f"{count}개 상품으로 카탈로그를 교체했습니다.", "count": count}


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


@app.get("/api/my-vendors/{vendor_id}/accounts")
def api_my_vendors_accounts_list(vendor_id: str, user: dict = Depends(require_web_user)):
    """다매장 운영 시 한 도매처에 계정을 여러 개(별명으로 구분) 등록할 수 있다 -
    텔레그램의 "계정추가"와 동일한 저장소를 쓴다."""
    store_id = f"web:{user['email']}"
    return {"accounts": vendors.list_store_vendor_accounts(store_id, vendor_id)}


class MyVendorAccountAddRequest(BaseModel):
    nickname: str = ""
    login_id: str
    login_pwd: str


@app.post("/api/my-vendors/{vendor_id}/accounts")
def api_my_vendors_accounts_add(vendor_id: str, req: MyVendorAccountAddRequest, user: dict = Depends(require_web_user)):
    store_id = f"web:{user['email']}"
    try:
        account_id = vendors.add_store_vendor_account(store_id, vendor_id, req.nickname, req.login_id, req.login_pwd)
    except ValueError as e:
        return {"ok": False, "message": str(e)}
    return {"ok": True, "id": account_id}


@app.delete("/api/my-vendors/{vendor_id}/accounts/{account_id}")
def api_my_vendors_accounts_delete(vendor_id: str, account_id: int, user: dict = Depends(require_web_user)):
    store_id = f"web:{user['email']}"
    ok = vendors.delete_store_vendor_account(store_id, vendor_id, account_id)
    return {"ok": ok}


@app.post("/api/my-vendors/{vendor_id}/accounts/{account_id}/default")
def api_my_vendors_accounts_set_default(vendor_id: str, account_id: int, user: dict = Depends(require_web_user)):
    store_id = f"web:{user['email']}"
    ok = vendors.set_default_store_vendor_account(store_id, vendor_id, account_id)
    return {"ok": ok}


class MyVendorToggleRequest(BaseModel):
    enabled: bool


@app.post("/api/my-vendors/{vendor_id}/toggle")
def api_my_vendors_toggle(vendor_id: str, req: MyVendorToggleRequest, user: dict = Depends(require_web_user)):
    """가격비교에서 이 도매처를 켜고 끈다(텔레그램의 "도매처 활성화/비활성화"와
    동일한 개념, 웹 계정용 저장소는 별도)."""
    store_id = f"web:{user['email']}"
    vendors.set_vendor_enabled_for_store(store_id, vendor_id, req.enabled)
    return {"ok": True}


class MyVendorPreferredRequest(BaseModel):
    vendor_id: Optional[str] = None


@app.post("/api/my-vendors/preferred")
def api_my_vendors_set_preferred(req: MyVendorPreferredRequest, user: dict = Depends(require_web_user)):
    """가격이 동률일 때 우선으로 볼 주 도매처를 지정한다. vendor_id를 비우면 해제."""
    store_id = f"web:{user['email']}"
    vendors.set_preferred_vendor_for_store(store_id, req.vendor_id)
    return {"ok": True}


@app.get("/api/store-reports/schedules")
def api_store_report_schedules_list(user: dict = Depends(require_web_user)):
    store_id = f"web:{user['email']}"
    accounts = vendors.list_store_vendor_accounts(store_id, "orderqueen")
    nickname_by_id = {a["id"]: a["nickname"] for a in accounts}
    schedules = store_reports.list_schedules(store_id)
    for s in schedules:
        s["account_nickname"] = nickname_by_id.get(s["account_id"])
    return {
        "linked_store_name": user.get("linked_store_name"),
        "orderqueen_accounts": accounts,
        "schedules": schedules,
    }


class StoreReportScheduleRequest(BaseModel):
    day_of_week: int = Field(..., ge=0, le=6, description="0=월 ... 6=일")
    time: str = Field(..., pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    account_id: Optional[int] = None


@app.post("/api/store-reports/schedules")
def api_store_report_schedules_add(req: StoreReportScheduleRequest, user: dict = Depends(require_web_user)):
    store_id = f"web:{user['email']}"
    new_id = store_reports.add_schedule(store_id, req.day_of_week, req.time, req.account_id)
    return {"ok": True, "id": new_id}


@app.delete("/api/store-reports/schedules/{schedule_id}")
def api_store_report_schedules_delete(schedule_id: int, user: dict = Depends(require_web_user)):
    store_id = f"web:{user['email']}"
    ok = store_reports.delete_schedule(store_id, schedule_id)
    return {"ok": ok}


class StoreReportScheduleToggleRequest(BaseModel):
    enabled: bool


@app.post("/api/store-reports/schedules/{schedule_id}/toggle")
def api_store_report_schedules_toggle(
    schedule_id: int, req: StoreReportScheduleToggleRequest, user: dict = Depends(require_web_user)
):
    store_id = f"web:{user['email']}"
    ok = store_reports.set_schedule_enabled(store_id, schedule_id, req.enabled)
    return {"ok": ok}


class SendManualReportRequest(BaseModel):
    period_from: date
    period_to: date
    top_items: list[dict]
    account_id: Optional[int] = None


@app.post("/api/store-reports/send-manual")
def api_store_reports_send_manual(req: SendManualReportRequest, user: dict = Depends(require_web_user)):
    """/order에서 수동으로 불러온 발주 추천을 텔레그램으로 보내 확인/스킵/수정
    으로 바로 도매처 담기까지 이어갈 수 있게 한다 - 예약 주기를 기다리지 않고
    지금 바로 자동 리포트와 같은 방식으로 처리하는 것."""
    store_id = f"web:{user['email']}"
    chat_id = _resolve_chat_id_for_store(store_id)
    if not chat_id:
        return {"ok": False, "message": "이 계정에 연결된 텔레그램 지점이 없습니다. 관리자에게 지점 연결을 요청해주세요."}

    classified = store_reports.build_manual_wholesale_report(store_id, req.account_id, req.top_items)
    report = {
        "period_from": req.period_from.isoformat(),
        "period_to": req.period_to.isoformat(),
        **classified,
    }

    sent_any = False
    for builder in (_format_wholesale_report_message, _format_coupang_report_message, _format_icecream_report_message):
        msg = builder(report)
        if msg:
            telegram_bot.send_message(chat_id, msg)
            sent_any = True

    if not sent_any:
        telegram_bot.send_message(
            chat_id,
            f"📦 수동 발주 리포트 ({report['period_from']} ~ {report['period_to']} 집계)\n\n"
            "이번 조회에서는 발주 대상 상품이 없습니다.",
        )

    if report["wholesale_items"]:
        telegram_store.set_disambig_state(chat_id, {
            "mode": "report_confirm",
            "current": True,
            "store_id": store_id,
            "report_key": report["report_key"],
            "wholesale_items": report["wholesale_items"],
        })

    return {
        "ok": True,
        "message": "텔레그램으로 발송했습니다.",
        "wholesale_count": len(report["wholesale_items"]),
    }


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


@app.get("/barcode-search", response_class=HTMLResponse)
def barcode_search_page(request: Request):
    if not get_current_web_user(request):
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("barcode_search.html", {"request": request, "active_page": "barcode_search"})


@app.get("/api/popular")
def api_popular(category: str = Query(...), limit: int = Query(30, ge=1, le=100)):
    if category not in popularity.CATEGORIES:
        return {"items": []}
    return {"items": popularity.get_top_items(category, limit=limit)}


@app.get("/patch-notes", response_class=HTMLResponse)
def patch_notes_page(request: Request):
    if not get_current_web_user(request):
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("patch_notes.html", {"request": request, "active_page": "patch_notes"})


@app.get("/api/patch-notes")
def api_patch_notes(_: dict = Depends(require_web_user)):
    return {"items": patch_notes.list_patch_notes()}


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
    telegram_bot.send_message(
        chat_id,
        f"{req.store_name}님, 승인이 완료되었습니다! 환영합니다.\n\n" + telegram_bot.HELP_TEXT,
    )
    return {"ok": True}


@app.post("/api/telegram/stores/{chat_id}/revoke")
def api_telegram_revoke(chat_id: str, _: bool = Depends(require_admin)):
    telegram_store.revoke_store(chat_id)
    return {"ok": True}


class TelegramRejectRequest(BaseModel):
    reason: str = Field(..., min_length=1)


@app.post("/api/telegram/stores/{chat_id}/reject")
def api_telegram_reject(chat_id: str, req: TelegramRejectRequest, _: bool = Depends(require_admin)):
    telegram_store.reject_store(chat_id, req.reason)
    telegram_bot.send_message(
        chat_id,
        "가맹점 등록이 반려됐습니다.\n"
        f"사유: {req.reason}\n"
        "문의사항이 있으면 대표님께 직접 연락해주세요.",
    )
    return {"ok": True}


@app.delete("/api/telegram/stores/{chat_id}")
def api_telegram_delete(chat_id: str, _: bool = Depends(require_admin)):
    """처리됨(승인/반려) 목록 정리용. 승인된 가맹점을 지우면 다음에 봇에게
    메시지를 보낼 때 신규 등록 절차부터 다시 시작하게 되니 주의가 필요하다
    (프론트에서 확인 문구로 안내)."""
    telegram_store.delete_store(chat_id)
    return {"ok": True}


@app.get("/admin/broadcast", response_class=HTMLResponse)
def admin_broadcast_page(request: Request, _: bool = Depends(require_admin)):
    return templates.TemplateResponse("broadcast_admin.html", {"request": request})


class BroadcastRequest(BaseModel):
    message: str = Field(..., min_length=1)


@app.post("/admin/api/broadcast")
def admin_api_broadcast(req: BroadcastRequest, _: bool = Depends(require_admin)):
    """승인된 모든 가맹점(텔레그램)에 공지 메시지를 보낸다. 매장 수가 많지
    않아(50개 안팎) 순차 전송으로도 충분하고, 결과(성공/실패 수)를 그 자리에서
    바로 보여줄 수 있어 백그라운드 작업으로 미루지 않는다."""
    stores = [s for s in telegram_store.list_stores() if s["approved"]]
    sent = sum(1 for s in stores if telegram_bot.send_message(s["chat_id"], req.message))
    failed = len(stores) - sent
    telegram_store.add_broadcast_history(req.message, sent, failed, len(stores))
    return {"ok": True, "sent": sent, "failed": failed, "total": len(stores)}


@app.get("/admin/api/broadcast/history")
def admin_api_broadcast_history(_: bool = Depends(require_admin)):
    return {"items": telegram_store.list_broadcast_history()}


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
    catalog = mapping.load_catalog()
    df = pd.DataFrame([
        {
            "version": "", "updated_at": "", "priority": "",
            "barcode": c.barcode, "menu_code": c.menu_code, "menu_name": c.menu_name,
            "is_coupang": c.is_coupang, "recommended_price": c.recommended_price,
            "search_keyword": c.search_keyword, "fixed_url": c.fixed_url,
            "pack_qty": c.pack_qty, "min_order": c.min_order,
            "notes": c.notes, "category": c.category,
        }
        for c in catalog.values()
    ])

    # 컬럼 없을 때 대비(카탈로그가 텅 비어 DataFrame([])이면 컬럼 자체가 없음)
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
@app.get("/index.html", response_class=HTMLResponse)
def landing_page(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "tools": biz_tools.list_tools()})


@app.get("/brand.html", response_class=HTMLResponse)
def brand_page(request: Request):
    return templates.TemplateResponse("brand.html", {"request": request})


@app.get("/logo.html", response_class=HTMLResponse)
def logo_page(request: Request):
    return templates.TemplateResponse("logo.html", {"request": request})


@app.get("/order", response_class=HTMLResponse)
def order_page(request: Request):
    if not get_current_web_user(request):
        return RedirectResponse(url="/login")
    return templates.TemplateResponse("order.html", {"request": request, "active_page": "order"})

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


@app.get("/api/barcode-search")
def api_barcode_search(q: str = Query(..., min_length=1), user: dict = Depends(require_web_user)):
    """텔레그램 "바코드" 명령과 동일한 검색(product_ranking.search_catalog,
    mapping.load_catalog() DB 카탈로그 기반) - 관리자 웹/텔레그램 바코드추가로
    저장한 값도 바로 반영된다."""
    return {"ok": True, "items": product_ranking.search_catalog(q, limit=10)}


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


@app.get("/api/order/last-report")
def api_order_last_report(account_id: Optional[int] = Query(None), user: dict = Depends(require_web_user)):
    """order 페이지를 벗어나도(새로고침 포함) 마지막으로 수동 생성한 발주표를
    이어볼 수 있도록 저장된 결과를 돌려준다 - 지점(계정)당 최신 1건만 보관."""
    store_id = f"web:{user['email']}"
    saved = store_reports.get_manual_report(store_id, account_id)
    if not saved:
        return {"ok": False}
    return {"ok": True, **saved}


@app.post("/import/orderqueen", response_model=OrderQueenImportResponse)
def import_from_orderqueen(req: OrderQueenImportRequest, user: dict = Depends(require_web_user)):
    store_id = f"web:{user['email']}"
    creds = vendors.get_store_vendor_credentials(store_id, "orderqueen", req.account_id)
    if not creds:
        raise HTTPException(
            status_code=400,
            detail="오더퀸 계정이 등록되어 있지 않습니다. '내 도매처 계정'에서 먼저 등록해주세요.",
        )
    login_id, login_pw = creds
    account = vendors.resolve_store_vendor_account(store_id, "orderqueen", req.account_id)
    account_nickname = account["nickname"] if account else None

    job_id = uuid.uuid4().hex[:8]
    sales_xlsx_path = str(DOWNLOAD_DIR / f"아이즈크림 오산세교_{job_id}.xlsx")

    # 1) 주문퀸 다운로드
    download_orderqueen_xlsx(
        login_id=login_id,
        login_pw=login_pw,
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
        login_id=login_id,
        login_pw=login_pw,
        period_from=period_from_2w,
        period_to=period_to,
        save_path=sales_xlsx_path_2w,
    )

    download_orderqueen_xlsx(
        login_id=login_id,
        login_pw=login_pw,
        period_from=period_from_3w,
        period_to=period_to,
        save_path=sales_xlsx_path_3w,
    )

    download_orderqueen_xlsx(
        login_id=login_id,
        login_pw=login_pw,
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
    catalog = mapping.load_catalog()
    disabled_vendors, preferred_vendor = vendors.get_store_vendor_prefs(store_id)

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
            if barcode:
                mapping.queue_unclassified_item(barcode, str(item.get("메뉴명", "") or ""), store_id)

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

    # 도매몰 발주 계산: 크롤러가 실제로 읽어온 1타 개수(unit_qty)를 알 때만
    # 60% 규칙(자동 리포트와 동일한 apply_case_rule)을 적용한다 - 카탈로그의
    # pack_qty는 관리자가 안 채우면 기본값 1이라 "모른다"를 구분할 수 없어서
    # 안 쓴다(1타=1개로 잘못 가정하면 판매수량=타수 사고가 재현됨).
        if item["is_coupang"] == 2 and cat:
            sold_qty = int(item.get("판매수량", 0) or 0)
            keyword = cat.search_keyword or cat.menu_name or item.get("메뉴명", "")
            offer = None
            if keyword:
                compare_result = price_compare.compare(keyword)
                groups = price_compare.filter_groups_for_store(compare_result.get("groups", []), disabled_vendors)
                for group in groups:
                    offer = store_reports._pick_best_offer(group.get("offers", []), preferred_vendor)
                    if offer:
                        break

            unit_qty = int(offer.get("unit_qty") or 0) if offer else 0
            if unit_qty <= 0:
                item["추천박스수"] = "1타 수량 미확인"
                item["포장반영"] = "#N/A"
                item["추천발주량_포장반영"] = "#N/A"
                item["주문표시"] = "1타 수량 미확인"
                continue

            ratio = sold_qty / unit_qty
            cases = store_reports.apply_case_rule(sold_qty, unit_qty)
            box_display = f"{cases}타 ({ratio:.2f})" if cases > 0 else f"60% 미달 ({ratio:.2f})"

            item["박스입수"] = unit_qty
            item["추천박스수"] = box_display
            item["포장반영"] = cases
            item["추천발주량_포장반영"] = cases * unit_qty
            item["주문표시"] = box_display
            item["wholesale_vendor_name"] = offer.get("vendor_name", offer.get("vendor_id", ""))


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
    summary["catalog_path"] = "DB (catalog_items)"

    summary["아이스크림품목수"] = int(sum(1 for x in top_items if x.get("is_coupang") == 0))
    summary["쿠팡품목수"] = int(sum(1 for x in top_items if x.get("is_coupang") == 1))
    summary["도매몰품목수"] = int(sum(1 for x in top_items if x.get("is_coupang") == 2))
    summary["문구완구품목수"] = int(sum(1 for x in top_items if x.get("is_coupang") == 3))

    # 인기상품 집계용 이력 기록 (전 가맹점 합산 TOP30 계산에 사용) - store_id는
    # 오더퀸 로그인ID가 아니라 실제 지점 식별자를 써야 지점별 집계(store_count)가
    # 정확해진다.
    manual_wholesale_items = []
    for item in top_items:
        qty = int(item.get("판매수량", 0) or 0)
        if qty <= 0:
            continue
        barcode = str(item.get("바코드번호", "") or "").strip().replace(".0", "")
        name = str(item.get("메뉴명", "") or "")
        item_key = barcode or name
        if item.get("is_coupang") == 0:
            popularity.log_event(store_id, "icecream", item_key, name, qty)
        elif item.get("is_coupang") == 1:
            popularity.log_event(store_id, "coupang", item_key, name, qty)
        elif item.get("is_coupang") == 2:
            popularity.log_event(store_id, "wholesale", item_key, name, qty)
            manual_wholesale_items.append({"item_key": item_key, "qty": qty})

    # 예약 주기가 도래하지 않아도 수동으로 불러온 도매몰 판매량이 자동 리포트의
    # 이월(carryover)에서 누락되지 않도록 반영한다 (갭이 있으면 store_reports가
    # 알아서 커서를 건드리지 않고 건너뜀).
    store_reports.apply_manual_pull_carryover(store_id, req.account_id, req.period_from, req.period_to, manual_wholesale_items)

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

    response = {
        "job_id": job_id,
        "xlsx_path": sales_xlsx_path,
        "summary": summary,
        "top_items": top_items,
        "export_path": export_path,
        "representative_product": representative_product,
        "representative_partner_link": representative_partner_link,
        "account_id": req.account_id,
        "account_nickname": account_nickname,
    }

    # order 페이지를 벗어나도 마지막 수동 생성 결과를 이어볼 수 있도록 저장.
    store_reports.save_manual_report(
        store_id, req.account_id, req.period_from.isoformat(), req.period_to.isoformat(), safety, response,
    )

    return response