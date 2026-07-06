# samwon_bot.py
"""삼원유통(15774281.com, 카페24 플랫폼) 봇.

가격이 비로그인 상태에서는 서버에서 전혀 내려오지 않아(placeholder 없음),
정확한 가격 selector는 실 로그인 테스트로 확인/보정이 필요하다.
여러 후보 selector를 순서대로 시도하는 방어적 구조로 작성.
"""
import re
from urllib.parse import quote

from playwright.sync_api import Page, sync_playwright, TimeoutError as PWTimeoutError

BASE_URL = "https://15774281.com"

PRICE_CANDIDATES = [
    "#span_product_price_text",
    ".price_wrap .price",
    "td.right .price",
    ".xans-product-detail .price",
]


def login_cafe24(page: Page, base_url: str, login_id: str, login_pwd: str) -> None:
    page.goto(f"{base_url}/member/login.html", wait_until="domcontentloaded", timeout=30000)
    page.fill("#member_id", login_id)
    page.fill("#member_passwd", login_pwd)
    page.locator("#member_form_3208978318, form[action*='Member/login']").first.locator(
        "button, input[type=submit], a.btnSubmit"
    ).first.click()
    page.wait_for_load_state("networkidle", timeout=20000)

    if "login.html" in page.url:
        raise RuntimeError("삼원유통(카페24) 로그인 실패 (아이디/비밀번호를 확인해주세요)")


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
    match = re.search(r"[xX×*]\s*(\d+)\s*개", text or "")
    return int(match.group(1)) if match else None


def search_candidates(page: Page, base_url: str, keyword: str, top_n: int = 3) -> list[dict]:
    url = f"{base_url}/product/search.html?keyword={quote(keyword)}"
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1200)

    items = page.locator("li.item.xans-record-")
    count = items.count()
    if count == 0:
        return []

    scored = []
    for i in range(min(count, 30)):
        item = items.nth(i)
        try:
            name = item.locator("p.name a").first.inner_text().strip()
            href = item.locator("a[href*='product_no=']").first.get_attribute("href")
        except Exception:
            continue

        score = _match_score(keyword, name)
        if score <= 0:
            continue

        product_no_match = re.search(r"product_no=(\d+)", href or "")
        product_no = product_no_match.group(1) if product_no_match else None

        price = None
        price_text = ""
        for sel in PRICE_CANDIDATES:
            loc = item.locator(sel)
            if loc.count() > 0:
                price_text = loc.first.inner_text().strip()
                price = _parse_price(price_text)
                if price:
                    break

        scored.append({
            "name": name,
            "price": price,
            "price_text": price_text,
            "unit_qty": _extract_unit_qty(name),
            "goods_no": product_no,
            "product_url": f"{base_url}/product/detail.html?product_no={product_no}" if product_no else None,
            "score": score,
        })

    deduped = {}
    for r in scored:
        key = (r["name"], r["price"])
        if key not in deduped:
            deduped[key] = r

    result = sorted(deduped.values(), key=lambda r: r["score"], reverse=True)
    return result[:top_n]


def fetch_candidates(login_id: str, login_pwd: str, keywords: list[str], top_n: int = 3) -> dict[str, list[dict]]:
    results: dict[str, list[dict]] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-setuid-sandbox"],
        )
        page = browser.new_page()

        try:
            login_cafe24(page, BASE_URL, login_id, login_pwd)

            for keyword in keywords:
                try:
                    results[keyword] = search_candidates(page, BASE_URL, keyword, top_n=top_n)
                except PWTimeoutError:
                    results[keyword] = []
                except Exception as e:
                    print(f"[SAMWON] 검색 실패 ({keyword}):", e)
                    results[keyword] = []
        finally:
            browser.close()

    return results
