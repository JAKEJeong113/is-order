# cafe24_bot.py
"""카페24(Cafe24) 플랫폼 공통 봇: 무마켓(moomarket)에서 사용."""
import os
import re
from pathlib import Path

from playwright.sync_api import Page, sync_playwright, TimeoutError as PWTimeoutError

import browser_limit
import vendors

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR))
DEBUG_SCREENSHOT_PATH = DATA_DIR / "debug_cafe24_cart_failure.png"


def login_cafe24(page: Page, base_url: str, login_id: str, login_pwd: str) -> None:
    """이 함수는 항상 "새로 로그인해야 하는 상황"에만 불린다. context에 예전
    쿠키가 남아있으면 login.html이 "이미 로그인됨"으로 오판해 다른 페이지로
    리다이렉트시켜 로그인 폼 자체가 없을 수 있어(야미몰에서 같은 문제를 겪음),
    항상 쿠키를 비우고 새로 시작한다."""
    page.context.clear_cookies()
    page.goto(f"{base_url}/member/login.html", wait_until="domcontentloaded", timeout=30000)

    try:
        # 이 테마의 로그인 폼은 fw-filter 기반 자체 검증 스크립트가 붙어있는데,
        # fill()로 값을 한 번에 채우면 이 검증이 "입력됨"으로 감지 못해 버튼을
        # 눌러도 조용히 아무 반응이 없는 문제가 실사용에서 있었다(에러 메시지도
        # 없이 로그인 폼에 그대로 남아있었음). 실제 타이핑처럼 한 글자씩 입력해서
        # 키 이벤트 기반 검증도 확실히 걸리게 한다.
        page.locator("#member_id").click()
        page.locator("#member_id").press_sequentially(login_id, delay=30)
        page.locator("#member_passwd").click()
        page.locator("#member_passwd").press_sequentially(login_pwd, delay=30)
    except PWTimeoutError:
        try:
            page.screenshot(path=str(DEBUG_SCREENSHOT_PATH))
        except Exception:
            pass
        raise RuntimeError(f"로그인 폼을 찾지 못함 (실제 도착 URL: {page.url})")

    # 카페24는 <button type=submit>이 아니라 onclick="MemberAction.login(...)"을 쓰는
    # <a class="btnLogin"> 링크로 로그인을 제출한다 (form id는 요청마다 랜덤하게 바뀜).
    login_btn = page.locator("a.btnLogin").first
    try:
        login_btn.click(timeout=5000, force=True)
    except Exception:
        login_btn.dispatch_event("click")

    # 로그인 처리 중 /exec/front/Member/login/ 같은 중간 처리 URL을 거쳤다가 최종
    # 목적지로 넘어가는 경우가 있어서(실사용 확인), 리다이렉트가 다 끝날 때까지
    # networkidle로 기다린 뒤에 최종 URL로 성공/실패를 판단한다.
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except PWTimeoutError:
        pass
    page.wait_for_timeout(1000)

    if "login" in page.url.lower():
        error_text = ""
        try:
            error_text = page.locator("body").inner_text(timeout=2000)[:300]
        except Exception:
            pass
        try:
            page.screenshot(path=str(DEBUG_SCREENSHOT_PATH))
        except Exception:
            pass
        raise RuntimeError(f"카페24 로그인 실패 (아이디/비밀번호를 확인해주세요) / URL: {page.url} / 화면: {error_text}")


