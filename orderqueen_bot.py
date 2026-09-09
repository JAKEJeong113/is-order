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

# wait_for_load_state("networkidle", ...)에 쓰는 타임아웃. 원래 20초로 짧게
# 박혀 있었는데, 2026-08-03 실제 운영 로그에서 로그인 직후는 통과하고 매출
# 리포트 페이지(SAL03020.itp) 진입 직후의 networkidle 대기에서만 20초를 넘겨
# 실패한 사례가 확인됐다(재시도 2회 다 같은 지점에서 실패). 이 페이지가 백그라운드
# 네트워크 요청(광고/분석 등으로 추정)을 계속 띄우는 걸로 보이는데, 조회
# 결과에 필요한 요소(날짜 입력칸/조회·다운로드 버튼)가 페이지 로드 시점부터
# 이미 DOM에 존재해서(실측 확인) 특정 요소를 기다리는 방식으로는 "조회 결과가
# 실제로 갱신됐는지"를 구분할 수 없다 - 그래서 wait_for_selector로 바꾸는 대신
# PAGE_GOTO_TIMEOUT_MS와 동일하게 여유를 늘리는 쪽을 택했다.
NETWORKIDLE_TIMEOUT_MS = PAGE_GOTO_TIMEOUT_MS

# 환경변수에서 읽기
REPORT_URL = "https://www.orderqueen.kr/backoffice_admin/SAL03020.itp"
DATE_FROM_SELECTOR = os.getenv("OQ_DATE_FROM_SELECTOR", "#schSDate")
DATE_TO_SELECTOR = os.getenv("OQ_DATE_TO_SELECTOR", "#schEDate")
SEARCH_BUTTON_SELECTOR = os.getenv("OQ_SEARCH_SELECTOR", "#btn-search button")
DOWNLOAD_BUTTON_SELECTOR = os.getenv("OQ_DOWNLOAD_SELECTOR", "#btn-excel button")

MENU_LIST_URL = "https://www.orderqueen.kr/backoffice_admin/MNU01020.itp"

# 상품 등록 화면(MNU01021.itp)의 "분류" 드롭다운 - 실측으로 확인한 매장의
# 실제 분류 코드값(매장마다 다를 수 있음 - 이 매장 기준). 앱에서 사용자가
# 직접 고르는 드롭다운을 채우는 데 쓴다.
CLASS_CODES = {
    "아이스크림": "003",
    "음료수": "004",
    "간식": "006",
    "완구,문구": "007",
    "과자": "015",
}


def _login(page, login_id: str, login_pw: str) -> None:
    """로그인 페이지에서 아이디/비번을 입력해 로그인한다. 실패하면
    RuntimeError를 던진다(디버그 스크린샷 없이 - 호출부에서 필요하면 남긴다)."""
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=PAGE_GOTO_TIMEOUT_MS)

    id_box = page.locator('input[type="text"]').first
    pw_box = page.locator('input[type="password"]').first
    id_box.fill(login_id)
    pw_box.fill(login_pw)
    pw_box.press("Enter")
    page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT_MS)

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
                page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT_MS)
                break

    if "login.itp" in page.url:
        raise RuntimeError("오더퀸 로그인에 실패했습니다 - 아이디/비밀번호를 확인해주세요.")


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
    page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT_MS)

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
                page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT_MS)
                break

    # ✅ 그래도 로그인 페이지면: 디버그 스크린샷 저장하고 중단
    if "login.itp" in page.url:
        debug_login = save_path.replace(".xlsx", "_login_debug.png")
        page.screenshot(path=debug_login, full_page=True)
        raise RuntimeError(f"Login seems to have failed. Screenshot saved: {debug_login}")

    # 2️⃣ 매출 리포트 페이지 이동
    page.goto(REPORT_URL, wait_until="domcontentloaded", timeout=PAGE_GOTO_TIMEOUT_MS)
    page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT_MS)

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
    page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT_MS)

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
# (2026-08-03: networkidle 타임아웃으로 재시도 2번이 같은 지점에서 모두
# 실패한 사례가 있어 - NETWORKIDLE_TIMEOUT_MS를 늘린 것과 별개로 - 여유를
# 한 번 더 뒀다.)
DOWNLOAD_MAX_ATTEMPTS = 3
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


