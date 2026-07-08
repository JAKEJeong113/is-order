import json
import math
import os
import re
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

import vendors


YAMIMALL_URL = "https://xn--352blx12s.com"

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR))
DEBUG_LOGIN_SCREENSHOT_PATH = DATA_DIR / "debug_yamimall_login_failure.png"


def run_yamimall_search(page, keyword: str, base_url: str = YAMIMALL_URL) -> None:
    """검색창 버튼(#sch_submit)은 아이콘폰트라 실제 클릭이 안 먹는 경우가 있어
    검색 폼이 실제로 이동하는 GET URL로 직접 이동한다."""
    page.goto(
        f"{base_url}/shop/search.php?skey={quote(keyword)}",
        wait_until="domcontentloaded",
        timeout=30000,
    )
    page.wait_for_timeout(1500)


def close_yamimall_popups(page):
    """
    야미몰 공지 팝업 닫기.
    여러 개의 공지 팝업이 떠도 처리함.
    가능하면 '24시간동안 열람안함'을 먼저 눌러 다음 접속 팝업을 줄임.
    """

    try:
        reject_buttons = page.locator(".hd_pops_reject")
        count = reject_buttons.count()

        for i in range(count):
            try:
                reject_buttons.nth(i).click(timeout=1000)
                page.wait_for_timeout(200)
            except Exception:
                pass
    except Exception:
        pass

    try:
        close_buttons = page.locator(".hd_pops_close")
        count = close_buttons.count()

        for i in range(count):
            try:
                close_buttons.nth(i).click(timeout=1000)
                page.wait_for_timeout(200)
            except Exception:
                pass
    except Exception:
        pass


def extract_wholesale_unit_qty(text: str) -> int | None:
    """
    예:
    (묶음) 45g X 8개 [1박스16개] -> 8
    75g x 6개 -> 6
    100g × 10개 -> 10
    """
    if not text:
        return None

    match = re.search(r"[xX×]\s*(\d+)\s*개", text)
    if not match:
        return None

    return int(match.group(1))

def normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def keyword_match_score(keyword: str, product_text: str) -> int:
    if not keyword or not product_text:
        return 0

    product_norm = normalize_text(product_text)
    words = [w for w in keyword.split() if w]

    score = 0
    for word in words:
        if normalize_text(word) in product_norm:
            score += 1

    return score

def find_best_yamimall_product(page, keyword: str, max_pages: int = 5):
    best_product = None
    best_text = ""
    best_score = -1
    best_index = -1
    best_page = 1

    for page_no in range(1, max_pages + 1):
        page.wait_for_timeout(1000)

        product_links = page.locator("a[href*='/shop/item.php?code=']")
        count = product_links.count()

        print(f"[YAMIMALL] search page {page_no}, product link count =", count)

        for i in range(count):
            link = product_links.nth(i)
            text = link.evaluate("(el) => el.innerText || el.textContent || ''").strip()

            print(f"[YAMIMALL] page {page_no} candidate {i} =", repr(text))

            if not text:
                continue

            if "SOLD OUT" in text.upper() or "품절" in text:
                continue

            unit_qty_temp = extract_wholesale_unit_qty(text)
            if not unit_qty_temp:
                continue

            score = keyword_match_score(keyword, text)

            print(f"[YAMIMALL] page {page_no} candidate {i} score =", score)

            if score > best_score:
                best_score = score
                best_product = link
                best_text = text
                best_index = i
                best_page = page_no

        

        # 다음 페이지 버튼 클릭
        # 다음 페이지 버튼 클릭
        next_page_no = page_no + 1
        next_page_link = None

        links = page.locator("a")
        link_count = links.count()

        for j in range(link_count):
            link = links.nth(j)

            try:
                link_text = link.inner_text(timeout=500).strip()
                href = link.get_attribute("href") or ""

                # '20개' 같은 상품 텍스트가 아니라, 정확히 페이지 번호 '2'만 잡기
                if link_text == str(next_page_no) and ("page=" in href or "search.php" in href):
                    next_page_link = link
                    break
            except Exception:
                continue

        if next_page_link is None:
            print(f"[YAMIMALL] page {next_page_no} link not found")
            break

        try:
            next_page_link.click(timeout=3000, force=True)
        except Exception:
            next_page_link.dispatch_event("click")

        page.wait_for_timeout(3000)
        print(f"[YAMIMALL] moved to page {next_page_no}, url =", page.url)

    return best_product, best_text, best_score, best_index, best_page

