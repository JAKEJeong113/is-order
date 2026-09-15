# orderqueen_bot.py

import os
import re
import time
from datetime import date, datetime
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

import browser_limit
import vendors

LOGIN_URL = "https://www.orderqueen.kr/backoffice_admin/login.itp"


def _block_heavy_resources(page) -> None:
    """등록 자동화는 텍스트/폼 입력만 필요하므로 이미지·폰트·미디어를
    차단해 페이지 로딩을 앞당긴다(다른 도매처 봇들과 동일한 패턴)."""
    page.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in ("image", "media", "font")
        else route.continue_(),
    )

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

# "메뉴관리"(MNU01020/1021, 서버 등록)와 실제 매장 키오스크 기기의 화면은
# 별개다 - "화면관리(유통)"(MNU02030.itp)에서 코너별로 메뉴를 따로 등록해야
# 기기에서 바코드 스캔 시 인식된다(실측 확인·사용자 확인: 오더퀸 모바일 앱은
# 이 둘을 한 번에 처리하지만 PC 화면/이 API 자동화 경로는 메뉴관리만 반영하고
# 화면관리(유통)는 그대로 빈 채로 남는다). 그래서 register_menu_item/
# update_menu_item으로 저장한 뒤 항상 push_menu_item_to_kiosk_screen도 같이
# 호출해야 실제로 매장에서 팔 수 있는 상태가 된다.
SCREEN_MANAGEMENT_URL = "https://www.orderqueen.kr/backoffice_admin/MNU02030.itp"

# 코너 코드(cornerCd)는 매장마다 다를 수 있다(코너관리에서 매장이 직접
# 만들고 이름 붙이는 구조 - class_cd와 같은 이유). 그래서 코드가 아니라
# 이름으로 찾는다. 대부분 매장이 "상품"이라는 이름의 코너를 실제 판매
# 상품 목록으로 쓴다(실측 확인).
KIOSK_SCREEN_CORNER_NAME = "상품"

# 상품 등록 화면(MNU01021.itp)의 "분류" 드롭다운 - 실측으로 확인한 한 매장의
# 실제 분류 코드값. 앱에서 사용자가 직접 고르는 드롭다운을 채우는 데 쓴다.
# 주의: 이 숫자 코드는 매장마다 다르다(점주가 오더퀸에서 분류를 추가/삭제/
# 순서변경하면 코드가 바뀜). 그래서 실제 등록 시에는 이 코드값이 아니라
# 분류 "이름"으로 각 매장 드롭다운에서 같은 이름을 찾아 고른다
# (register_menu_item 참고) - 코드로만 고르면 "모든 계정에 추가" 시 일부
# 매장에서 엉뚱한 분류에 들어가거나 등록 자체가 실패한다.
CLASS_CODES = {
    "아이스크림": "003",
    "음료수": "004",
    "간식": "006",
    "완구,문구": "007",
    "과자": "015",
}


def _wait_past_login(page) -> None:
    """로그인 제출 후 로그인 페이지를 벗어났는지(리다이렉트 완료)만 기다린다.
    오더퀸은 로그인 후 페이지가 백그라운드 요청을 계속 띄워서
    wait_for_load_state("networkidle")가 좀처럼 안 끝난다 - 특히 "모든 계정에
    추가"로 여러 브라우저가 동시에 돌 때 서버 자원 경합까지 겹치면 45초를
    넘겨 타임아웃 나는 사례가 확인됐다(실측). networkidle 대신 URL이
    login.itp를 벗어났는지 + DOM 로드만 확인한다."""
    try:
        page.wait_for_url(lambda u: "login.itp" not in u, timeout=15000)
    except PWTimeoutError:
        pass
    try:
        page.wait_for_load_state("domcontentloaded", timeout=PAGE_GOTO_TIMEOUT_MS)
    except PWTimeoutError:
        pass


def _login(page, login_id: str, login_pw: str) -> None:
    """로그인 페이지에서 아이디/비번을 입력해 로그인한다. 실패하면
    RuntimeError를 던진다(디버그 스크린샷 없이 - 호출부에서 필요하면 남긴다)."""
    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=PAGE_GOTO_TIMEOUT_MS)

    id_box = page.locator('input[type="text"]').first
    pw_box = page.locator('input[type="password"]').first
    id_box.fill(login_id)
    pw_box.fill(login_pw)
    pw_box.press("Enter")
    _wait_past_login(page)

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
                _wait_past_login(page)
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


REGISTER_MAX_ATTEMPTS = 2
REGISTER_RETRY_DELAY_SECONDS = 3


def register_menu_item_with_retry(
    login_id: str, login_pw: str, barcode: str, menu_name: str, sale_price: int, class_cd: str,
    store_id: str | None = None, class_name: str | None = None,
) -> dict:
    """register_menu_item을 감싸서 한 번 더 시도한다("모든 계정에 추가"로
    여러 매장을 병렬(ThreadPoolExecutor)로 등록할 때, Render 서버에서
    헤드리스 브라우저 여러 개가 동시에 뜨는 순간의 자원 경합으로
    Page.goto가 net::ERR_ABORTED로 끊기는 사례가 실측 확인됨). 이 실패는
    등록 모달을 열기도 전(즉 아직 아무것도 안 쓴) 단계에서 나므로 재시도해도
    중복 등록 위험이 없고, 혹시 더 뒤에서 실패해도 오더퀸 자체가 바코드
    중복 등록을 막아주므로("이미 등록된 바코드" 안내) 안전하다. 완전히
    새 브라우저로 다시 시도한다(예외만 재시도 - {"ok": False, ...}로
    정상 반환된 오더퀸 자체 안내 메시지는 재시도하지 않고 그대로 전달)."""
    last_error: Exception | None = None
    for attempt in range(1, REGISTER_MAX_ATTEMPTS + 1):
        try:
            return register_menu_item(
                login_id, login_pw, barcode=barcode, menu_name=menu_name,
                sale_price=sale_price, class_cd=class_cd, store_id=store_id, class_name=class_name,
            )
        except Exception as e:
            last_error = e
            if attempt < REGISTER_MAX_ATTEMPTS:
                time.sleep(REGISTER_RETRY_DELAY_SECONDS)
    raise last_error


