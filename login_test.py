from playwright.sync_api import sync_playwright
import json

ID = "lottotto0404"
PW = "wjdtkdrud22@"

LOGIN_URL = "https://m.cmtstory.com/member/login.php"
TEST_URL  = "https://m.cmtstory.com/goods/goods_view.php?goodsNo=1000000387"

with sync_playwright() as p:
    # headless=False: 크롬창을 띄워서 “진짜로 로그인 되는지” 눈으로 확인
    browser = p.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()

    # 1) 로그인 페이지 이동
    page.goto(LOGIN_URL, wait_until="domcontentloaded")

    # 2) 아이디/비번 입력
    # ⚠️ 사이트마다 input name이 다를 수 있어. 실패하면 여기 selector만 수정하면 됨.
    page.fill('input[name="loginId"]', ID)
    page.fill('input[name="loginPwd"]', PW)

    # 3) 로그인 버튼 클릭
    page.locator("button.member_login_order_btn.member_login_btn").click()
    page.wait_for_load_state("networkidle")

    # 4) 로그인 유지 확인: 특정 상품페이지 들어가보기
    page.goto(TEST_URL, wait_until="domcontentloaded")
    html = page.content()

    if "login" in page.url:
        print("❌ 로그인 실패(또는 selector 불일치)")
    else:
        print("✅ 로그인 성공! 상품페이지 접근 OK")

    # 5) 쿠키 저장(다음 단계에서 requests로 재사용 가능)
    cookies = context.cookies()
    with open("cmt_cookies.json", "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)

    print("🍪 쿠키 저장 완료: cmt_cookies.json")

    browser.close()