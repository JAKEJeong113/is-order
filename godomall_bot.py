# godomall_bot.py
"""고도몰(Godomall) 플랫폼 공통 봇: 과자생각(ccdome), 삼봉몰(3bong)에서 재사용."""
import re
from urllib.parse import quote

from playwright.sync_api import Page, sync_playwright, TimeoutError as PWTimeoutError


def login_godomall(page: Page, base_url: str, login_id: str, login_pwd: str) -> None:
    page.goto(f"{base_url}/member/login.php", wait_until="domcontentloaded", timeout=30000)
    page.fill("#loginId", login_id)
    page.fill("#loginPwd", login_pwd)
    page.locator("#formLogin").locator("button, input[type=submit]").first.click()
    page.wait_for_timeout(3000)

    if "login.php" in page.url:
        raise RuntimeError("고도몰 로그인 실패 (아이디/비밀번호를 확인해주세요)")

    # 비밀번호 변경 안내 팝업("다음에 변경") 무시하고 넘어가기
    later_btn = page.locator("#btnLater, button:has-text('다음에 변경')")
    if later_btn.count() > 0:
        later_btn.first.click(timeout=3000)
        page.wait_for_timeout(1000)


def _parse_price(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def _match_score(keyword: str, product_text: str) -> int:
    if not keyword or not product_text:
        return 0
    product_norm = _normalize_text(product_text)
    score = 0
    for word in keyword.split():
        if word and _normalize_text(word) in product_norm:
            score += 1
    return score


def _extract_unit_qty(text: str) -> int | None:
    match = re.search(r"[xX×]\s*(\d+)\s*개", text or "")
    return int(match.group(1)) if match else None


def search_candidates(page: Page, base_url: str, keyword: str, top_n: int = 3) -> list[dict]:
    """로그인된 상태에서 keyword로 검색해 검색어 일치도 상위 top_n개 후보를 반환."""
    url = f"{base_url}/goods/goods_search.php?keyword={quote(keyword)}"
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(800)

    items = page.locator("li:has(a[href*='goods_view.php']):has(.item_price)")
    count = items.count()
    if count == 0:
        return []

    scored = []
    for i in range(min(count, 30)):
        item = items.nth(i)
        try:
            name = item.locator(".item_name").first.inner_text().strip()
            price_text = item.locator(".item_price").first.inner_text().strip()
            href = item.locator("a[href*='goods_view.php']").first.get_attribute("href")
        except Exception:
            continue

        score = _match_score(keyword, name)
        if score <= 0:
            continue

        goods_no_match = re.search(r"goodsNo=(\d+)", href or "")
        goods_no = goods_no_match.group(1) if goods_no_match else None

        scored.append({
            "name": name,
            "price": _parse_price(price_text),
            "price_text": price_text,
            "unit_qty": _extract_unit_qty(name),
            "goods_no": goods_no,
            "product_url": f"{base_url}/goods/goods_view.php?goodsNo={goods_no}" if goods_no else None,
            "score": score,
        })

    deduped = {}
    for r in scored:
        key = (r["name"], r["price"])
        if key not in deduped:
            deduped[key] = r

    result = sorted(deduped.values(), key=lambda r: r["score"], reverse=True)
    return result[:top_n]


def _extract_page_items(page: Page, base_url: str) -> list[dict]:
    items = page.locator("li:has(a[href*='goods_view.php']):has(.item_price)")
    count = items.count()
    results = []

    for i in range(count):
        item = items.nth(i)
        try:
            name = item.locator(".item_name").first.inner_text().strip()
            price_text = item.locator(".item_price").first.inner_text().strip()
            href = item.locator("a[href*='goods_view.php']").first.get_attribute("href")
        except Exception:
            continue

        goods_no_match = re.search(r"goodsNo=(\d+)", href or "")
        goods_no = goods_no_match.group(1) if goods_no_match else None

        results.append({
            "name": name,
            "price": _parse_price(price_text),
            "unit_qty": _extract_unit_qty(name),
            "goods_no": goods_no,
            "product_url": f"{base_url}/goods/goods_view.php?goodsNo={goods_no}" if goods_no else None,
        })

    return results


def crawl_full_catalog(base_url: str, login_id: str, login_pwd: str, category_code: str, max_pages: int = 60) -> list[dict]:
    """'전체상품' 카테고리(스토어마다 cateCd가 다름)를 끝까지 페이지를 넘기며 전부 수집한다."""
    all_products: dict[str, dict] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-setuid-sandbox"],
        )
        page = browser.new_page()

        try:
            login_godomall(page, base_url, login_id, login_pwd)

            for page_no in range(1, max_pages + 1):
                page.goto(
                    f"{base_url}/goods/goods_list.php?cateCd={category_code}&page={page_no}",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                page.wait_for_timeout(600)

                items = _extract_page_items(page, base_url)
                if not items:
                    break

                new_count = 0
                for it in items:
                    key = it["goods_no"] or it["name"]
                    if key not in all_products:
                        all_products[key] = it
                        new_count += 1

                # 더 이상 새로운 상품이 없으면(마지막 페이지가 반복되는 경우) 종료
                if new_count == 0:
                    break
        finally:
            browser.close()

    return list(all_products.values())


def fetch_candidates(base_url: str, login_id: str, login_pwd: str, keywords: list[str], top_n: int = 3) -> dict[str, list[dict]]:
    """여러 키워드에 대해 로그인 1회 후 후보 목록을 조회. {keyword: [candidate, ...]} 반환."""
    results: dict[str, list[dict]] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-setuid-sandbox"],
        )
        page = browser.new_page()

        try:
            login_godomall(page, base_url, login_id, login_pwd)

            for keyword in keywords:
                try:
                    results[keyword] = search_candidates(page, base_url, keyword, top_n=top_n)
                except PWTimeoutError:
                    results[keyword] = []
                except Exception as e:
                    print(f"[GODOMALL] {base_url} 검색 실패 ({keyword}):", e)
                    results[keyword] = []
        finally:
            browser.close()

    return results


def add_to_cart(base_url: str, login_id: str, login_pwd: str, goods_no: str, qty: int) -> dict:
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-setuid-sandbox"],
        )
        page = browser.new_page()

        try:
            login_godomall(page, base_url, login_id, login_pwd)

            page.goto(
                f"{base_url}/goods/goods_view.php?goodsNo={goods_no}",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            page.wait_for_timeout(500)

            qty_input = page.locator("input[name='goodsCnt'], input.qty_input").first
            if qty_input.count() > 0:
                qty_input.fill(str(qty))

            # 상세페이지의 실제 담기 버튼은 #cartBtn. (.btn_basket_cart는 상세페이지 하단
            # '함께 보면 좋은 상품' 추천 위젯용이라 다른 상품 goods_no를 가리킴 - 사용 금지)
            cart_btn = page.locator("#cartBtn")
            if cart_btn.count() == 0:
                return {"ok": False, "goods_no": goods_no, "qty": qty, "reason": "담기 버튼(#cartBtn)을 찾지 못함"}

            cart_btn.click(timeout=5000)
            page.wait_for_timeout(1500)

            # "상품이 장바구니에 담겼습니다" 확인 팝업 닫기 (취소 = 현재 페이지 유지)
            close_btn = page.locator("button:has-text('취소'), button:has-text('확인')")
            if close_btn.count() > 0:
                close_btn.first.click(timeout=3000)
                page.wait_for_timeout(500)

            return {"ok": True, "goods_no": goods_no, "qty": qty}
        except Exception as e:
            return {"ok": False, "goods_no": goods_no, "qty": qty, "reason": str(e)}
        finally:
            browser.close()