def _parse_price(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def _extract_unit_qty(text: str) -> int | None:
    if not text:
        return None
    match = re.search(r"[xX×*]\s*(\d+)\s*개|(\d+)\s*개입", text)
    if match:
        return int(match.group(1) or match.group(2))
    match = re.search(r"\((\d+)\s*입\)", text)
    if match:
        return int(match.group(1))
    return None


def fetch_unit_qty_from_detail_page(base_url: str, login_id: str, login_pwd: str, product_url: str) -> int | None:
    """목록 페이지 상품명에는 구매 단위가 거의 안 보이는 상품(무마켓 대부분)도,
    상세페이지에는 "총 상품금액(수량) : 13,800원 (40개)"처럼 실제 구매 단위
    개수가 그대로 계산돼서 나온다("구매단위 40EA"/"주문단위 100매" 같은
    라벨은 상품 종류마다 표기가 달라 직접 파싱하기 불안정해서, 항상 같은
    형식으로 뜨는 이 요약 문구를 쓴다). 카탈로그 전체를 상세페이지까지
    크롤링하면 상품 수천 개 × 페이지 이동이라 너무 느리고 타임아웃 위험도
    커서, 실제 발주 리포트에 뜬 상품만 그때그때 이 함수로 보충하고 결과를
    캐시에 저장해둔다(store_reports.py에서 호출)."""
    with browser_limit.browser_semaphore, sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-setuid-sandbox"],
        )
        try:
            context = browser.new_context()
            page = context.new_page()
            login_cafe24(page, base_url, login_id, login_pwd)
            page.goto(product_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(500)
            body_text = page.inner_text("body")
        except Exception as e:
            print(f"[CAFE24] {product_url} 상세페이지 조회 실패:", e)
            return None
        finally:
            browser.close()

    match = re.search(r"총\s*상품금액\(수량\)\s*:\s*[\d,]+\s*원\s*\((\d+)\s*개\)", body_text)
    if not match:
        return None
    qty = int(match.group(1))
    return qty if qty > 0 else None


def _extract_list_page_items(page: Page, base_url: str) -> list[dict]:
    """상품이 페이지당 최대 80개까지 있어서, 아이템마다 Playwright 왕복 호출을
    여러 번 하면(수십 페이지 누적 시) 크롤링이 비정상적으로 느려진다. 페이지당
    한 번의 evaluate로 필요한 값을 한꺼번에 뽑아온다."""
    raw_items = page.evaluate("""
        () => Array.from(document.querySelectorAll('ul.prdList li')).map(li => {
            const link = li.querySelector('div.thumbnail a');
            const img = li.querySelector('div.thumbnail img');
            const desc = li.querySelector('div.description');
            return {
                href: link ? link.getAttribute('href') : null,
                name: img ? img.getAttribute('alt') : '',
                descText: desc ? desc.innerText : '',
            };
        })
    """)

    results = []
    for raw in raw_items:
        href = raw.get("href") or ""
        name = (raw.get("name") or "").strip()

        no_match = re.search(r"/product/[^/]+/(\d+)/", href)
        product_no = no_match.group(1) if no_match else None
        if not name or not product_no:
            continue

        # 가격 표시 위치가 테마마다 조금씩 달라서 상품명 옆 텍스트 전체에서 숫자+원을 찾는다
        price = None
        price_match = re.search(r"([\d,]+)\s*원", raw.get("descText") or "")
        if price_match:
            price = _parse_price(price_match.group(1))

        results.append({
            "name": name,
            "price": price,
            "unit_qty": _extract_unit_qty(name),
            "product_url": f"{base_url}{href}" if href.startswith("/") else href,
            "goods_no": product_no,
        })

    return results


def _block_heavy_resources(page: Page) -> None:
    """크롤링은 텍스트/가격만 필요하므로 이미지·폰트·미디어를 차단해 메모리 사용을 줄인다."""
    page.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in ("image", "media", "font")
        else route.continue_(),
    )


