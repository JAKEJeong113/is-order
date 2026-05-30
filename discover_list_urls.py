import json, requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

COOKIES_PATH = "cmt_cookies.json"

# 모바일이든 PC든, "카테고리 메뉴가 들어있는 페이지"면 됨
START_URLS = [
    "https://m.cmtstory.com/",
    "https://www.cmtstory.com/",
]

def session_from_cookies():
    cookies = json.load(open(COOKIES_PATH, "r", encoding="utf-8"))
    s = requests.Session()
    for c in cookies:
        s.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
    s.headers.update({"User-Agent": "Mozilla/5.0"})
    return s

def extract_list_urls(html: str, base_url: str):
    soup = BeautifulSoup(html, "lxml")
    urls = set()
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        text = a.get_text(strip=True)

        # 목록 페이지로 보이는 패턴들(사이트마다 달라서 넓게 잡음)
        if ("goods_list" in href) or ("cateCd=" in href) or ("goods_search" in href):
            full = urljoin(base_url, href)
            urls.add((text, full))
    return urls

s = session_from_cookies()

all_urls = set()
for u in START_URLS:
    r = s.get(u, timeout=30)
    print("GET", u, "->", r.status_code, r.url)
    if r.status_code != 200:
        continue
    for item in extract_list_urls(r.text, r.url):
        all_urls.add(item)

print("\n=== 후보 목록 URL (텍스트, URL) ===")
for text, url in sorted(all_urls)[:200]:
    print(text, "|", url)

print("\n총 후보 수:", len(all_urls))