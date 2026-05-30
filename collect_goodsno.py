import re, json, time, requests
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

COOKIES_PATH = "cmt_cookies.json"
BASE_LIST_URL = "https://m.cmtstory.com/goods/goods_list.php?cateCd=003"

def session_from_cookies():
    cookies = json.load(open(COOKIES_PATH, "r", encoding="utf-8"))
    s = requests.Session()
    for c in cookies:
        s.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    return s

def set_page(url: str, page_num: int, page_param: str = "page"):
    parsed = urlparse(url)
    q = parse_qs(parsed.query)
    q[page_param] = [str(page_num)]
    new_query = urlencode(q, doseq=True)
    return urlunparse(parsed._replace(query=new_query))

def collect_goodsnos(list_url: str, max_pages: int = 200, sleep_s: float = 0.25):
    s = session_from_cookies()

    # 사이트마다 페이지 파라미터명이 다를 수 있어서 후보를 자동 테스트
    page_params = ["page", "pageNum", "pageNo", "p", "pg"]
    best = {"param": None, "count": 0, "goods": set()}

    for param in page_params:
        test_url = set_page(list_url, 1, param)
        r = s.get(test_url, timeout=30)
        if r.status_code != 200:
            continue
        found = set(re.findall(r"goodsNo=(\d+)", r.text))
        if len(found) > best["count"]:
            best = {"param": param, "count": len(found), "goods": found}

    if best["param"] is None or best["count"] == 0:
        raise RuntimeError("페이지 파라미터를 찾지 못했어요. 목록 HTML에 goodsNo가 없거나, JS로 로딩되는 구조일 수 있어요.")

    page_param = best["param"]
    print(f"[OK] page 파라미터 추정: '{page_param}' (1페이지에서 goodsNo {best['count']}개 발견)")

    goods = set()
    prev_total = 0

    for p in range(1, max_pages + 1):
        url = set_page(list_url, p, page_param)
        r = s.get(url, timeout=30)
        if r.status_code != 200:
            print(f"[STOP] page={p} status={r.status_code}")
            break

        found = set(re.findall(r"goodsNo=(\d+)", r.text))
        if not found:
            print(f"[STOP] page={p} goodsNo 0개 (끝이거나 구조 다름)")
            break

        goods |= found
        print(f"page {p:03d} | found {len(found):03d} | total {len(goods)}")

        # 새로 늘지 않으면 종료(마지막 페이지)
        if len(goods) == prev_total:
            print("[STOP] 더 이상 신규 goodsNo 없음")
            break
        prev_total = len(goods)

        time.sleep(sleep_s)

    return sorted(goods)

if __name__ == "__main__":
    goods_nos = collect_goodsnos(BASE_LIST_URL)
    print("\nTOTAL goodsNo:", len(goods_nos))
    print("SAMPLE:", goods_nos[:20])

    with open("goodsno_body_hair.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(goods_nos))
    print("[SAVED] goodsno_body_hair.txt")