def _dismiss_popups(page) -> None:
    """jQuery UI 팝업(예: "사용자 정보" - 비밀번호 3개월 경과 안내, 공지사항
    등)이 오버레이와 함께 뜨는 경우가 있어 "등록"/"저장" 버튼 클릭을
    가로막는다(실측 확인). 같은 클래스(.ui-dialog-titlebar-close)의 닫기
    버튼이 숨겨진 다른 팝업 것까지 여러 개 있어서 .first로 찾으면 엉뚱한(안
    보이는) 걸 클릭하게 되므로, jQuery UI 표준 닫기 방법인 Escape 키를 먼저
    쓴다 - 어떤 팝업이 열려있든 범용으로 통한다."""
    for _ in range(2):
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
        except Exception:
            pass
    try:
        visible_close = page.locator(".ui-dialog-titlebar-close:visible")
        if visible_close.count() > 0:
            visible_close.first.click(timeout=3000)
            page.wait_for_timeout(300)
    except Exception:
        pass
    # 공지사항류 배너(예: "오늘 하루동안 보지 않기 닫기")는 jQuery UI
    # dialog가 아니라 그냥 텍스트가 "닫기"인 버튼/링크일 수 있어(실측 확인 -
    # Escape만으론 안 닫히고 화면에 남아 저장 버튼 클릭까지 먹통으로 만듦)
    # 별도로도 시도한다.
    try:
        generic_close = page.locator('button:has-text("닫기"):visible, a:has-text("닫기"):visible')
        if generic_close.count() > 0:
            generic_close.first.click(timeout=3000)
            page.wait_for_timeout(300)
    except Exception:
        pass
    # 2026-09-14부터 새로 뜨기 시작한 전체화면 공지 배너(예: "문자 발송 2차
    # 인증 적용 안내") - 닫기가 <span id="close">닫기</span>라 위의
    # button/a 셀렉터로는 못 잡고, 전체 화면을 덮어 이후 모든 클릭을
    # 무반응으로 만든다(실측 확인 - 화면관리(유통) 자동화가 이걸로 계속
    # 조용히 실패했었음). 페이지마다 다시 뜰 수 있어 매번 시도한다.
    try:
        notice_close = page.locator("#popup_box1 #close:visible, span#close:visible")
        if notice_close.count() > 0:
            notice_close.first.click(force=True, timeout=3000)
            page.wait_for_timeout(300)
    except Exception:
        pass


def _normalize_for_match(text: str) -> str:
    """한글/영문/숫자만 남기고 나머지(공백, 대괄호 등 특수문자)는 제거한다.
    오더퀸에 저장된 상품명을 목록에서 다시 찾아 등록 여부를 확인할 때 쓴다
    (아래 register_menu_item 참고) - 오더퀸이 저장 시 대괄호 등 특수문자를
    다르게 표시/치환하는 경우가 있어(실측: "[멕시칸타코]"가 들어간 상품명이
    실제로는 저장됐는데도 원문 그대로 비교하면 못 찾아서 등록 실패로
    오판했음), 그런 차이에 흔들리지 않게 정규화해서 비교한다."""
    return re.sub(r"[^0-9A-Za-z가-힣]", "", text)


def _search_barcode_row(page, barcode: str) -> dict | None:
    """메뉴관리 목록에서 바코드로 검색해 실제 그 바코드로 등록된 행을 찾는다.
    화면 텍스트만 보고 판단하지 않고 data-barcode-no 속성으로 정확히
    대조한다(register_menu_item이 겪었던 "다른 상품이 섞여 보이는" 오탐을
    피하기 위함). 없으면 None."""
    search_box = page.locator("#schBarcodeNo").first
    search_box.fill(barcode)
    search_box.press("Enter")
    page.wait_for_timeout(1200)
    row_loc = page.locator(f'tr[data-barcode-no="{barcode}"]')
    if row_loc.count() == 0:
        return None
    row = row_loc.first
    price_text = row.locator(".fnSalePrice").first.inner_text().strip()
    menu_nm_cell = row.locator(".fnMenuNm").first
    menu_nm = (menu_nm_cell.get_attribute("data-menu-nm") or menu_nm_cell.inner_text()).strip()
    return {
        "store_no": row.get_attribute("data-store-no"),
        "menu_cd": row.get_attribute("data-menu-cd"),
        "sale_price": int(re.sub(r"[^0-9]", "", price_text) or "0"),
        "menu_nm": menu_nm,
    }


def _open_menu_detail(page, store_no: str, menu_cd: str) -> None:
    """목록의 "상세" 버튼은 <a> 태그가 아니라(그래서 링크 텍스트로 찾으려던
    첫 시도가 실패했다 - 실측 확인), 클릭 시 JS가 숨겨진 #FrmSearch 폼의
    storeNo/menuCd를 채우고 action을 MNU01021.itp로 바꿔 제출하는 방식이다.
    같은 폼 제출을 그대로 재현해서 상세/수정 화면으로 들어간다."""
    page.evaluate(
        """([storeNo, menuCd]) => {
            document.querySelector('#storeNo').value = storeNo;
            document.querySelector('#menuCd').value = menuCd;
            const f = document.querySelector('#FrmSearch');
            f.setAttribute('target', '');
            f.setAttribute('action', '/backoffice_admin/MNU01021.itp');
        }""",
        [store_no, menu_cd],
    )
    with page.expect_navigation(timeout=PAGE_GOTO_TIMEOUT_MS):
        page.evaluate("document.querySelector('#FrmSearch').submit()")