def find_top_yamimall_products(page, keyword: str, top_n: int = 3, max_pages: int = 1):
    """가격비교용: 검색어 일치도 상위 top_n개 후보(이름/가격/1타수량)를 반환. (장바구니 담지 않음)"""
    scored = []

    for page_no in range(1, max_pages + 1):
        page.wait_for_timeout(1000)

        product_links = page.locator("a[href*='/shop/item.php?code=']")
        count = product_links.count()

        for i in range(count):
            link = product_links.nth(i)
            try:
                text = link.evaluate("(el) => el.innerText || el.textContent || ''").strip()
            except Exception:
                continue

            if not text or "SOLD OUT" in text.upper() or "품절" in text:
                continue

            unit_qty = extract_wholesale_unit_qty(text)
            if not unit_qty:
                continue

            score = keyword_match_score(keyword, text)
            if score <= 0:
                continue

            try:
                container = link.locator("xpath=ancestor::li[1]")
                price_input = container.locator("input[name='ct_price']")
                price = None
                if price_input.count() > 0:
                    price = _parse_price(price_input.first.get_attribute("value") or "")
                href = link.get_attribute("href")
            except Exception:
                price = None
                href = None

            scored.append({
                "name": text,
                "price": price,
                "unit_qty": unit_qty,
                "product_url": href,
                "score": score,
            })

    deduped = {}
    for r in scored:
        key = (r["name"], r["price"])
        if key not in deduped:
            deduped[key] = r

    result = sorted(deduped.values(), key=lambda r: r["score"], reverse=True)
    return result[:top_n]


# "전체상품"(001000000)은 이름과 달리 일부(추천/인기)만 보여주는 화면이라
# 실제 전체 수집을 위해서는 카테고리별 "전체보기" 코드를 각각 순회해야 한다.
# list.php는 포트를 명시한 URL(:443)로 접속해야 카테고리 필터가 정상 적용된다.
FULL_CATALOG_CATEGORY_CODES = [
    "001001001",  # 국산과자
    "001002002",  # 세계과자
    "001003001",  # 젤리/마쉬멜로
    "001004001",  # 초콜릿
    "001005001",  # 사탕/껌
    "001006001",  # 안주
    "001007001",  # 식자재
    "001008001",  # 음료
    "001011001",  # 중국식품
    "001012001",  # 문구완구
    "001013001",  # 기타
    "001014001",  # 라면
]


def _extract_list_page_items(page) -> list[dict]:
    product_links = page.locator("a[href*='/shop/item.php?code=']")
    count = product_links.count()
    items = []

    for i in range(count):
        link = product_links.nth(i)
        try:
            text = link.evaluate("(el) => el.innerText || el.textContent || ''").strip()
        except Exception:
            continue

        if not text or "SOLD OUT" in text.upper() or "품절" in text:
            continue

        unit_qty = extract_wholesale_unit_qty(text)

        try:
            container = link.locator("xpath=ancestor::li[1]")
            price_input = container.locator("input[name='ct_price']")
            price = None
            if price_input.count() > 0:
                price = _parse_price(price_input.first.get_attribute("value") or "")
            href = link.get_attribute("href")
        except Exception:
            price = None
            href = None

        items.append({
            "name": text,
            "price": price,
            "unit_qty": unit_qty,
            "product_url": href,
            "goods_no": None,
            "_key": href or text,
        })

    return items


def _block_heavy_resources(page) -> None:
    """크롤링은 텍스트/가격만 필요하므로 이미지·폰트·미디어를 차단해 메모리 사용을 줄인다."""
    page.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in ("image", "media", "font")
        else route.continue_(),
    )


