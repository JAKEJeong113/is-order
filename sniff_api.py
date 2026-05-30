from playwright.sync_api import sync_playwright

GOODS_NO = "1000000047"
URL = f"https://m.cmtstory.com/goods/goods_view.php?goodsNo={GOODS_NO}"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()

    # ✅ 이미 로그인 자동화가 된다면: 여기서 로그인 페이지로 가서 로그인 후 진행해도 됨
    # 너는 cookies.json이 있으니, 일단은 "기존 방식대로 로그인 자동화" 사용을 추천.
    page = context.new_page()

    def on_response(res):
        try:
            u = res.url
            if "goods_ps.php" in u:
                print("\n=== HIT goods_ps.php ===")
                print("URL:", u)
                print("STATUS:", res.status)
                # 응답이 JSON이면 json()이 되고, 아니면 text()로 찍힘
                try:
                    print("JSON:", res.json())
                except:
                    txt = res.text()
                    print("TEXT:", txt[:1000])
        except:
            pass

    page.on("response", on_response)

    page.goto(URL, wait_until="networkidle")

    # 옵션이 있는 상품은 옵션 선택 시 goods_ps.php가 호출되는 경우가 많아서 클릭 유도
    # (옵션 dropdown이 있으면 클릭/선택해보기)
    page.wait_for_timeout(5000)

    input("콘솔에 goods_ps.php가 찍혔는지 확인 후, 엔터를 누르면 종료합니다...")
    browser.close()