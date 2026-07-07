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

    with sync_playwright() as p:
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

                if page_no % PAGE_RECYCLE_INTERVAL == 0:
                    page.close()
                    page = context.new_page()
                    _block_heavy_resources(page)
        finally:
            browser.close()

    return list(all_products.values())