def update_menu_item(
    login_id: str, login_pw: str, barcode: str, menu_name: str, new_price: int,
    store_id: str | None = None,
) -> dict:
    """이미 오더퀸에 등록된 상품의 상품명/판매가를 앱에서 입력한 값으로
    덮어쓴다. register_menu_item은 신규 등록 폼만 열 수 있어서, 이미 등록된
    바코드를 다시 등록하면 오더퀸이 저장을 거부하는데도(실측: "이미 등록된
    바코드입니다") 목록에 그 바코드+이름이 이미 있다는 이유로 등록 검증을
    통과해버려 "성공"으로 잘못 보고하고 값은 그대로 남는 문제가 있었다
    (실측 사례: 8801062631094 "빅 가나마일드" 가격 미반영). 처음엔 가격만
    갱신했는데, 사용자가 검색 결과 화면에서 상품명도 직접 고쳐서 등록하고
    싶어해(카탈로그 이름이 마음에 안 들 때 등) 이름도 같이 갱신하도록
    넓혔다. 목록에서 해당 바코드 행을 찾아 상세 화면(MNU01021.itp)으로
    들어가 menuNm/menuFullNm/salePrice를 바꿔 저장한다.

    반환값에 barcode 자체가 목록에 없으면 "not_found": True를 같이 담아,
    호출부(register_or_update_menu_item)가 신규 등록으로 넘어갈 수 있게
    한다."""
    vendor_id = "orderqueen"

    with browser_limit.browser_semaphore, sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            cached_state = vendors.get_session_state(store_id, vendor_id) if store_id else None
            context = browser.new_context(storage_state=cached_state) if cached_state else browser.new_context()
            page = context.new_page()
            _block_heavy_resources(page)

            dialog_messages: list[str] = []

            def _on_dialog(dialog):
                dialog_messages.append(dialog.message)
                dialog.accept()

            page.on("dialog", _on_dialog)

            def _open_menu_list_logged_in() -> bool:
                page.goto(MENU_LIST_URL, wait_until="domcontentloaded", timeout=PAGE_GOTO_TIMEOUT_MS)
                if "login.itp" in page.url:
                    return False
                page.wait_for_selector('button:has-text("등록"), a:has-text("등록")', timeout=15000)
                return True

            if cached_state:
                logged_in = _open_menu_list_logged_in()
                if not logged_in:
                    context.clear_cookies()
                    _login(page, login_id, login_pw)
                    _open_menu_list_logged_in()
                    cached_state = None
            else:
                _login(page, login_id, login_pw)
                _open_menu_list_logged_in()

            _dismiss_popups(page)
            row_info = _search_barcode_row(page, barcode)
            if not row_info:
                return {
                    "ok": False, "not_found": True,
                    "message": "해당 바코드로 등록된 상품을 찾을 수 없습니다.",
                }

            name_already_same = _normalize_for_match(row_info["menu_nm"]) == _normalize_for_match(menu_name[:40])
            price_already_same = row_info["sale_price"] == int(new_price)
            if name_already_same and price_already_same:
                # 이미 입력한 값과 같으면 굳이 저장을 다시 안 해도 된다.
                return {"ok": True, "message": "이미 동일한 내용으로 등록되어 있습니다."}

            _open_menu_detail(page, row_info["store_no"], row_info["menu_cd"])
            _dismiss_popups(page)

            page.locator("#menuNm").first.fill(menu_name[:40])
            page.locator("#menuFullNm").first.fill(menu_name)
            page.locator("#salePrice").first.fill(str(int(new_price)))

            dialog_messages.clear()
            try:
                with page.expect_navigation(timeout=15000):
                    page.locator("#btn-submit").first.click(force=True)
            except PWTimeoutError:
                # 검증 오류 등으로 저장이 막히면 alert만 뜨고 페이지 이동은
                # 없다 - dialog_messages에 원인이 남아있으니 그대로 진행한다.
                pass
            page.wait_for_timeout(500)
            combined = " ".join(dialog_messages)

            # dialog 메시지만 믿지 않고, 목록을 다시 조회해서 실제 표시가로
            # 반영 여부를 직접 확인한다(register_menu_item과 같은 이유).
            page.goto(MENU_LIST_URL, wait_until="domcontentloaded", timeout=PAGE_GOTO_TIMEOUT_MS)
            _dismiss_popups(page)
            row_info2 = _search_barcode_row(page, barcode)
            if not row_info2:
                return {"ok": False, "message": combined or "저장 확인에 실패했습니다."}

            verified = (
                row_info2["sale_price"] == int(new_price)
                and _normalize_for_match(row_info2["menu_nm"]) == _normalize_for_match(menu_name[:40])
            )

            if store_id and verified:
                vendors.save_session_state(store_id, vendor_id, context.storage_state())

            if not verified:
                return {
                    "ok": False,
                    "message": combined or f"수정이 반영되지 않았습니다(현재 {row_info2['menu_nm']} / {row_info2['sale_price']}원).",
                }
            return {"ok": True, "message": combined or "수정되었습니다."}
        finally:
            browser.close()


