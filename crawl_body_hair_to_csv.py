import re, csv, json, time, requests
from bs4 import BeautifulSoup
from urllib.parse import quote

COOKIES_PATH = "cmt_cookies.json"
GOODSNO_TXT = "goodsno_body_hair.txt"

def session_from_cookies():
    cookies = json.load(open(COOKIES_PATH, "r", encoding="utf-8"))
    s = requests.Session()
    for c in cookies:
        s.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
    s.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        "Referer": "https://m.cmtstory.com/"
    })
    return s

def double_urlencode(url: str) -> str:
    # ✅ sniff에서 본 currentPageUrl은 이중 인코딩 형태였음
    return quote(quote(url, safe=""), safe="")

def fetch_supply_price_from_layer_option(session: requests.Session, goods_no: str):
    detail_url = f"https://m.cmtstory.com/goods/goods_view.php?goodsNo={goods_no}"
    post_url = "https://m.cmtstory.com/goods/layer_option.php"

    payload = {
        "type": "goods",
        "goodsNo": goods_no,
        "currentPageUrl": double_urlencode(detail_url)
    }

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": detail_url,
        "Origin": "https://m.cmtstory.com",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }

    r = session.post(post_url, data=payload, headers=headers, timeout=30)
    if r.status_code != 200:
        return None, f"layer_option_status={r.status_code} | body_preview={r.text[:120]}"

    soup = BeautifulSoup(r.text, "lxml")

    price_el = soup.select_one('input#set_goods_price')
    fixed_el = soup.select_one('input#set_goods_fixedPrice')
    coupon_el = soup.select_one('input[name="set_coupon_dc_price"], input#set_coupon_dc_price')

    def to_int(v):
        if v is None:
            return None
        v = str(v).strip()
        if v == "":
            return None
        try:
            return int(float(v))
        except:
            return None

    supply_price = to_int(price_el.get("value")) if price_el else None
    fixed_price  = to_int(fixed_el.get("value")) if fixed_el else None
    coupon_price = to_int(coupon_el.get("value")) if coupon_el else None

    if supply_price is None:
        preview = soup.get_text(" ", strip=True)[:120]
        return None, f"no_set_goods_price | preview={preview}"

    debug = f"set_goods_price={supply_price} | fixed={fixed_price} | coupon={coupon_price}"
    return supply_price, debug
def parse_name_brand_from_detail(html: str):
    soup = BeautifulSoup(html, "lxml")

    # ✅ 상품명은 og:title이 제일 안정적
    name = ""
    og = soup.select_one('meta[property="og:title"]')
    if og and og.get("content"):
        name = og["content"].strip()

    # og:title이 없으면 기존 후보(단, "필수정보" 같은 섹션 제목 방지 위해 필터)
    if not name:
        for sel in [".goods_name", ".item_name", "h1", "h2", "h3"]:
            el = soup.select_one(sel)
            if el and el.get_text(strip=True):
                cand = el.get_text(strip=True)
                if cand in ("필수정보", "상품필수정보", "상품정보"):
                    continue
                name = cand
                break

    text = soup.get_text("\n", strip=True)

    brand = ""
    m = re.search(r"브랜드\s*[:：]\s*([^\n]+)", text)
    if m:
        brand = m.group(1).strip()

    return name, brand

if __name__ == "__main__":
    goods_nos = [line.strip() for line in open(GOODSNO_TXT, "r", encoding="utf-8") if line.strip()]
    s = session_from_cookies()

    rows = []
    for i, goods_no in enumerate(goods_nos, 1):
        detail_url = f"https://m.cmtstory.com/goods/goods_view.php?goodsNo={goods_no}"

        # 1) 상세페이지(상품명/브랜드용)
        r = s.get(detail_url, headers={"Referer": "https://m.cmtstory.com/"}, timeout=30)
        if r.status_code != 200:
            print(f"[SKIP] {goods_no} detail status={r.status_code}")
            continue

        name, brand = parse_name_brand_from_detail(r.text)

        # 2) ✅ 공급가(회원가): layer_option.php에서 확정 추출
        supply_price, price_debug = fetch_supply_price_from_layer_option(s, goods_no)

        rows.append({
            "goodsNo": goods_no,
            "name": name,
            "brand": brand,
            "supply_price": supply_price,
            "url": detail_url,
            "price_candidates": price_debug
        })

        if i % 20 == 0:
            print(f"done {i}/{len(goods_nos)}")

        time.sleep(0.2)

    with open("body_hair_products.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["goodsNo", "name", "brand", "supply_price", "url", "price_candidates"]
        )
        w.writeheader()
        w.writerows(rows)

    print("[SAVED] body_hair_products.csv", len(rows))