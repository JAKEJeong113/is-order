# orderqueen_bot.py

import os
import time
from datetime import date
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
    store_id: str | None = None,
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

            def _dismiss_popups() -> None:
                """jQuery UI 팝업(예: "사용자 정보" - 비밀번호 3개월 경과 안내,
                공지사항 등)이 오버레이와 함께 뜨는 경우가 있어 "등록" 버튼
                클릭을 가로막는다(실측 확인). 같은 클래스(.ui-dialog-titlebar-close)의
                닫기 버튼이 숨겨진 다른 팝업 것까지 여러 개 있어서 .first로
                찾으면 엉뚱한(안 보이는) 걸 클릭하게 되므로, jQuery UI 표준
                닫기 방법인 Escape 키를 먼저 쓴다 - 어떤 팝업이 열려있든
                범용으로 통한다."""
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
                # 공지사항류 배너(예: "오늘 하루동안 보지 않기 닫기")는 jQuery
                # UI dialog가 아니라 그냥 텍스트가 "닫기"인 버튼/링크일 수
                # 있어(실측 확인 - Escape만으론 안 닫히고 화면에 남아
                # 저장 버튼 클릭까지 먹통으로 만듦) 별도로도 시도한다.
                try:
                    generic_close = page.locator('button:has-text("닫기"):visible, a:has-text("닫기"):visible')
                    if generic_close.count() > 0:
                        generic_close.first.click(timeout=3000)
                        page.wait_for_timeout(300)
                except Exception:
                    pass

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
                _dismiss_popups()
                page.locator('button:has-text("등록"), a:has-text("등록")').first.click(force=True)
                try:
                    form.locator("#barcodeNo").first.wait_for(state="visible", timeout=6000)
                    modal_opened = True
                    break
                except PWTimeoutError:
                    continue
            if not modal_opened:
                return {"ok": False, "message": "등록 화면을 열지 못했습니다. 잠시 후 다시 시도해주세요."}

            form.locator("#classCd").first.select_option(class_cd)
            # menuNm(짧은 이름)은 POS 화면 표시용이라 길이 제한이 있을 수 있어
            # 안전하게 40자로 자르고, menuFullNm(전체 상품명)에는 원본을 그대로 둔다.
            form.locator("#menuNm").first.fill(menu_name[:40])
            form.locator("#menuFullNm").first.fill(menu_name)
            form.locator("#salePrice").first.fill(str(int(sale_price)))
            form.locator("#barcodeNo").first.fill(barcode)

            # 필드를 채우는 동안(특히 select_option의 change 이벤트 등으로)
            # 새 팝업이 뜰 수 있어 저장 직전에 한 번 더 정리한다.
            _dismiss_popups()

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
            _dismiss_popups()
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
            verified = barcode in list_text and menu_name[:40] in list_text

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