def register_menu_item(
    login_id: str, login_pw: str, barcode: str, menu_name: str, sale_price: int, class_cd: str,
    store_id: str | None = None, class_name: str | None = None,
) -> dict:
    """오더퀸 "메뉴관리"(MNU01020.itp)에 상품 하나를 새로 등록한다. 신제품
    입고 시 바코드 앱에서 스캔한 값을 오더퀸에도 바로 등록해, 점주가 앱과
    오더퀸을 오가며 바코드를 두 번 입력하지 않게 하기 위함이다 - 매장 POS에
    직접 반영되는 쓰기 작업이므로 호출부(app)에서 사용자가 명시적으로 누른
    "등록" 버튼에서만 불러야 한다.

    분류/상품명(menuNm, menuFullNm)/판매가(salePrice)/바코드(barcodeNo)만
    채우고, 나머지 필드(사용여부/진열구분 등)는 오더퀸 등록 폼 자체의
    기본값(실측 확인: 전부 정상적으로 미리 채워져 있음)을 그대로 둔다.
    분류는 class_name(분류 이름) → class_cd(코드값) → "미분류"류 항목
    순서로 이 매장 드롭다운에서 찾아 고른다 - 코드값은 매장마다 다를 수
    있어서("모든 계정에 추가" 시 매장별로 분류 구성이 제각각) 이름 매칭을
    우선하고, 적당한 분류가 없으면 "미분류"에 넣는다.

    store_id를 주면(앱에서는 항상 준다) 다른 도매처 봇들(godomall_bot의
    add_to_cart 등)과 같은 방식으로 로그인 세션(쿠키)을 vendors.py에
    캐싱한다 - 매번 로그인 페이지 이동/입력/networkidle 대기를 반복하는 게
    전체 등록 시간의 대부분을 차지해서(실측 체감), 두 번째 등록부터는
    로그인을 통째로 건너뛰어 크게 빨라진다. 캐시가 없거나 만료됐으면 그때만
    새로 로그인하고, 성공하면 갱신된 세션을 다시 저장해둔다.

    반환값: {"ok": True} 또는 {"ok": False, "message": "..."} - 오더퀸이
    자체적으로 띄우는 안내/오류 메시지(예: 바코드 중복)를 그대로 담아
    돌려줘서 앱에서 사용자에게 정확한 이유를 보여줄 수 있게 한다."""
    if class_cd not in CLASS_CODES.values():
        raise ValueError(f"알 수 없는 분류 코드: {class_cd}")

    vendor_id = "orderqueen"

    with browser_limit.browser_semaphore, sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            cached_state = vendors.get_session_state(store_id, vendor_id) if store_id else None
            context = browser.new_context(storage_state=cached_state) if cached_state else browser.new_context()
            page = context.new_page()
            _block_heavy_resources(page)

            # 저장 시 오더퀸이 confirm/alert 다이얼로그로 결과를 알려준다
            # (실측: "저장하시겠습니까?" 확인창 뒤 "저장되었습니다"/오류 안내) -
            # 전부 자동으로 수락하면서 메시지를 모아둔다.
            dialog_messages: list[str] = []

            def _on_dialog(dialog):
                dialog_messages.append(dialog.message)
                dialog.accept()

            page.on("dialog", _on_dialog)

            def _open_menu_list_logged_in() -> bool:
                """상품 목록 페이지로 이동해서 실제로 로그인된 상태인지
                돌려준다. networkidle 전체 대기 대신, 서버가 이미 리다이렉트를
                끝낸 뒤의 URL만 확인한다(다른 봇들과 동일한 패턴) - MNU01020.itp
                자체에도 숨겨진 input[type=password]가 있어서(다른 용도의
                숨김 패널) 로그인 폼과 구분하는 선택자로는 못 쓴다(실측
                확인 - 그 숨김 필드에 계속 붙잡혀 타임아웃남). 로그인된
                상태로 확인되면 "등록" 버튼이 실제로 뜰 때까지만 마저 기다린다
                (오더퀸 사이트가 백그라운드 요청을 계속 띄우는 편이라
                networkidle은 불필요하게 오래 걸림 - SAL03020.itp 리포트
                페이지에서 실측된 것과 같은 현상)."""
                page.goto(MENU_LIST_URL, wait_until="domcontentloaded", timeout=PAGE_GOTO_TIMEOUT_MS)
                if "login.itp" in page.url:
                    return False
                page.wait_for_selector('button:has-text("등록"), a:has-text("등록")', timeout=15000)
                return True

            if cached_state:
                logged_in = _open_menu_list_logged_in()
                if not logged_in:
                    context.clear_cookies()
                    _login(page, login_id, login_pw)
                    _open_menu_list_logged_in()
                    cached_state = None  # 이번엔 새로 로그인했으니 아래서 세션을 다시 저장
            else:
                _login(page, login_id, login_pw)
                _open_menu_list_logged_in()

            # 페이지에 id="barcodeNo"가 두 개 있다(하나는 숨김 input, 하나는
            # 등록 모달 안의 실제 입력창 - 실측 확인, 유효하지 않은 HTML이지만
            # 실제로 이렇게 되어 있음). ".content_in_in.detail_in"도 페이지에
            # 두 개(등록용/상세보기용) 있을 수 있어(실측 확인) :visible로
            # 지금 실제로 보이는 것만 골라야 한다.
            form = page.locator(".content_in_in.detail_in:visible")

            # 이 사이트가 팝업/모달 렌더링 타이밍이 불안정해서(실측: 같은
            # 코드가 어떨 때는 바로 되고 어떨 때는 "등록" 클릭이 씹힌 것처럼
            # 모달이 안 열림) 한 번에 안 되면 팝업 재정리 + 재클릭을 몇 번
            # 더 시도한다.
            modal_opened = False
            for attempt in range(3):
                page.wait_for_timeout(500)
                _dismiss_popups(page)
                page.locator('button:has-text("등록"), a:has-text("등록")').first.click(force=True)
                try:
                    form.locator("#barcodeNo").first.wait_for(state="visible", timeout=6000)
                    modal_opened = True
                    break
                except PWTimeoutError:
                    continue
            if not modal_opened:
                return {"ok": False, "message": "등록 화면을 열지 못했습니다. 잠시 후 다시 시도해주세요."}

            # 분류 선택: 코드값(class_cd)은 매장마다 다를 수 있으므로, 우선
            # 분류 "이름"(class_name)으로 이 매장 드롭다운에서 같은 이름을
            # 찾아 고른다. 이름이 안 넘어왔거나 못 찾으면 코드값으로, 그것도
            # 없으면 "미분류"류 항목으로 넣는다(사용자 요청). "미분류"조차
            # 없으면 그 매장만 실패로 돌려주고 이유를 알려준다("모든 계정에
            # 추가" 시 매장별로 결과가 갈릴 수 있음).
            class_select = form.locator("#classCd").first
            _opts = class_select.locator("option")
            _entries: list[tuple[str, str]] = []
            for _i in range(_opts.count()):
                _o = _opts.nth(_i)
                _val = (_o.get_attribute("value") or "").strip()
                _txt = (_o.text_content() or "").strip()
                if _val:  # 맨 앞 "선택하세요" 같은 빈 값 항목은 건너뛴다
                    _entries.append((_val, _txt))

            _chosen = None
            if class_name:
                _nm = class_name.strip()
                _chosen = next((v for v, t in _entries if t == _nm), None)
                if not _chosen:
                    _chosen = next((v for v, t in _entries if _nm and (_nm in t or t in _nm)), None)
            if not _chosen and class_cd:
                _chosen = next((v for v, t in _entries if v == class_cd), None)
            if not _chosen:
                _uncat_kw = ("미분류", "미지정", "분류없음", "분류 없음", "기타")
                _chosen = next(
                    (v for v, t in _entries if any(_k in t for _k in _uncat_kw)), None,
                )
            if not _chosen:
                return {
                    "ok": False,
                    "message": f"이 매장 오더퀸에 '{class_name or class_cd}' 분류도, '미분류' 분류도 없어 "
                    "등록하지 못했습니다. 오더퀸에서 '미분류' 분류를 만들어두면 다음부터 자동으로 들어갑니다.",
                }
            class_select.select_option(_chosen)
            # menuNm(짧은 이름)은 POS 화면 표시용이라 길이 제한이 있을 수 있어
            # 안전하게 40자로 자르고, menuFullNm(전체 상품명)에는 원본을 그대로 둔다.
            form.locator("#menuNm").first.fill(menu_name[:40])
            form.locator("#menuFullNm").first.fill(menu_name)
            form.locator("#salePrice").first.fill(str(int(sale_price)))
            form.locator("#barcodeNo").first.fill(barcode)

            # 필드를 채우는 동안(특히 select_option의 change 이벤트 등으로)
            # 새 팝업이 뜰 수 있어 저장 직전에 한 번 더 정리한다.
            _dismiss_popups(page)

            dialog_messages.clear()
            # 페이지 전체에는 "저장" 버튼이 여러 개(다른 숨겨진 패널 것까지)
            # 있어서 그냥 .first를 쓰면 안 보이는 엉뚱한 버튼을 눌러 타임아웃
            # 난다(실측 확인) - 지금 열려 있는(보이는) 등록 폼 범위로 좁혀서
            # 그 안의 저장 버튼만 클릭한다.
            form.locator('button:has-text("저장")').first.click(force=True)
            page.wait_for_timeout(1500)

            # dialog 메시지만 믿고 성공 여부를 판단했다가, 세션 재사용 시
            # 다이얼로그가 안 뜨는(혹은 못 잡는) 경우가 있어 "성공"으로
            # 잘못 보고하면서 실제로는 등록이 안 된 사고가 실측으로 확인됐다
            # (매장 POS에 반영되는 쓰기 작업이라 이런 오탐이 특히 위험함) -
            # dialog 메시지는 참고만 하고, 방금 넣은 바코드로 목록을 다시
            # 검색해서 실제로 등록됐는지 직접 확인한 결과만 신뢰한다.
            combined = " ".join(dialog_messages)
            failure_keywords = ("실패", "중복", "오류", "이미", "다시", "존재")
            dialog_says_fail = any(kw in combined for kw in failure_keywords)

            page.goto(MENU_LIST_URL, wait_until="domcontentloaded", timeout=PAGE_GOTO_TIMEOUT_MS)
            _dismiss_popups(page)
            search_box = page.locator("#schBarcodeNo").first
            search_box.fill(barcode)
            search_box.press("Enter")
            page.wait_for_timeout(1200)
            list_text = page.inner_text("body")
            # 바코드만 확인하면 부족하다(실측으로 발견한 심각한 오탐 사례:
            # 이미 다른 상품이 그 바코드로 등록돼 있으면 오더퀸이 "해당
            # 바코드로 등록된 메뉴가 존재합니다"라며 저장을 거부하는데, 이
            # 문구는 실패 키워드에 안 걸리고 검색 결과엔 그 "기존" 상품이
            # 그대로 나오니 verified가 True가 돼버려서 우리가 새로 등록한
            # 것처럼 잘못 보고했었다) - 우리가 넣은 상품명까지 같이 나와야만
            # 진짜 우리 등록이 반영된 것으로 인정한다.
            #
            # 원문 그대로 비교하면 안 된다(실측 확인: 상품명에 대괄호가 들어간
            # "롯데 도리토스 [멕시칸타코] 70g"을 등록했더니 실제로는 저장에
            # 성공했는데(오더퀸 확인창도 "저장 되었습니다") 목록 페이지의
            # 표시 방식이 달라서(특수문자 처리 차이로 추정) 원문 그대로는
            # 못 찾아 "등록 실패"로 잘못 보고 - 게다가 그 "실패" 메시지에
            # dialog 원문("저장 되었습니다")을 그대로 붙여서 "실패 - 저장
            # 되었습니다"라는 앞뒤가 안 맞는 문구까지 나갔다). 한글/영문/숫자만
            # 남기고 비교해서 이런 표시 차이에 흔들리지 않게 한다.
            normalized_list = _normalize_for_match(list_text)
            normalized_name = _normalize_for_match(menu_name[:40])
            name_matches = bool(normalized_name) and normalized_name in normalized_list
            verified = barcode in list_text and name_matches

            if store_id and verified:
                # 세션이 그대로 유효했던 경우(cached_state가 None으로 안 바뀜)도
                # 만료 시점을 최대한 늦추려고 매번 갱신 저장해둔다.
                vendors.save_session_state(store_id, vendor_id, context.storage_state())

            if not verified:
                return {"ok": False, "message": combined or "등록되지 않았습니다. 다시 시도해주세요."}
            if dialog_says_fail:
                # 목록엔 나타났는데 오더퀸이 실패성 안내도 같이 띄운 애매한
                # 경우 - 실제로 반영은 됐으니 성공으로 처리하되 원문 메시지를
                # 그대로 남겨 사용자가 확인할 수 있게 한다.
                return {"ok": True, "message": f"등록됨 (참고: {combined})"}
            return {"ok": True, "message": combined or "저장 되었습니다."}
        finally:
            browser.close()