def crawl_full_catalog(
    username: str, password: str, base_url: str = YAMIMALL_URL,
    category_codes: list[str] | None = None, max_pages: int = 60,
) -> list[dict]:
    """카테고리별 '전체보기' 코드를 모두 순회해서 전체 상품을 수집한다.
    같은 플랫폼을 쓰는 다른 스토어(또요몰 등)를 위해 base_url/category_codes를
    바꿔 넣을 수 있게 했다 (기본값은 기존 야미몰 동작 그대로).
    카테고리 하나에도 페이지가 여러 장일 수 있어(또요몰 젤리 카테고리 하나가 11페이지+)
    각 카테고리마다 새 상품이 없어질 때까지 페이지를 끝까지 넘긴다."""
    codes = category_codes if category_codes is not None else FULL_CATALOG_CATEGORY_CODES
    products: dict[str, dict] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-setuid-sandbox"],
        )
        # 페이지 하나로 카테고리를 전부(수십~수백 페이지 이동) 돌면 브라우저 메모리가
        # 계속 쌓여서 Render 인스턴스가 OOM으로 재시작되는 문제가 있었다. 로그인 세션은
        # context 단위로 유지되니, 카테고리마다 페이지를 새로 만들어 메모리를 정리한다.
        context = browser.new_context()
        page = context.new_page()
        _block_heavy_resources(page)

        try:
            login_yamimall(page, username, password, base_url=base_url)

            for code in codes:
                for page_no in range(1, max_pages + 1):
                    try:
                        page.goto(
                            f"{base_url}:443/shop/list.php?code={code}&page={page_no}",
                            wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        page.wait_for_timeout(1200)
                    except Exception as e:
                        print(f"[YAMIMALL] 카테고리 {code} 페이지 {page_no} 로딩 실패:", e)
                        break

                    items = _extract_list_page_items(page)
                    if not items:
                        break

                    new_count = 0
                    for item in items:
                        key = item.pop("_key")
                        if key not in products:
                            products[key] = item
                            new_count += 1

                    # 더 이상 새로운 상품이 없으면(마지막 페이지가 반복되는 경우) 다음 카테고리로
                    if new_count == 0:
                        break

                page.close()
                page = context.new_page()
                _block_heavy_resources(page)
        finally:
            browser.close()

    return list(products.values())


def calc_yamimall_cart_qty(sold_qty: int, unit_qty: int) -> int:
    """
    판매수량 / 1타수량을 50% 기준 반올림.
    판매가 있었는데 계산 결과가 0이면 최소 1타.
    """
    if sold_qty <= 0 or unit_qty <= 0:
        return 0

    cart_qty = math.floor((sold_qty / unit_qty) + 0.5)

    if sold_qty > 0 and cart_qty == 0:
        cart_qty = 1

    return cart_qty


def login_yamimall(page, username: str, password: str, base_url: str = YAMIMALL_URL) -> None:
    """홈페이지에서 로그인 버튼(트리거)을 클릭해 모달/페이지 전환을 유도하는 대신
    로그인 폼이 있는 URL로 바로 이동한다. 홈페이지의 공지 팝업이 트리거를 가려서
    클릭이 기본 타임아웃(30초)까지 멈추는 문제가 실사용에서 재현됐는데, 트리거
    클릭 자체를 없애서 근본적으로 피한다.
    이 함수는 항상 "새로 로그인해야 하는 상황"에만 불린다(캐시된 세션이 없거나
    유효하지 않다고 판단된 경우). context에 남아있는 예전 쿠키가 있으면
    login.php가 "이미 로그인됨"으로 오판해 다른 페이지로 리다이렉트시킬 수 있어,
    항상 쿠키를 비우고 완전히 새로 시작한다."""
    page.context.clear_cookies()
    page.goto(f"{base_url}/shop/login.php", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1000)

    try:
        page.fill("#login_id", username)
        page.fill("#login_pw", password)
    except PlaywrightTimeoutError:
        try:
            page.screenshot(path=str(DEBUG_LOGIN_SCREENSHOT_PATH))
        except Exception:
            pass
        raise RuntimeError(f"로그인 폼을 찾지 못함 (실제 도착 URL: {page.url})")

    page.locator(".login_btn").click()
    page.wait_for_timeout(1000)

    close_yamimall_popups(page)


def fetch_candidates(username: str, password: str, keyword: str, top_n: int = 3) -> list[dict]:
    """검색어 일치도 상위 top_n개 후보(이름/가격/1타수량)를 조회 (장바구니 담지 않음)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-setuid-sandbox"],
        )
        page = browser.new_page()

        try:
            login_yamimall(page, username, password)
            run_yamimall_search(page, keyword)

            return find_top_yamimall_products(page, keyword, top_n=top_n, max_pages=4)
        finally:
            browser.close()


def _parse_price(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def add_to_cart(store_id: str, username: str, password: str, product_url: str, qty: int = 1, keyword: str | None = None) -> dict:
    """상품 상세페이지에서 실제로 장바구니에 담는다. product_url은 item.php?code=... 형태.
    지점별 로그인 세션(쿠키)을 캐시해서, 저장된 세션이 있으면 로그인 과정을 건너뛴다.
    캐시된 세션이 만료됐으면(담기 버튼을 못 찾으면) 새로 로그인해서 한 번 더 시도한다.
    일부 상품(예: 맛/색상별로 카드가 나뉘어 있는 상품)은 상세페이지에서 담기가 막혀있고
    목록/검색결과의 인라인 담기 버튼으로만 담아지는 게 실사용으로 확인돼서, keyword가 있으면
    그 경우 add_to_cart_via_list로 자동 재시도한다."""
    code_match = re.search(r"code=(\d+)", product_url or "")
    if not code_match:
        return {"ok": False, "reason": f"상품 코드 추출 실패: {product_url}"}
    item_code = code_match.group(1)

    # Playwright sync API는 같은 스레드에서 중첩(nested) 사용이 안 된다. 그래서
    # item.php 방식이 막힌 상품을 목록 방식(add_to_cart_via_list)으로 재시도할 때는
    # 여기서 바로 호출하지 않고, 아래 with 블록이 완전히 끝난 뒤에 호출한다.
    needs_list_fallback = False
    result: dict = {"ok": False, "reason": "알 수 없는 오류"}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-setuid-sandbox"],
        )
        cached_state = vendors.get_session_state(store_id, "yamimall")
        context = browser.new_context(storage_state=cached_state) if cached_state else browser.new_context()
        page = context.new_page()

        # 캐시된 세션이 만료된 채로 담기를 누르면 "로그인이 필요합니다" 같은
        # 네이티브 alert가 뜨고 로그인 페이지로 튕긴다. 담기 버튼 자체는 비로그인
        # 상태에서도 페이지에 존재해서 클릭 자체는 "성공"하기 때문에, 이 alert를
        # 잡아두지 않으면 실제로는 실패했는데 성공으로 잘못 보고하게 된다.
        alert_messages: list[str] = []

        def _on_dialog(dialog):
            alert_messages.append(dialog.message)
            dialog.dismiss()

        page.on("dialog", _on_dialog)

        def _read_cart_count() -> int:
            """헤더의 실시간 장바구니 개수 배지(.cart_prod_cnt_class)를 읽는다.
            못 읽으면 -1(판단 불가)을 반환해서, 클릭 성공 여부만으로 함부로
            성공/실패를 단정하지 않게 한다."""
            try:
                text = page.locator(".cart_prod_cnt_class").first.inner_text(timeout=2000)
                digits = re.sub(r"[^\d]", "", text or "")
                return int(digits) if digits else 0
            except Exception:
                return -1

        def _try_add() -> str:
            alert_messages.clear()
            page.goto(
                f"{YAMIMALL_URL}:443/shop/item.php?code={item_code}",
                wait_until="domcontentloaded",
                timeout=30000,
            )

            before_count = _read_cart_count()

            # 수량 1개는 +버튼을 누를 필요가 없다. 수량 조절 UI(.add_qty_class)가
            # 지연된 AJAX로 늦게 뜨거나, 상품에 따라 아예 없는 경우(고정 수량 등)도
            # 있어서 실제로 필요할 때(qty>1)만 기다리고 요구한다.
            if qty > 1:
                plus_button = page.locator(".add_qty_class").first
                try:
                    plus_button.wait_for(state="attached", timeout=8000)
                except PlaywrightTimeoutError:
                    return "no_qty_button"

                for _ in range(qty - 1):
                    plus_button.click(timeout=1000)
                    page.wait_for_timeout(150)

            cart_btn = page.locator("#sit_btn_cart")
            if cart_btn.count() == 0:
                return "no_cart_button"

            try:
                cart_btn.click(timeout=5000)
            except Exception:
                # alert 때문에 Playwright가 "navigation 대기"에서 타임아웃날 수
                # 있는데, 그래도 클릭 자체는 이미 서버로 전달됐을 수 있어 여기서
                # 바로 실패 처리하지 않고 아래 실제 확인 단계로 넘어간다.
                pass
            page.wait_for_timeout(1500)

            if "login.php" in page.url or any("로그인" in m for m in alert_messages):
                return "login_required"

            # 담기 확인 팝업 처리 (있으면 닫기)
            dialog_btn = page.locator(".ui-dialog-buttonpane button").first
            if dialog_btn.count() > 0:
                try:
                    dialog_btn.click(timeout=2000)
                except Exception:
                    pass
                page.wait_for_timeout(500)

            # alert/리다이렉트가 없어도 재고부족·최소수량 등 다른 사유로 조용히
            # 실패하는 경우가 있어서, 클릭 성공 여부만으로는 신뢰할 수 없다는 게
            # 실측으로 확인됐다. 새로고침 후 실제 장바구니 개수 배지로 최종 확인한다.
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
                login_yamimall(page, username, password)
                logged_in_fresh = True

            outcome = _try_add()
            if outcome in ("no_cart_button", "login_required", "not_added") and cached_state:
                # 캐시된 세션이 만료됐을 수 있으니 새로 로그인해서 한 번 더 시도
                login_yamimall(page, username, password)
                logged_in_fresh = True
                outcome = _try_add()

            if outcome == "no_qty_button":
                result = {"ok": False, "reason": "수량 조절 버튼을 찾지 못함 (품절이거나 페이지 구조 변경)"}
            elif outcome == "no_cart_button":
                result = {"ok": False, "reason": "장바구니 버튼을 찾지 못함"}
            elif outcome == "login_required":
                result = {"ok": False, "reason": "로그인이 필요합니다 (아이디/비밀번호를 확인해주세요)"}
            elif outcome == "not_added" or outcome.startswith("blocked:"):
                if keyword:
                    needs_list_fallback = True
                else:
                    fallback_reason = (
                        outcome[len("blocked:"):] if outcome.startswith("blocked:")
                        else "담기를 시도했지만 장바구니 수량이 늘지 않음 (재고/최소수량 등 확인 필요)"
                    )
                    result = {"ok": False, "reason": fallback_reason}
            else:
                if logged_in_fresh:
                    vendors.save_session_state(store_id, "yamimall", context.storage_state())
                result = {"ok": True, "item_code": item_code, "qty": qty}
        except Exception as e:
            result = {"ok": False, "reason": str(e)}
        finally:
            browser.close()

    if needs_list_fallback:
        return add_to_cart_via_list(
            store_id, "yamimall", username, password, product_url, qty,
            base_url=YAMIMALL_URL, keyword=keyword,
        )

    return result


def add_to_cart_via_list(
    store_id: str,
    vendor_id: str,
    username: str,
    password: str,
    product_url: str,
    qty: int = 1,
    base_url: str = YAMIMALL_URL,
    keyword: str | None = None,
) -> dict:
    """일부 스토어(또요몰 등)는 상품 상세페이지(item.php)에 접속하면 봇 감지로
    페이지가 about:blank로 리다이렉트되는 문제가 있어, item.php 대신 검색 결과
    목록 화면에서 바로 담는 방식을 쓴다. product_url의 code= 값으로 목록에서
    정확한 상품 컨테이너를 찾아 그 안의 담기 버튼을 클릭한다."""
    code_match = re.search(r"code=(\d+)", product_url or "")
    if not code_match:
        return {"ok": False, "reason": f"상품 코드 추출 실패: {product_url}"}
    item_code = code_match.group(1)
    # item_name에는 상품명 아래에 "(타) 16g X 30개 [1박스6타]" 같은 포장단위
    # 설명이 줄바꿈으로 붙어 있을 수 있다(텔레그램 메시지 표시용으로는 유용하지만
    # 검색어로 그대로 쓰면 사이트 검색이 매칭되는 상품을 못 찾는다). 첫 줄(순수
    # 상품명)만 검색어로 쓴다.
    search_keyword = (keyword or item_code).splitlines()[0].strip()

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-setuid-sandbox"],
        )
        cached_state = vendors.get_session_state(store_id, vendor_id)
        context = browser.new_context(storage_state=cached_state) if cached_state else browser.new_context()
        page = context.new_page()

        alert_messages: list[str] = []

        def _on_dialog(dialog):
            alert_messages.append(dialog.message)
            dialog.dismiss()

        page.on("dialog", _on_dialog)

        def _read_cart_count() -> int:
            try:
                text = page.locator(".cart_prod_cnt_class").first.inner_text(timeout=2000)
                digits = re.sub(r"[^\d]", "", text or "")
                return int(digits) if digits else 0
            except Exception:
                return -1

        def _find_container():
            run_yamimall_search(page, search_keyword, base_url=base_url)
            # 검색 결과 렌더링이 느릴 수 있어(특히 로그인 직후) 고정 대기시간
            # 대신 상품 링크가 실제로 나타날 때까지 명시적으로 기다린다.
            try:
                page.wait_for_selector("a[href*='/shop/item.php?code=']", timeout=8000)
            except PlaywrightTimeoutError:
                pass
            links = page.locator(f"a[href*='code={item_code}']")
            if links.count() == 0:
                try:
                    # 파일명이 고정이면 동시에 실패하는 다른 상품/스토어 요청이 덮어써서
                    # 엉뚱한 화면을 보게 될 수 있어, 상품 코드를 파일명에 넣어 구분한다.
                    all_links = page.locator("a[href*='/shop/item.php?code=']")
                    all_count = all_links.count()
                    sample_names = []
                    for i in range(min(all_count, 10)):
                        try:
                            t = all_links.nth(i).evaluate("(el) => el.innerText || ''").strip()
                            if t:
                                sample_names.append(t.splitlines()[0][:60])
                        except Exception:
                            pass
                    summary = {
                        "page_url": page.url,
                        "search_keyword": search_keyword,
                        "target_item_code": item_code,
                        "total_item_links_on_page": all_count,
                        "sample_product_names": sample_names,
                    }
                    summary_path = DATA_DIR / f"debug_yamimall_search_not_found_{item_code}.json"
                    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception:
                    pass
                return None
            return links.first.locator("xpath=ancestor::li[1]")

        def _try_add() -> str:
            alert_messages.clear()
            container = _find_container()
            if container is None:
                return "not_found"

            before_count = _read_cart_count()

            # 이 목록 카드의 수량 스텝퍼(.qty_plus_class2)는 화면 크기가 0이라
            # Playwright의 일반 클릭/입력으로는 아예 상호작용이 안 된다(실측
            # 확인, force=True/dispatch_event도 값이 안 바뀜). 대신 수량 입력칸
            # (input[name='qty[]'])의 "기본값"을 그대로 "1세트당 개수"로 쓴다 -
            # 상품에 따라 1(=1타 단위로 담는 상품)이거나 12/24 같은 포장수량
            # (=낱개 단위로 담고 최소구매수량이 강제되는 상품)일 수 있는데, 어느
            # 쪽이든 "기본값 × qty"가 사용자가 의도한 "qty세트" 수량과 일치한다.
            # 이 값을 JS로 직접 넣고 change 이벤트를 발생시켜 반영한다(입력칸도
            # 화면상 안 보여서 fill()은 안 먹고 evaluate로 값만 바꿔야 한다).
            qty_field = container.locator("input[name='qty[]']").first
            qty_debug = {"qty_requested": qty}
            if qty_field.count() > 0 and qty > 1:
                try:
                    unit_qty = int(qty_field.input_value() or "1")
                except Exception:
                    unit_qty = 1
                target_qty = max(unit_qty, 1) * qty
                qty_debug["unit_qty_before"] = unit_qty
                qty_debug["target_qty"] = target_qty
                try:
                    qty_field.evaluate(
                        """
                        (el, val) => {
                            el.value = val;
                            el.dispatchEvent(new Event('input', { bubbles: true }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                        """,
                        str(target_qty),
                    )
                except Exception as e:
                    qty_debug["set_error"] = str(e)
                try:
                    qty_debug["qty_value_right_before_click"] = qty_field.input_value()
                except Exception:
                    pass
                try:
                    debug_path = DATA_DIR / f"debug_yamimall_qty_{item_code}.json"
                    debug_path.write_text(json.dumps(qty_debug, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception:
                    pass

            cart_btn = container.locator(".list_cart2_class, .sct_cart_add").first
            if cart_btn.count() == 0:
                return "no_cart_button"

            try:
                cart_btn.click(timeout=3000, force=True)
            except Exception:
                cart_btn.dispatch_event("click")
            page.wait_for_timeout(1000)

            # 비로그인/세션 만료 상태로 담기를 누르면 "로그인이 필요합니다" alert 후
            # login.php로 이동한다. 이 경우 담기 버튼은 찾았지만 실제로는 로그인이
            # 안 된 상태이므로, 세션이 만료된 것으로 간주하고 재로그인을 유도한다.
            if "login.php" in page.url or any("로그인" in m for m in alert_messages):
                return "login_required"

            # 담기 확인 팝업 처리 (add_yamimall_cart와 동일한 확인/계속쇼핑 패턴)
            for btn_text in ("확인", "계속쇼핑"):
                popup_btn = page.locator(f"button.ui-button:has-text('{btn_text}')").last
                if popup_btn.count() > 0:
                    try:
                        popup_btn.click(timeout=2000, force=True)
                    except Exception:
                        try:
                            popup_btn.evaluate("(el) => el.click()")
                        except Exception:
                            pass
                    page.wait_for_timeout(500)

            # alert/리다이렉트가 없어도 조용히 실패하는 경우가 있어(실측으로 확인),
            # 헤더의 실시간 장바구니 개수 배지로 최종 확인한다.
            after_count = _read_cart_count()
            if before_count >= 0 and after_count >= 0 and after_count <= before_count:
                if alert_messages:
                    return f"blocked:{alert_messages[-1]}"
                return "not_added"

            return "ok"

        try:
            logged_in_fresh = False
            if not cached_state:
                login_yamimall(page, username, password, base_url=base_url)
                logged_in_fresh = True

            outcome = _try_add()
            if outcome in ("no_cart_button", "not_found", "login_required", "not_added") and cached_state:
                # 캐시된 세션이 만료됐을 수 있으니 새로 로그인해서 한 번 더 시도
                login_yamimall(page, username, password, base_url=base_url)
                logged_in_fresh = True
                outcome = _try_add()

            if outcome == "not_found":
                return {"ok": False, "reason": f"검색 결과에서 해당 상품(코드 {item_code})을 찾지 못함"}
            if outcome == "no_cart_button":
                return {"ok": False, "reason": "장바구니 버튼을 찾지 못함"}
            if outcome == "login_required":
                return {"ok": False, "reason": "로그인이 필요합니다 (아이디/비밀번호를 확인해주세요)"}
            if outcome == "not_added":
                return {"ok": False, "reason": "담기를 시도했지만 장바구니 수량이 늘지 않음 (재고/최소수량 등 확인 필요)"}
            if outcome.startswith("blocked:"):
                return {"ok": False, "reason": outcome[len("blocked:"):]}

            if logged_in_fresh:
                vendors.save_session_state(store_id, vendor_id, context.storage_state())

            return {"ok": True, "item_code": item_code, "qty": qty}
        except Exception as e:
            return {"ok": False, "reason": str(e)}
        finally:
            browser.close()


def add_yamimall_cart(username: str, password: str, items: list[dict]):
    success = []
    failed = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-setuid-sandbox",
            ],
        )

        page = browser.new_page()

        try:
            login_yamimall(page, username, password)

            for item in items:
                name = (
                    item.get("catalog_menu_name")
                    or item.get("메뉴명")
                    or item.get("품목명")
                    or item.get("menu_name")
                    or ""
                )

                keyword = (
                    item.get("wholesale_search_keyword")
                    or item.get("catalog_search_keyword")
                    or item.get("search_keyword")
                    or item.get("catalog_menu_name")
                    or name
                )

                sold_qty = int(item.get("판매수량", 0) or item.get("sold_qty", 0) or 0)

                print("[YAMIMALL] keyword =", keyword)
                print("[YAMIMALL] item =", item)


                if sold_qty <= 0:
                    continue

                try:
                    # 검색
                    run_yamimall_search(page, keyword)

                    print("[YAMIMALL] current url =", page.url)
                    # 검색 결과 첫 상품
                    print(
                        "[YAMIMALL] page text sample =",
                        page.locator("body").inner_text()[:500]
                    )

                    first_product, product_text, best_score, best_index, best_page = find_best_yamimall_product(
                        page=page,
                        keyword=keyword,
                        max_pages=5
                    )

                    if first_product is None:
                        failed.append({
                            "name": name,
                            "keyword": keyword,
                            "sold_qty": sold_qty,
                            "reason": "검색 결과 없음",
                            "searched_pages": 5
                        })
                        continue

                    if best_score <= 0:
                        failed.append({
                            "name": name,
                            "keyword": keyword,
                            "sold_qty": sold_qty,
                            "reason": "검색어와 일치하는 적합한 상품을 찾지 못함"
                        })
                        continue

                    unit_qty = extract_wholesale_unit_qty(product_text)

                    if not unit_qty:
                        failed.append({
                            "name": name,
                            "keyword": keyword,
                            "sold_qty": sold_qty,
                            "product_text": product_text,
                            "reason": "1타 수량 추출 실패"
                        })
                        continue

                    # 판매수량 / 1타수량 → 50% 기준 반올림
                    cart_qty = calc_yamimall_cart_qty(sold_qty, unit_qty)

                    if cart_qty <= 0:
                        continue

                    # 품절 체크: 실제 문구/selector에 따라 추후 보완 가능
                    product_area_text = ""
                    try:
                        product_area_text = page.locator("body").inner_text(timeout=1000)
                    except Exception:
                        product_area_text = ""

                    if "품절" in product_area_text and page.locator(".list_cart2_class").count() == 0:
                        failed.append({
                            "name": name,
                            "keyword": keyword,
                            "sold_qty": sold_qty,
                            "unit_qty": unit_qty,
                            "cart_qty": cart_qty,
                            "reason": "품절 또는 장바구니 버튼 없음"
                        })
                        continue

                    # 검색 결과 첫 번째 장바구니 버튼 클릭
                    cart_buttons = page.locator(".list_cart2_class")

                    if cart_buttons.count() == 0:
                        failed.append({
                            "name": name,
                            "keyword": keyword,
                            "sold_qty": sold_qty,
                            "unit_qty": unit_qty,
                            "cart_qty": cart_qty,
                            "reason": "장바구니 버튼 없음"
                        })
                        continue

                    print("[YAMIMALL] selected page =", best_page)
                    print("[YAMIMALL] selected index =", best_index)
                    print("[YAMIMALL] cart button count =", cart_buttons.count())
                    cart_index = max(0, best_index // 5)

                    if cart_index >= cart_buttons.count():
                        cart_index = cart_buttons.count() - 1

                    print("[YAMIMALL] click cart button index =", cart_index)

                    try:
                        cart_buttons.nth(cart_index).click(timeout=3000, force=True)
                    except Exception:       
                        cart_buttons.nth(cart_index).dispatch_event("click")

                    page.wait_for_timeout(1000)

                    print("[YAMIMALL] cart popup opened")

                    # 기본 수량 1 → 필요한 타수만큼 + 클릭
                    plus_button = page.locator(".add_qty_class").first

                    print("[YAMIMALL] plus button count =", page.locator(".add_qty_class").count())

                    for _ in range(cart_qty - 1):
                        plus_button.click(timeout=1000)
                        page.wait_for_timeout(100)

                    print("[YAMIMALL] qty set done")

                    # 확인 클릭
                    print("[YAMIMALL] confirm button count =", page.locator("button.ui-button:has-text('확인')").count())
                    confirm_btn = page.locator("button.ui-button:has-text('확인')").last

                    try:
                        confirm_btn.click(timeout=3000, force=True)
                    except Exception:
                        confirm_btn.evaluate("(el) => el.click()")

                    print("[YAMIMALL] confirm clicked")
                    page.wait_for_timeout(700)

                    # 계속쇼핑 클릭
                    print("[YAMIMALL] continue shopping count =", page.locator("button.ui-button:has-text('계속쇼핑')").count())
                    continue_btn = page.locator("button.ui-button:has-text('계속쇼핑')").last

                    try:
                        continue_btn.click(timeout=3000, force=True)
                    except Exception:
                        continue_btn.evaluate("(el) => el.click()")

                    print("[YAMIMALL] continue shopping clicked")
                    page.wait_for_timeout(700)

                    success.append({
                        "name": name,
                        "keyword": keyword,
                        "yamimall_product_text": product_text,
                        "sold_qty": sold_qty,
                        "unit_qty": unit_qty,
                        "cart_qty": cart_qty,
                        "reason": "장바구니 담기 성공"
                    })

                except PlaywrightTimeoutError:
                    failed.append({
                        "name": name,
                        "keyword": keyword,
                        "sold_qty": sold_qty,
                        "reason": "페이지 로딩 시간 초과"
                    })

                except Exception as e:
                    failed.append({
                        "name": name,
                        "keyword": keyword,
                        "sold_qty": sold_qty,
                        "reason": f"장바구니 담기 실패: {str(e)}"
                    })

        finally:
            browser.close()

    return {
        "ok": True,
        "success": success,
        "failed": failed,
        "success_count": len(success),
        "failed_count": len(failed)
    }

