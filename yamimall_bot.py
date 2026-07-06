import math
import re
from urllib.parse import quote

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


YAMIMALL_URL = "https://xn--352blx12s.com"


def run_yamimall_search(page, keyword: str) -> None:
    """검색창 버튼(#sch_submit)은 아이콘폰트라 실제 클릭이 안 먹는 경우가 있어
    검색 폼이 실제로 이동하는 GET URL로 직접 이동한다."""
    page.goto(
        f"{YAMIMALL_URL}/shop/search.php?skey={quote(keyword)}",
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


def crawl_full_catalog(username: str, password: str) -> list[dict]:
    """카테고리별 '전체보기' 코드를 모두 순회해서 전체 상품을 수집한다."""
    products: dict[str, dict] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-setuid-sandbox"],
        )
        page = browser.new_page()
        _block_heavy_resources(page)

        try:
            login_yamimall(page, username, password)

            for code in FULL_CATALOG_CATEGORY_CODES:
                try:
                    page.goto(
                        f"https://xn--352blx12s.com:443/shop/list.php?code={code}",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    page.wait_for_timeout(1200)
                except Exception as e:
                    print(f"[YAMIMALL] 카테고리 {code} 로딩 실패:", e)
                    continue

                for item in _extract_list_page_items(page):
                    key = item.pop("_key")
                    if key not in products:
                        products[key] = item
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


def login_yamimall(page, username: str, password: str) -> None:
    page.goto(YAMIMALL_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2000)
    close_yamimall_popups(page)

    page.locator(".hdgnb_login_class").click()
    page.wait_for_timeout(1000)

    page.fill("#login_id", username)
    page.fill("#login_pw", password)

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


def add_to_cart(username: str, password: str, product_url: str, qty: int = 1) -> dict:
    """상품 상세페이지에서 실제로 장바구니에 담는다. product_url은 item.php?code=... 형태."""
    code_match = re.search(r"code=(\d+)", product_url or "")
    if not code_match:
        return {"ok": False, "reason": f"상품 코드 추출 실패: {product_url}"}
    item_code = code_match.group(1)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--disable-setuid-sandbox"],
        )
        page = browser.new_page()

        try:
            login_yamimall(page, username, password)
            page.goto(
                f"{YAMIMALL_URL}:443/shop/item.php?code={item_code}",
                wait_until="domcontentloaded",
                timeout=30000,
            )

            # 수량 조절 버튼(.add_qty_class)이 지연된 AJAX로 뜨는 경우가 있어 넉넉히 대기
            plus_button = page.locator(".add_qty_class").first
            try:
                plus_button.wait_for(state="attached", timeout=8000)
            except PlaywrightTimeoutError:
                return {"ok": False, "reason": "수량 조절 버튼을 찾지 못함 (품절이거나 페이지 구조 변경)"}

            for _ in range(max(qty, 1) - 1):
                plus_button.click(timeout=1000)
                page.wait_for_timeout(150)

            cart_btn = page.locator("#sit_btn_cart")
            if cart_btn.count() == 0:
                return {"ok": False, "reason": "장바구니 버튼을 찾지 못함"}

            cart_btn.click(timeout=5000)
            page.wait_for_timeout(1500)

            # 담기 확인 팝업 처리 (있으면 닫기)
            dialog_btn = page.locator(".ui-dialog-buttonpane button").first
            if dialog_btn.count() > 0:
                try:
                    dialog_btn.click(timeout=2000)
                except Exception:
                    pass

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

