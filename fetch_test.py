import json, requests
from bs4 import BeautifulSoup

cookies = json.load(open("cmt_cookies.json", "r", encoding="utf-8"))

s = requests.Session()
for c in cookies:
    s.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))

url = "https://m.cmtstory.com/goods/goods_view.php?goodsNo=1000000387"
r = s.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
print("status:", r.status_code)
print("final url:", r.url)
print(r.text[:500])