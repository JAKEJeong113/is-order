# orderqueen_bot.py

import os
import time
from datetime import date
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

import browser_limit

LOGIN_URL = "https://www.orderqueen.kr/backoffice_admin/login.itp"

# 오더퀸 자체 사이트가 가끔 페이지 로딩이 느릴 때가 있어(실측: SAL03020.itp
# 매출 리포트 페이지에서 기본 30초 타임아웃 초과 확인), 조금 여유를 둔다.
PAGE_GOTO_TIMEOUT_MS = 45000

# 환경변수에서 읽기
REPORT_URL = "https://www.orderqueen.kr/backoffice_admin/SAL03020.itp"
DATE_FROM_SELECTOR = os.getenv("OQ_DATE_FROM_SELECTOR", "#schSDate")
DATE_TO_SELECTOR = os.getenv("OQ_DATE_TO_SELECTOR", "#schEDate")
SEARCH_BUTTON_SELECTOR = os.getenv("OQ_SEARCH_SELECTOR", "#btn-search button")
DOWNLOAD_BUTTON_SELECTOR = os.getenv("OQ_DOWNLOAD_SELECTOR", "#btn-excel button")


def download_orderqueen_xlsx(
    login_id: str,
    login_pw: str,
    period_from: date,
    period_to: date,
    save_path: str,
) -> None:
    if not REPORT_URL:
        raise RuntimeError("OQ_REPORT_URL is not set.")

    os.makedirs(os.path.dirname(save_path), exist_ok=True)

    with browser_limit.browser_semaphore, sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage"
            ],
        )
        try:
            _download_orderqueen_xlsx_inner(
                browser, login_id, login_pw, period_from, period_to, save_path,
            )
        finally:
            # 로그인/페이지 이동 중 타임아웃 등으로 중간에 실패해도 브라우저
            # 프로세스가 안 닫힌 채 남는 걸 막는다(재시도 시 좀비 프로세스가
            # 쌓이는 문제 방지).
            browser.close()


def _download_orderqueen_xlsx_inner(
    browser, login_id: str, login_pw: str, period_from: date, period_to: date, save_path: str,
) -> None:
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()

    # 1️⃣ 로그인 페이지 이동
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=PAGE_GOTO_TIMEOUT_MS)

    # ✅ 아이디/비번 입력 (일단 가장 흔한 케이스: text 1개 + password 1개)
    id_box = page.locator('input[type="text"]').first
    pw_box = page.locator('input[type="password"]').first

    id_box.fill(login_id)
    pw_box.fill(login_pw)

    # ✅ 로그인 제출: 버튼 클릭 대신 Enter로 submit (버튼 셀렉터 문제 회피)
    pw_box.press("Enter")
    page.wait_for_load_state("networkidle", timeout=20000)

    # ✅ 아직 로그인 페이지면(실패/추가 버튼 필요) fallback 후보 클릭
    if "login.itp" in page.url:
        candidates = [
            'button:has-text("로그인")',
            'button:has-text("Login")',
            'button:has-text("확인")',
            'input[type="submit"]',
            'button[type="submit"]',
            'a:has-text("로그인")',
            'a:has-text("Login")',
        ]
        for sel in candidates:
            loc = page.locator(sel)
            if loc.count() > 0:
                loc.first.click()
                page.wait_for_load_state("networkidle", timeout=20000)
                break

    # ✅ 그래도 로그인 페이지면: 디버그 스크린샷 저장하고 중단
    if "login.itp" in page.url:
        debug_login = save_path.replace(".xlsx", "_login_debug.png")
        page.screenshot(path=debug_login, full_page=True)
        raise RuntimeError(f"Login seems to have failed. Screenshot saved: {debug_login}")

    # 2️⃣ 매출 리포트 페이지 이동
    page.goto(REPORT_URL, wait_until="domcontentloaded", timeout=PAGE_GOTO_TIMEOUT_MS)
    page.wait_for_load_state("networkidle", timeout=20000)

    # 3️⃣ 기간 입력 (readonly라 JS로 강제 세팅)
    page.evaluate(
        """([selector, value]) => {
            const el = document.querySelector(selector);
            if (el) {
                el.removeAttribute('readonly');
                el.value = value;
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }
        }""",
        [DATE_FROM_SELECTOR, period_from.isoformat()],
    )

    page.evaluate(
        """([selector, value]) => {
            const el = document.querySelector(selector);
            if (el) {
                el.removeAttribute('readonly');
                el.value = value;
                el.dispatchEvent(new Event('change', { bubbles: true }));
            }
        }""",
        [DATE_TO_SELECTOR, period_to.isoformat()],
    )

    # 조회 클릭
    page.click(SEARCH_BUTTON_SELECTOR)
    page.wait_for_load_state("networkidle", timeout=20000)

    # 4️⃣ 엑셀 다운로드
    try:
        with page.expect_download(timeout=30000) as download_info:
            page.click(DOWNLOAD_BUTTON_SELECTOR)

        download = download_info.value
        download.save_as(save_path)

    except PWTimeoutError:
        # 디버깅용 스크린샷 저장
        debug_path = save_path.replace(".xlsx", "_debug.png")
        page.screenshot(path=debug_path, full_page=True)
        raise RuntimeError(f"Excel download failed. Screenshot saved: {debug_path}")

    context.close()


# 예약 리포트는 하루 한 번(15분 창) 안에서만 기회가 있어서, 오더퀸 자체
# 사이트의 일시적인 느림/타임아웃 한 번으로 그날 리포트를 통째로 못 받는
# 사고가 실제로 있었다(SAL03020.itp 30초 타임아웃). 완전히 새 브라우저
# 세션으로 한 번 더 시도해서 이런 일시적 문제를 흡수한다.
DOWNLOAD_MAX_ATTEMPTS = 2
DOWNLOAD_RETRY_DELAY_SECONDS = 5


def download_orderqueen_xlsx_with_retry(
    login_id: str, login_pw: str, period_from: date, period_to: date, save_path: str,
) -> None:
    last_error: Exception | None = None
    for attempt in range(1, DOWNLOAD_MAX_ATTEMPTS + 1):
        try:
            download_orderqueen_xlsx(login_id, login_pw, period_from, period_to, save_path)
            return
        except Exception as e:
            last_error = e
            if attempt < DOWNLOAD_MAX_ATTEMPTS:
                time.sleep(DOWNLOAD_RETRY_DELAY_SECONDS)
    raise last_error