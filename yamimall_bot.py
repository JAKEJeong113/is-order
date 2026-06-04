import math
import re
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


YAMIMALL_URL = "https://xn--352blx12s.com"


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
            page.goto(YAMIMALL_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)
            close_yamimall_popups(page)

            # 로그인 페이지 이동
            page.locator(".hdgnb_login_class").click()
            page.wait_for_timeout(1000)

            # 아이디 / 비밀번호 입력
            page.fill("#login_id", username)
            page.fill("#login_pw", password)

            # 로그인 실행
            page.locator(".login_btn").click()
            page.wait_for_timeout(1000)

            # 로그인 후 팝업 다시 닫기
            close_yamimall_popups(page)

            for item in items:
                name = item.get("메뉴명") or item.get("품목명") or item.get("menu_name") or ""

                keyword = (
                    item.get("wholesale_search_keyword")
                    or item.get("search_keyword")
                    or item.get("catalog_search_keyword")
                    or name
                )

                sold_qty = int(item.get("판매수량", 0) or item.get("sold_qty", 0) or 0)

                print("[YAMIMALL] keyword =", keyword)
                print("[YAMIMALL] item =", item)


                if sold_qty <= 0:
                    continue

                try:
                    # 검색
                    page.fill("#sch_str", "")
                    page.fill("#sch_str", keyword)
                    
                    page.locator("#sch_submit").click(force=True)

                    page.wait_for_timeout(4000)

                    print("[YAMIMALL] current url =", page.url)
                    # 검색 결과 첫 상품
                    product_links = page.locator("a[href*='/shop/item.php?code=']")
                    print(
                        "[YAMIMALL] page text sample =",
                        page.locator("body").inner_text()[:500]
                    )

                    if product_links.count() == 0:
                        failed.append({
                            "name": name,
                            "keyword": keyword,
                            "sold_qty": sold_qty,
                            "reason": "검색 결과 없음"
                        })
                        continue

                    first_product = None
                    product_text = ""
                    best_score = -1
                    best_index = -1

                    for i in range(product_links.count()):
                        link = product_links.nth(i)
                        text = link.evaluate("(el) => el.innerText || el.textContent || ''").strip()

                        print(f"[YAMIMALL] candidate {i} =", repr(text))

                        if not text:
                            continue

                        if "SOLD OUT" in text.upper() or "품절" in text:
                            continue

                        unit_qty_temp = extract_wholesale_unit_qty(text)
                        if not unit_qty_temp:
                            continue

                        score = keyword_match_score(keyword, text)

                        print(f"[YAMIMALL] candidate {i} score =", score)

                        if score > best_score:
                            best_score = score
                            first_product = link
                            product_text = text
                            best_index = i

                    if first_product is None or best_score <= 0:
                        failed.append({
                            "name": name,
                            "keyword": keyword,
                            "sold_qty": sold_qty,
                            "reason": "검색어와 일치하는 적합한 상품을 찾지 못함"
                        })
                        continue

                    if first_product is None:
                        failed.append({
                            "name": name,
                            "keyword": keyword,
                            "sold_qty": sold_qty,
                            "reason": "검색 결과에서 1타 수량 포함 상품을 찾지 못함"
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

                    print("[YAMIMALL] selected index =", best_index)
                    print("[YAMIMALL] cart button count =", cart_buttons.count())
                    print("[YAMIMALL] click cart button index =", best_index // 5)
                    
                    try:
                        cart_buttons.nth(best_index // 5).click(timeout=3000, force=True)
                    except Exception:
                        cart_buttons.nth(best_index // 5).evaluate("(el) => el.click()")

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