def _find_corner_code(page, corner_name: str) -> str | None:
    options = page.locator("select#cornerCd option")
    for i in range(options.count()):
        o = options.nth(i)
        if (o.text_content() or "").strip() == corner_name:
            return o.get_attribute("value")
    return None


def _barcode_in_current_corner(page, barcode: str) -> bool:
    """지금 코너 필터가 적용된 화면관리(유통) 목록에서 이 바코드가 이미
    있는지 확인한다(호출 전에 select#cornerCd를 원하는 코너로 맞춰둬야 함)."""
    page.locator('#FrmSearch input[name="barcodeNo"]').fill(barcode)
    page.locator("#btn-search").first.click(force=True)
    page.wait_for_timeout(1000)
    return barcode in page.inner_text("#innerHtmlDiv")


def push_menu_item_to_kiosk_screen(
    login_id: str, login_pw: str, barcode: str, store_id: str | None = None,
) -> dict:
    """메뉴관리(MNU01020/1021)에 등록/수정한 상품을 실제 매장 키오스크
    화면에도 반영한다("화면관리(유통)" - MNU02030.itp). 오더퀸 모바일 앱으로
    신제품을 등록하면 이 둘을 한 번에 처리해주지만, PC 화면(그리고 이걸
    자동화하는 이 봇)으로 등록하면 메뉴관리만 반영되고 화면관리(유통)는
    그대로 비어있어서, 매장 키오스크에서 바코드를 스캔해도 인식되지 않는
    문제가 있었다(실측 확인·사용자 확인). register_menu_item/update_menu_item
    으로 메뉴를 저장한 뒤 반드시 이 함수도 같이 불러야 한다.

    이미 그 코너에 등록되어 있으면(예: update_menu_item으로 기존 상품
    가격만 바꾼 경우) 아무 것도 하지 않고 성공으로 돌려준다 - 재등록해도
    오더퀸이 별 문제 없이 받아주는 것으로 보이지만(실측), 굳이 매번 다시
    등록할 이유가 없다."""
    vendor_id = "orderqueen"

    with browser_limit.browser_semaphore, sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            cached_state = vendors.get_session_state(store_id, vendor_id) if store_id else None
            context = browser.new_context(storage_state=cached_state) if cached_state else browser.new_context()
            page = context.new_page()
            _block_heavy_resources(page)

            dialog_messages: list[str] = []

            def _on_dialog(dialog):
                dialog_messages.append(dialog.message)
                dialog.accept()

            page.on("dialog", _on_dialog)

            def _open_screen_page_logged_in() -> bool:
                page.goto(SCREEN_MANAGEMENT_URL, wait_until="domcontentloaded", timeout=PAGE_GOTO_TIMEOUT_MS)
                if "login.itp" in page.url:
                    return False
                page.wait_for_selector("select#cornerCd", timeout=15000)
                return True

            if cached_state:
                logged_in = _open_screen_page_logged_in()
                if not logged_in:
                    context.clear_cookies()
                    _login(page, login_id, login_pw)
                    _open_screen_page_logged_in()
                    cached_state = None
            else:
                _login(page, login_id, login_pw)
                _open_screen_page_logged_in()

            page.wait_for_timeout(500)
            _dismiss_popups(page)

            corner_cd = _find_corner_code(page, KIOSK_SCREEN_CORNER_NAME)
            if not corner_cd:
                return {
                    "ok": False,
                    "message": f"'{KIOSK_SCREEN_CORNER_NAME}' 코너를 찾을 수 없습니다. 코너관리에서 먼저 만들어주세요.",
                }

            page.locator("select#cornerCd").select_option(corner_cd)
            page.wait_for_timeout(800)
            _dismiss_popups(page)

            if _barcode_in_current_corner(page, barcode):
                if store_id:
                    vendors.save_session_state(store_id, vendor_id, context.storage_state())
                return {"ok": True, "message": "이미 화면(키오스크)에 등록되어 있습니다.", "already": True}

            page.locator(".btn-add").first.click(force=True)
            page.wait_for_timeout(1000)  # 모달의 초기 자동검색(필터 없음) 완료 대기 - 아래서 필터링된 결과로 덮어씀
            page.locator("#pop-menu-add #schType").select_option("S")

            # 모달의 후보 목록은 AJAX로 갱신되는데, 고정 대기시간만 믿고
            # 체크박스를 누르면 아직 안 바뀐 이전(필터 전) 목록의 엉뚱한
            # 행을 눌러 완전히 다른 상품을 코너에 등록하게 되는 사고가
            # 실측으로 발생했다 - 반드시 검색 응답 자체를 기다린 뒤,
            # 실제로 화면에 뜬 행의 바코드가 우리가 찾는 바코드와 일치하는지
            # 한 번 더 확인하고서만 체크한다.
            with page.expect_response(lambda r: "MNU04011_AmLST" in r.url, timeout=15000):
                page.locator("#pop-menu-add #barcodeNo").fill(barcode)
                page.locator("#btn-menu-search").first.click(force=True)
            page.wait_for_timeout(500)

            rows = page.locator("#tbl-add tbody tr.fnClickRow")
            if rows.count() == 0:
                try:
                    page.evaluate("$('#pop-menu-add').dialog('close')")
                except Exception:
                    pass
                return {
                    "ok": False,
                    "message": "메뉴관리에서 이 바코드를 찾지 못했습니다(먼저 메뉴 등록이 필요합니다).",
                }

            row = rows.first
            row_barcode = (row.locator(".barcode-no").first.inner_text() or "").strip()
            if row_barcode != barcode:
                try:
                    page.evaluate("$('#pop-menu-add').dialog('close')")
                except Exception:
                    pass
                return {"ok": False, "message": f"검색 결과 바코드 불일치(찾음: {row_barcode!r}) - 등록을 건너뜁니다."}

            row.locator('input[name="menuCd"]').click(force=True, timeout=8000)

            dialog_messages.clear()
            page.locator("#btn-add-reg").first.click(force=True, timeout=8000)
            page.wait_for_timeout(1500)
            combined = " ".join(dialog_messages)

            try:
                page.evaluate("$('#pop-menu-add').dialog('close')")
            except Exception:
                pass
            page.wait_for_timeout(300)

            verified = _barcode_in_current_corner(page, barcode)

            if store_id and verified:
                vendors.save_session_state(store_id, vendor_id, context.storage_state())

            if not verified:
                return {"ok": False, "message": combined or "화면(키오스크) 등록 확인에 실패했습니다."}
            return {"ok": True, "message": combined or "화면(키오스크)에 등록되었습니다."}
        finally:
            browser.close()


