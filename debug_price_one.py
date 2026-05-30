import json, re, requests
from bs4 import BeautifulSoup

COOKIES_PATH = "cmt_cookies.json"
GOODS_NO = "1000000047"
URL = f"https://m.cmtstory.com/goods/goods_view.php?goodsNo={GOODS_NO}"

PRICE_LABEL_PRIORITY = ["공급가", "도매가", "회원가", "할인가", "판매가", "정상가"]
EXCLUDE_LABELS = ["배송비", "적립", "포인트", "쿠폰", "혜택", "합계", "총", "결제", "배송", "적립금"]

def session_from_cookies():
    cookies = json.load(open(COOKIES_PATH, "r", encoding="utf-8"))
    s = requests.Session()
    for c in cookies:
        s.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
    s.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://m.cmtstory.com/"})
    return s

def extract_won_all(text: str):
    out = []
    for m in re.findall(r"(\d[\d,]{2,})\s*원", text):
        out.append(int(m.replace(",", "")))
    return out

def extract_won_first(text: str):
    m = re.search(r"(\d[\d,]{2,})\s*원", text)
    if not m:
        return None
    return int(m.group(1).replace(",", ""))

def pick_price_debug(soup: BeautifulSoup):
    candidates = []  # (priority_idx, price, label, context)

    # (A) tr/th/td 구조
    for row in soup.select("tr"):
        th = row.find("th")
        td = row.find("td")
        if not th or not td:
            continue
        label = th.get_text(" ", strip=True)
        value = td.get_text(" ", strip=True)

        if any(x in label for x in EXCLUDE_LABELS):
            continue

        for i, key in enumerate(PRICE_LABEL_PRIORITY):
            if key in label:
                price = extract_won_first(value)
                if price is not None:
                    candidates.append((i, price, key, f"[TABLE] {label} => {value}"))

    # (B) 라벨 주변 텍스트
    for i, key in enumerate(PRICE_LABEL_PRIORITY):
        for node in soup.find_all(string=re.compile(key)):
            parent = node.parent
            if not parent:
                continue
            ctx1 = parent.get_text(" ", strip=True)
            ctx2 = parent.parent.get_text(" ", strip=True) if parent.parent else ""

            for ctx in [ctx1, ctx2]:
                if any(x in ctx for x in EXCLUDE_LABELS):
                    continue
                price = extract_won_first(ctx)
                if price is not None:
                    candidates.append((i, price, key, f"[NEAR] {ctx[:200]}"))
                    break

    candidates.sort(key=lambda x: (x[0], x[1]))
    return candidates

if __name__ == "__main__":
    s = session_from_cookies()
    r = s.get(URL, timeout=30)
    print("status:", r.status_code, "final:", r.url)

    soup = BeautifulSoup(r.text, "lxml")

    # 페이지에 존재하는 모든 '원' 후보(참고)
    text = soup.get_text("\n", strip=True)
    all_won = extract_won_all(text)
    print("all '원' amounts (sample 30):", all_won[:30])

    cands = pick_price_debug(soup)
    print("\n=== candidates (top 20) ===")
    for c in cands[:20]:
        print(c[2], c[1], "|", c[3])