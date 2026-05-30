# partners_test.py
import hmac
import hashlib
import json
import os
from datetime import datetime, timezone
from urllib.parse import quote

import requests

ACCESS_KEY = os.environ["CP_ACCESS_KEY"]
SECRET_KEY = os.environ["CP_SECRET_KEY"]

DOMAIN = "https://api-gateway.coupang.com"
METHOD = "POST"
PATH = "/v2/providers/affiliate_open_api/apis/openapi/deeplink"


def make_signed_date() -> str:
    return datetime.now(timezone.utc).strftime("%y%m%dT%H%M%SZ")


def make_authorization(method: str, path: str, query: str, access_key: str, secret_key: str) -> str:
    signed_date = make_signed_date()
    message = f"{signed_date}{method}{path}{query}"
    signature = hmac.new(
        secret_key.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return (
        f"CEA algorithm=HmacSHA256, "
        f"access-key={access_key}, "
        f"signed-date={signed_date}, "
        f"signature={signature}"
    )


def build_coupang_search_url(keyword: str) -> str:
    q = quote(keyword)
    return f"https://www.coupang.com/np/search?component=&q={q}&channel=user"


def create_deeplink_from_urls(coupang_urls: list[str]) -> dict:
    authorization = make_authorization(
        method=METHOD,
        path=PATH,
        query="",
        access_key=ACCESS_KEY,
        secret_key=SECRET_KEY,
    )

    url = f"{DOMAIN}{PATH}"
    payload = {"coupangUrls": coupang_urls}

    resp = requests.post(
        url,
        headers={
            "Authorization": authorization,
            "Content-Type": "application/json",
        },
        data=json.dumps(payload),
        timeout=30,
    )

    print("status_code =", resp.status_code)
    print(resp.text)
    resp.raise_for_status()
    return resp.json()


def create_deeplink_from_search_keyword(keyword: str) -> dict:
    source_url = build_coupang_search_url(keyword)
    print("source_url =", source_url)
    return create_deeplink_from_urls([source_url])


if __name__ == "__main__":
    keyword = "메로나 아이스크림"
    result = create_deeplink_from_search_keyword(keyword)

    print("\nparsed_result:")
    print(json.dumps(result, ensure_ascii=False, indent=2))