# push_menu_item_to_kiosk_screen을 앱 요청과 같은 타이밍(동기)에 부르면
# 브라우저 세션을 하나 더 여는 만큼 사용자가 체감하는 등록 시간이 늘어난다
# (실측: 이미 반영된 상품도 최소 6초, 신규는 그 이상 추가). 매장 키오스크
# 반영이 몇십 초~몇 분 늦어도 상관없다는 사용자 확인에 따라, 대신 이
# 작업큐에 넣어두고 별도 스케줄러(main.py)가 뒤에서 처리한다
# (cart_jobs.py의 FOR UPDATE SKIP LOCKED 큐와 같은 패턴).
KIOSK_SCREEN_JOB_MAX_ATTEMPTS = 3


def init_kiosk_screen_job_table() -> None:
    conn = vendors.get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS oq_kiosk_screen_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            store_id TEXT NOT NULL,
            account_id INTEGER NOT NULL,
            barcode TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            result_message TEXT,
            created_at TEXT NOT NULL,
            finished_at TEXT
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_oq_kiosk_screen_jobs_status ON oq_kiosk_screen_jobs (status, id)")
        conn.commit()
    finally:
        conn.close()


def enqueue_kiosk_screen_job(store_id: str, account_id: int, barcode: str) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    conn = vendors.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO oq_kiosk_screen_jobs (store_id, account_id, barcode, status, created_at)
            VALUES (?, ?, ?, 'pending', ?) RETURNING id
            """,
            (store_id, account_id, barcode, now),
        )
        job_id = cur.fetchone()[0]
        conn.commit()
        return job_id
    finally:
        conn.close()


def _claim_kiosk_screen_jobs(limit: int) -> list[dict]:
    """대기 중인 작업을 최대 limit개 원자적으로 집어 processing으로 표시한다
    (동시에 여러 프로세스가 폴링해도 중복 처리 안 되게 - cart_jobs.claim_next_job과
    같은 이유)."""
    conn = vendors.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE oq_kiosk_screen_jobs SET status = 'processing', attempts = attempts + 1
            WHERE id IN (
                SELECT id FROM oq_kiosk_screen_jobs WHERE status = 'pending'
                ORDER BY id ASC FOR UPDATE SKIP LOCKED LIMIT ?
            )
            RETURNING id, store_id, account_id, barcode, attempts
            """,
            (limit,),
        )
        rows = cur.fetchall()
        conn.commit()
        return [
            {"id": r[0], "store_id": r[1], "account_id": r[2], "barcode": r[3], "attempts": r[4]}
            for r in rows
        ]
    finally:
        conn.close()


