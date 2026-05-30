from playwright.sync_api import sync_playwright

GOODS_NO = "1000000047"
URL = f"https://m.cmtstory.com/goods/goods_view.php?goodsNo={GOODS_NO}"

KEYWORDS = [
    "goods", "option", "price", "ajax", "ps.php", "view", "cart", "member", "benefit"
]

def looks_interesting(url: str) -> bool:
    u = url.lower()
    return any(k in u for k in KEYWORDS)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()

    def on_response(res):
        try:
            req = res.request
            rtype = req.resource_type  # "xhr", "fetch", "document", ...
            if rtype not in ("xhr", "fetch"):
                return

            u = res.url
            if not looks_interesting(u):
                return

            print("\n=== XHR/FETCH ===")
            print("URL:", u)
            print("STATUS:", res.status)
            print("METHOD:", req.method)

            # POST면 payload도 같이 찍기(민감정보는 너가 가려도 됨)
            if req.method == "POST":
                try:
                    data = req.post_data
                    if data:
                        print("POST_DATA:", data[:1000])
                except:
                    pass

            # 응답이 json이면 json 찍고, 아니면 text 일부 출력
            ctype = (res.headers.get("content-type") or "").lower()
            print("CONTENT-TYPE:", ctype)

            if "application/json" in ctype:
                try:
                    j = res.json()
                    print("JSON_KEYS:", list(j.keys())[:30] if isinstance(j, dict) else type(j))
                    print("JSON_PREVIEW:", str(j)[:1500])
                except:
                    txt = res.text()
                    print("TEXT_PREVIEW:", txt[:1500])
            else:
                # html 조각/텍스트인 경우도 있음
                txt = res.text()
                print("TEXT_PREVIEW:", txt[:800])

        except Exception as e:
            # 너무 시끄럽게 하지 않기
            pass

    page.on("response", on_response)

    page.goto(URL, wait_until="networkidle")

    # ✅ 옵션/수량 변경이 있어야 ajax가 나가는 상품이 많아서, 몇 초 대기
    page.wait_for_timeout(8000)

    input("콘솔에 XHR/FETCH 로그가 찍혔는지 확인 후 엔터를 누르면 종료합니다...")
    browser.close()