def crawl_full_catalog(
    base_url: str, login_id: str, login_pwd: str, category_code: str, max_pages: int = 100,
) -> list[dict]:
    """'전체상품' 카테고리(cate_no)를 끝까지 페이지를 넘기며 전부 수집한다."""
    all_products: dict[str, dict] = {}

    with browser_limit.browser_semaphore, sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-setuid-sandbox"],
        )
        # 페이지 하나로 수십~80페이지를 계속 이동하면 브라우저 메모리가 쌓여서 Render
        # 인스턴스가 OOM으로 재시작되는 문제가 있었다. 로그인 세션은 context 단위로
        # 유지되니, 일정 페이지마다 페이지를 새로 만들어 메모리를 정리한다.
        context = browser.new_context()
        page = context.new_page()
        _block_heavy_resources(page)
        PAGE_RECYCLE_INTERVAL = 10

        try:
            login_cafe24(page, base_url, login_id, login_pwd)

            # 정렬 기준이 실시간 인기도 등으로 안정적이지 않으면, 크롤링 도중 상품
            # 순서가 바뀌어 인접한 두 페이지가 우연히 같은 상품들을 보여줄 수 있다
            # (고도몰 계열에서 이 때문에 일부 상품이 통째로 누락되던 문제가 실제로
            # 확인됨). 새 상품이 없는 페이지를 한 번 만났다고 바로 멈추면 그 뒤에
            # 있는 진짜 새 상품을 놓칠 수 있어서, 연속으로 여러 번 없을 때만 멈춘다.
            consecutive_empty_pages = 0
            for page_no in range(1, max_pages + 1):
                try:
                    page.goto(
                        f"{base_url}/product/list.html?cate_no={category_code}&page={page_no}",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    page.wait_for_timeout(600)
                except Exception as e:
                    # 페이지 하나가 타임아웃나도 여기서 예외가 그대로 올라가면
                    # crawl_vendor의 try/except에 걸려서 여태 모은 상품이 전부
                    # 버려지고(replace_vendor_catalog가 아예 안 불림) 오래된 캐시가
                    # 그대로 남는 사고가 있었다(실측: 무마켓이 8페이지에서 이걸로
                    # 매번 실패). 여기서 그만 보고, 지금까지 모은 건 그대로 살린다.
                    print(f"[CAFE24] {base_url} 페이지 {page_no} 로딩 실패:", e)
                    break

                items = _extract_list_page_items(page, base_url)
                if not items:
                    break

                new_count = 0
                for it in items:
                    key = it["goods_no"] or it["name"]
                    if key not in all_products:
                        all_products[key] = it
                        new_count += 1

                if new_count == 0:
                    consecutive_empty_pages += 1
                    if consecutive_empty_pages >= 3:
                        break
                else:
                    consecutive_empty_pages = 0

                if page_no % PAGE_RECYCLE_INTERVAL == 0:
                    page.close()
                    page = context.new_page()
                    _block_heavy_resources(page)
        finally:
            browser.close()

    return list(all_products.values())


def add_to_cart(store_id: str, base_url: str, login_id: str, login_pwd: str, product_url: str, qty: int = 1) -> dict:
    """상품 상세페이지에서 실제로 장바구니에 담는다. 지점별 로그인 세션(쿠키)을
    캐시해서, 저장된 세션이 있으면 로그인 과정을 건너뛴다. 캐시된 세션이 만료됐으면
    (담기 버튼을 못 찾으면) 새로 로그인해서 한 번 더 시도한다."""
    with browser_limit.browser_semaphore, sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-setuid-sandbox"],
        )
        cached_state = vendors.get_session_state(store_id, "moomarket")
        context = browser.new_context(storage_state=cached_state) if cached_state else browser.new_context()
        page = context.new_page()

        # 야미몰에서 클릭 성공/alert만 믿었다가 실제로는 하나도 안 담기는 문제를
        # 겪은 뒤로, 여기서도 처음부터 alert 캡처 + 실제 장바구니 개수 확인을 같이 한다.
        alert_messages: list[str] = []

        def _on_dialog(dialog):
            alert_messages.append(dialog.message)
            # "장바구니에 동일한 상품이 있습니다. 추가하시겠습니까?" confirm은
            # 거절(dismiss)하면 실제로는 아무 것도 안 담긴 채로 넘어가서(기존
            # 재테스트로 남아있던 상품과 겹칠 때 실사용에서 재현됨), 사용자 의도대로
            # 수락(accept)해서 실제로 담기게 한다. 그 외(로그인 필요 등 진짜 오류성
            # 알림)는 그대로 거절한다.
            if "동일한 상품" in dialog.message or "이미 장바구니" in dialog.message:
                dialog.accept()
            else:
                dialog.dismiss()

        page.on("dialog", _on_dialog)

        def _read_cart_count() -> int:
            try:
                text = page.locator(".EC-Layout-Basket-count").first.inner_text(timeout=2000)
                digits = re.sub(r"[^\d]", "", text or "")
                return int(digits) if digits else 0
            except Exception:
                return -1

        def _try_add() -> str:
            alert_messages.clear()
            page.goto(product_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(800)

            before_count = _read_cart_count()

            # 무마켓은 다른 도매처와 달리 수량 입력칸(#quantity, name="quantity_opt[]")이
            # readonly라 값을 직접 채울 수 없고, 기본값이 이미 "구매단위"(예: 26EA)
            # 1세트로 채워져 있다. qty는 "몇 세트를 담을지"를 의미하므로, qty>1이면
            # +버튼(.QuantityUp)을 (qty-1)번 눌러서 세트 수만큼 늘린다(클릭 한 번 =
            # 구매단위만큼 증가하는 사이트 자체 동작). 예전엔 이름이 다른 셀렉터를
            # 써서 이 필드를 아예 못 찾아 항상 최소 1세트만 담기고 있었다.
            if qty > 1:
                up_btn = page.locator("a.QuantityUp, a.up").first
                if up_btn.count() > 0:
                    for _ in range(qty - 1):
                        try:
                            up_btn.click(timeout=1000)
                            page.wait_for_timeout(150)
                        except Exception:
                            break

            # 헤더에 항상 떠 있는 "장바구니로 이동" 링크(<a href="/order/basket.html">)도
            # 텍스트가 "장바구니"라 넓은 텍스트 검색으로는 이게 먼저 잡혀서 실제로는
            # 아무것도 안 담고 장바구니 페이지로만 이동해버리는 문제가 있었다(실사용
            # 확인). 실제 담기 버튼의 표준 클래스(.btn-basket)를 먼저 찾고, 그게 없을
            # 때만 헤더 링크를 제외한 텍스트 검색으로 넓힌다.
            cart_btn = page.locator("a.btn-basket, button.btn-basket, #BtnBasket, a.btnBasket").first
            if cart_btn.count() == 0:
                cart_btn = page.locator(
                    "a:has-text('장바구니'):not([href='/order/basket.html']), "
                    "button:has-text('장바구니')"
                ).first
            if cart_btn.count() == 0:
                try:
                    page.screenshot(path=str(DEBUG_SCREENSHOT_PATH))
                except Exception:
                    pass
                return "no_cart_button"

            try:
                cart_btn.click(timeout=5000)
            except Exception:
                pass
            page.wait_for_timeout(1500)

            if "login" in page.url.lower() or any("로그인" in m for m in alert_messages):
                return "login_required"

            # 확인 팝업(계속쇼핑/장바구니로 이동 등)이 있으면 닫기
            confirm_btn = page.locator(
                "button:has-text('확인'), a:has-text('확인'), button:has-text('닫기')"
            ).first
            if confirm_btn.count() > 0:
                try:
                    confirm_btn.click(timeout=2000)
                except Exception:
                    pass
                page.wait_for_timeout(500)

            # 클릭 성공/alert 없음만으로는 실제로 담겼는지 신뢰할 수 없다는 게 야미몰
            # 사례로 확인됐다. 새로고침 후 실제 장바구니 개수 배지로 최종 확인한다.
            try:
                page.reload(wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            after_count = _read_cart_count()

            if before_count >= 0 and after_count >= 0 and after_count <= before_count:
                if alert_messages:
                    return f"blocked:{alert_messages[-1]}"
                return "not_added"

            return "ok"

        try:
            logged_in_fresh = False
            if not cached_state:
                login_cafe24(page, base_url, login_id, login_pwd)
                logged_in_fresh = True

            outcome = _try_add()
            if outcome in ("no_cart_button", "login_required", "not_added") and cached_state:
                # 캐시된 세션이 만료됐을 수 있으니 새로 로그인해서 한 번 더 시도
                login_cafe24(page, base_url, login_id, login_pwd)
                logged_in_fresh = True
                outcome = _try_add()

            if outcome == "no_cart_button":
                return {"ok": False, "reason": "장바구니 버튼을 찾지 못함"}
            if outcome == "login_required":
                return {"ok": False, "reason": "로그인이 필요합니다 (아이디/비밀번호를 확인해주세요)"}
            if outcome == "not_added":
                return {"ok": False, "reason": "담기를 시도했지만 장바구니 수량이 늘지 않음 (재고/최소수량 등 확인 필요)"}
            if outcome.startswith("blocked:"):
                return {"ok": False, "reason": outcome[len("blocked:"):]}

            if logged_in_fresh:
                vendors.save_session_state(store_id, "moomarket", context.storage_state())

            return {"ok": True, "qty": qty}
        except Exception as e:
            return {"ok": False, "reason": str(e)}
        finally:
            browser.close()