def _finish_kiosk_screen_job(job_id: int, status: str, message: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn = vendors.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE oq_kiosk_screen_jobs SET status = ?, result_message = ?, finished_at = ? WHERE id = ?",
            (status, message, now, job_id),
        )
        conn.commit()
    finally:
        conn.close()


def process_pending_kiosk_screen_jobs(limit: int = 5) -> dict:
    """대기 중인 화면(키오스크) 반영 작업을 최대 limit개 처리한다. 계정
    정보는 큐에 저장하지 않고(자격증명을 굳이 한 곳에 더 두지 않기 위함)
    처리 시점에 store_id/account_id로 다시 조회한다(register-item API가
    이미 쓰는 것과 같은 조회 함수). 실패해도 attempts가
    KIOSK_SCREEN_JOB_MAX_ATTEMPTS 미만이면 pending으로 되돌려 다음 스케줄에
    재시도하고, 그 이상이면 failed로 확정한다(호출부가 관리자에게 알림)."""
    jobs = _claim_kiosk_screen_jobs(limit)
    done = 0
    failed = 0
    permanently_failed: list[dict] = []
    for job in jobs:
        account = vendors.resolve_store_vendor_account(job["store_id"], "orderqueen", job["account_id"])
        if not account:
            _finish_kiosk_screen_job(job["id"], "failed", "삭제되었거나 존재하지 않는 계정입니다.")
            failed += 1
            permanently_failed.append({**job, "message": "계정을 찾을 수 없음"})
            continue
        try:
            result = push_menu_item_to_kiosk_screen(
                account["login_id"], account["login_pwd"], job["barcode"],
                store_id=f"{job['store_id']}:{job['account_id']}",
            )
        except Exception as e:
            result = {"ok": False, "message": str(e)}

        if result.get("ok"):
            _finish_kiosk_screen_job(job["id"], "done", result.get("message", ""))
            done += 1
            continue

        if job["attempts"] < KIOSK_SCREEN_JOB_MAX_ATTEMPTS:
            # 다음 스케줄에서 다시 시도할 수 있게 pending으로 되돌린다
            # (attempts는 claim 시점에 이미 증가했으므로 그대로 둠).
            conn = vendors.get_conn()
            try:
                cur = conn.cursor()
                cur.execute("UPDATE oq_kiosk_screen_jobs SET status = 'pending' WHERE id = ?", (job["id"],))
                conn.commit()
            finally:
                conn.close()
        else:
            _finish_kiosk_screen_job(job["id"], "failed", result.get("message", ""))
            permanently_failed.append({**job, "message": result.get("message", "")})
        failed += 1

    return {"done": done, "failed": failed, "permanently_failed": permanently_failed}