def register_menu_item(
    login_id: str, login_pw: str, barcode: str, menu_name: str, sale_price: int, class_cd: str,
) -> dict:
    """오더퀸 "메뉴관리"(MNU01020.itp)에 상품 하나를 새로 등록한다. 신제품
    입고 시 바코드 앱에서 스캔한 값을 오더퀸에도 바로 등록해, 점주가 앱과
    오더퀸을 오가며 바코드를 두 번 입력하지 않게 하기 위함이다 - 매장 POS에
    직접 반영되는 쓰기 작업이므로 호출부(app)에서 사용자가 명시적으로 누른
    "등록" 버튼에서만 불러야 한다.

    분류(classCd)/상품명(menuNm, menuFullNm)/판매가(salePrice)/바코드
    (barcodeNo)만 채우고, 나머지 필드(사용여부/진열구분 등)는 오더퀸
    등록 폼 자체의 기본값(실측 확인: 전부 정상적으로 미리 채워져 있음)을
    그대로 둔다.

    반환값: {"ok": True} 또는 {"ok": False, "message": "..."} - 오더퀸이
    자체적으로 띄우는 안내/오류 메시지(예: 바코드 중복)를 그대로 담아
    돌려줘서 앱에서 사용자에게 정확한 이유를 보여줄 수 있게 한다."""
    if class_cd not in CLASS_CODES.values():
        raise ValueError(f"알 수 없는 분류 코드: {class_cd}")

    with browser_limit.browser_semaphore, sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            context = browser.new_context()
            page = context.new_page()

            # 저장 시 오더퀸이 confirm/alert 다이얼로그로 결과를 알려준다
            # (실측: "저장하시겠습니까?" 확인창 뒤 "저장되었습니다"/오류 안내) -
            # 전부 자동으로 수락하면서 메시지를 모아둔다.
            dialog_messages: list[str] = []

            def _on_dialog(dialog):
                dialog_messages.append(dialog.message)
                dialog.accept()

            page.on("dialog", _on_dialog)

            _login(page, login_id, login_pw)

            page.goto(MENU_LIST_URL, wait_until="domcontentloaded", timeout=PAGE_GOTO_TIMEOUT_MS)
            page.wait_for_load_state("networkidle", timeout=NETWORKIDLE_TIMEOUT_MS)

            page.locator('button:has-text("등록"), a:has-text("등록")').first.click()
            page.wait_for_timeout(1000)

            page.select_option("#classCd", class_cd)
            # menuNm(짧은 이름)은 POS 화면 표시용이라 길이 제한이 있을 수 있어
            # 안전하게 40자로 자르고, menuFullNm(전체 상품명)에는 원본을 그대로 둔다.
            page.fill("#menuNm", menu_name[:40])
            page.fill("#menuFullNm", menu_name)
            page.fill("#salePrice", str(int(sale_price)))
            page.fill("#barcodeNo", barcode)

            dialog_messages.clear()
            # 페이지 전체에는 "저장" 버튼이 여러 개(다른 숨겨진 패널 것까지)
            # 있어서 그냥 .first를 쓰면 안 보이는 엉뚱한 버튼을 눌러 타임아웃
            # 난다(실측 확인) - 지금 열려 있는 등록 폼을 감싸는 컨테이너
            # 범위로 좁혀서 그 안의(보이는) 저장 버튼만 클릭한다.
            page.locator('.content_in_in.detail_in button:has-text("저장")').first.click()
            page.wait_for_timeout(2500)

            combined = " ".join(dialog_messages)
            failure_keywords = ("실패", "중복", "오류", "이미", "다시")
            if any(kw in combined for kw in failure_keywords):
                return {"ok": False, "message": combined or "등록에 실패했습니다."}
            return {"ok": True, "message": combined}
        finally:
            browser.close()