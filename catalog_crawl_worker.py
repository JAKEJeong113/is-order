# catalog_crawl_worker.py
"""도매처 하나의 전체상품 크롤링을 별도 프로세스에서 실행하는 워커.

catalog_crawler.crawl_vendor가 이 프로세스를 띄우고, 제한시간을 넘기면
프로세스 그룹째(Chromium 자식 포함) 강제 종료한다 - 스레드는 중간에 죽일
방법이 없어서, 예전엔 타임아웃 난 크롤링이 브라우저를 쥔 채 서버 안에
계속 살아남아 메모리가 쌓였다. 종료되면 이 프로세스가 쓴 메모리도 전부
운영체제로 돌아간다.

사용법: python -m catalog_crawl_worker <vendor_id> <결과를 쓸 json 경로>"""
import json
import sys

import vendors
from catalog_crawler import _crawl_vendor_products


def main() -> int:
    vendor_id, out_path = sys.argv[1], sys.argv[2]
    creds = vendors.get_vendor_credentials(vendor_id)
    if not creds:
        print("계정 정보 없음", file=sys.stderr)
        return 2
    login_id, login_pwd = creds
    products = _crawl_vendor_products(vendor_id, vendors.VENDORS[vendor_id], login_id, login_pwd)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