def register_or_update_menu_item(
    login_id: str, login_pw: str, barcode: str, menu_name: str, sale_price: int, class_cd: str,
    store_id: str | None = None, class_name: str | None = None,
) -> dict:
    """앱의 "오더퀸 등록" 버튼 하나로 신규 등록/기존 상품 상품명·가격 갱신을
    모두 처리한다. 먼저 update_menu_item으로 갱신을 시도해서 - 이미 등록된
    바코드면 그대로 처리되고, "not_found"면 아직 등록 안 된 것이므로
    register_menu_item으로 새로 등록한다. 대부분의 재등록 시도(이미 있는
    상품을 수정만 하는 경우)는 로그인 세션 하나로 끝나고, 정말 신규인
    경우에만 두 번째 세션(등록 폼)이 추가로 열린다.

    이 함수는 메뉴관리(서버) 저장까지만 하고 화면관리(유통)(실제 매장
    키오스크 기기)은 건드리지 않는다 - push_menu_item_to_kiosk_screen이
    별도 브라우저 세션을 한 번 더 여는 탓에 그 자리에서 같이 하면 앱
    사용자가 체감하는 등록 시간이 눈에 띄게 늘어난다(실측 확인 - 사용자
    요청으로 분리). 화면(키오스크) 반영은 호출부(main.py)가
    enqueue_kiosk_screen_job으로 큐에 넣어 백그라운드 스케줄러가 뒤늦게
    처리하게 한다 - 몇십 초~몇 분 정도 늦게 매장에 반영돼도 무방하다는
    사용자 확인."""
    update_result = update_menu_item(login_id, login_pw, barcode, menu_name, sale_price, store_id=store_id)
    if not update_result.get("not_found"):
        return update_result
    return register_menu_item(
        login_id, login_pw, barcode=barcode, menu_name=menu_name, sale_price=sale_price,
        class_cd=class_cd, store_id=store_id, class_name=class_name,
    )


def register_or_update_menu_item_with_retry(
    login_id: str, login_pw: str, barcode: str, menu_name: str, sale_price: int, class_cd: str,
    store_id: str | None = None, class_name: str | None = None,
) -> dict:
    """register_menu_item_with_retry와 같은 이유로 register_or_update_menu_item을
    한 번 더 시도한다(자원 경합으로 인한 예외만 재시도 - 정상 반환된
    {"ok": False, ...}는 그대로 전달)."""
    last_error: Exception | None = None
    for attempt in range(1, REGISTER_MAX_ATTEMPTS + 1):
        try:
            return register_or_update_menu_item(
                login_id, login_pw, barcode=barcode, menu_name=menu_name, sale_price=sale_price,
                class_cd=class_cd, store_id=store_id, class_name=class_name,
            )
        except Exception as e:
            last_error = e
            if attempt < REGISTER_MAX_ATTEMPTS:
                time.sleep(REGISTER_RETRY_DELAY_SECONDS)
    raise last_error