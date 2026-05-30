import json, requests
from bs4 import BeautifulSoup
from urllib.parse import quote

def double_urlencode(url: str) -> str:
    return quote(quote(url, safe=""), safe="")

cookies = json.load(open("cmt_cookies.json","r",encoding="utf-8"))
s = requests.Session()
for c in cookies:
    s.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path","/"))
s.headers.update({"User-Agent":"Mozilla/5.0"})

goods_no="1000000047"
detail_url=f"https://m.cmtstory.com/goods/goods_view.php?goodsNo={goods_no}"

payload={
    "type":"goods",
    "goodsNo":goods_no,
    "currentPageUrl": double_urlencode(detail_url)
}

headers={
    "User-Agent": "Mozilla/5.0",
    "Referer": detail_url,
    "Origin": "https://m.cmtstory.com",
    "X-Requested-With": "XMLHttpRequest",
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
}

r=s.post("https://m.cmtstory.com/goods/layer_option.php", data=payload, headers=headers)
print("status:", r.status_code)
print("preview:", r.text[:300])

if r.status_code == 200:
    soup=BeautifulSoup(r.text,"lxml")
    print("set_goods_price:", soup.select_one("#set_goods_price")["value"])
    print("set_goods_fixedPrice:", soup.select_one("#set_goods_fixedPrice")["value"])