import json
from playwright.sync_api import sync_playwright

COOKIES_PATH = "cmt_cookies.json"
GOODS_NO = "1000000047"
URL = f"https://m.cmtstory.com/goods/goods_view.php?goodsNo={GOODS_NO}"

def load_cookies():
    # cmt_cookies.json은 이미 Playwright 형식으로 저장되어 있음
    return json.load(open(COOKIES_PATH, "r", encoding="utf-8"))

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)

    # ✅ 쿠키 주입을 위해 context 생성
    context = browser.new_context()
    context.add_cookies(load_cookies())

    page = context.new_page()

    def on_response(res):
        try:
            req = res.request
            if req.resource_type not in ("xhr", "fetch"):
                return

            print("\n=== XHR/FETCH ===")
            print("URL:", res.url)
            print("STATUS:", res.status)
            print("METHOD:", req.method)

            if req.method == "POST":
                data = req.post_data
                if data:
                    print("POST_DATA:", data[:1200])  # 민감값 있으면 너가 지워도 됨

            ctype = (res.headers.get("content-type") or "").lower()
            print("CONTENT-TYPE:", ctype)

            # 너무 길면 일부만
            try:
                if "application/json" in ctype:
                    j = res.json()
                    s = str(j)
                    print("JSON_PREVIEW:", s[:1800])
                else:
                    t = res.text()
                    print("TEXT_PREVIEW:", t[:800])
            except:
                pass

        except:
            pass

    page.on("response", on_response)

    # ✅ 쿠키가 적용되도록 먼저 도메인 진입
    page.goto("https://m.cmtstory.com/", wait_until="domcontentloaded")

    # ✅ 그 다음 상품 페이지
    page.goto(URL, wait_until="networkidle")

    # ✅ 옵션/수량 변경해야 XHR 나가는 상품도 있어서 잠깐 대기
    page.wait_for_timeout(10000)

    input("XHR/FETCH 로그가 찍혔는지 확인 후 엔터를 누르면 종료합니다...")
    browser.close()