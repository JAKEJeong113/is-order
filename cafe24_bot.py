# cafe24_bot.py
"""카페24(Cafe24) 플랫폼 공통 봇: 무마켓(moomarket)에서 사용."""
import re

from playwright.sync_api import Page, sync_playwright, TimeoutError as PWTimeoutError


def login_cafe24(page: Page, base_url: str, login_id: str, login_pwd: str) -> None:
    page.goto(f"{base_url}/member/login.html", wait_until="domcontentloaded", timeout=30000)
    page.fill("#member_id", login_id)
    page.fill("#member_passwd", login_pwd)

    # 카페24는 <button type=submit>이 아니라 onclick="MemberAction.login(...)"을 쓰는
    # <a class="btnLogin"> 링크로 로그인을 제출한다 (form id는 요청마다 랜덤하게 바뀜).
    page.locator("a.btnLogin").first.click()

    try:
        page.wait_for_url(lambda url: "login" not in url.lower(), timeout=8000)
    except PWTimeoutError:
        pass

    if "login" in page.url.lower():
        raise RuntimeError(f"카페24 로그인 실패 (아이디/비밀번호를 확인해주세요) / URL: {page.url}")


def _parse_price(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def _extract_unit_qty(text: str) -> int | None:
    match = re.search(r"[xX×*]\s*(\d+)\s*개|(\d+)\s*개입", text or "")
    if not match:
        return None
    return int(match.group(1) or match.group(2))


def _extract_list_page_items(page: Page, base_url: str) -> list[dict]:
    items = page.locator("ul.prdList li")
    count = items.count()
    results = []

    for i in range(count):
        item = items.nth(i)
        try:
            link = item.locator("div.thumbnail a").first
            href = link.get_attribute("href") or ""
            img = item.locator("div.thumbnail img").first
            name = (img.get_attribute("alt") or "").strip()

            # 가격 표시 위치가 테마마다 조금씩 달라서 상품명 옆 텍스트 전체에서 숫자+원을 찾는다
            desc_text = item.locator("div.description").inner_text()
            price = None
            price_match = re.search(r"([\d,]+)\s*원", desc_text)
            if price_match:
                price = _parse_price(price_match.group(1))

            no_match = re.search(r"/product/[^/]+/(\d+)/", href)
            product_no = no_match.group(1) if no_match else None
        except Exception:
            continue

        if not name or not product_no:
            continue

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

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-setuid-sandbox"],
        )
        page = browser.new_page()
        _block_heavy_resources(page)

        try:
            login_cafe24(page, base_url, login_id, login_pwd)

            for page_no in range(1, max_pages + 1):
                page.goto(
                    f"{base_url}/product/list.html?cate_no={category_code}&page={page_no}",
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
                page.wait_for_timeout(600)

                items = _extract_list_page_items(page, base_url)
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
