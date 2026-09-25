# icemoa_import.py
"""아이스모아(icemoa.com) - 아이스크림 바코드/가격 참고사이트에서 상품 목록을
가져와 catalog_items(is_coupang=0, 아이스크림)에 반영한다.

로그인이 필요한 도매몰(catalog_auto_import.py)과 달리, 이 사이트는 브라우저
헤더만 흉내내면 공개 데이터 페이지(/im/im_data/data.html)를 그대로 받을 수
있는 정적 조회 사이트다(실측: User-Agent/Referer 없이 requests로 바로 받으면
403 - 확인 후 헤더 추가로 해결). 로그인/장바구니 없이 페이지 하나에 전체
상품이 표(<table data-id="companyA">~"companyF">)로 나뉘어 있고, "가격"
컬럼 자체가 이미 소비자 판매가라(도매 사입가가 아님) catalog_auto_import.py
처럼 박스가÷개수로 마진을 계산할 필요 없이 그대로 recommended_price로 쓴다.

병합 규칙(catalog_auto_import.py와 같은 철학 - 사용자 확인된 정책 재사용):
  - DB에 없는 바코드 -> 새로 추가(is_coupang=0)
  - 있는데 recommended_price가 비어있음(0/NULL) -> 그 값만 채움
  - 있고 값도 있는데 이번 값과 다르면 -> 이 사이트가 "가격 정보" 그 자체가
    목적이라 명시가로 취급해 덮어씀 + 가격인상안내용 기록(mapping.record_price_change)
  - 값이 같으면 안 건드림(불필요한 updated_at 갱신 방지)
menu_name/search_keyword 등 관리자가 손댔을 수 있는 다른 필드는 신규 추가 때만
채우고, 이미 있는 상품은 절대 덮어쓰지 않는다.

"companyF"(Sold out/단종) 표는 대상에서 제외한다 - 더 이상 파는 상품이 아니라
가격 정보 자체가 의미 없다.

같은 바코드가 "신제품" 표와 브랜드별 표에 동시에 걸리면서 가격이 다른 경우가
실측으로 확인됐다(예: 8809713222578 고구마루바 - 신제품 표엔 갱신 전 가격
600원이 남아있고 해태 표엔 갱신된 1200원) - catalog_auto_import.py가 여러
도매처 가격이 겹칠 때 쓰는 규칙(더 비싼 쪽 채택)을 그대로 재사용해서 해결한다.

실행: python icemoa_import.py
"""
from datetime import datetime

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

import db_conn
import mapping
from godomall_bot import _validate_barcode_checksum

DATA_URL = "https://icemoa.com/im/im_data/data.html"
CATEGORY_ICECREAM = 0  # main.py의 is_coupang 정의: 0=아이스크림
EXCLUDED_TABLE_IDS = {"companyF"}  # "Sold out/단종" 표

# 직접 requests로 받으면 403(Forbidden)이 뜬다 - Referer/User-Agent가 없는
# 요청을 막아두는 것으로 보여 브라우저처럼 흉내낸다(실측 확인).
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://icemoa.com/",
}


def _fetch_rows() -> list[dict]:
    """data.html의 표(단종 제외)를 전부 파싱해서 상품 후보 리스트로 돌려준다.
    체크섬이 안 맞는 바코드(실측: 자리표시용 "8809713-000000" 같은 값)는
    걸러낸다."""
    resp = requests.get(DATA_URL, headers=_HEADERS, timeout=20)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")

    rows = []
    for table in soup.find_all("table"):
        if table.get("data-id") in EXCLUDED_TABLE_IDS:
            continue
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue
            name = tds[0].get_text(strip=True)
            price_text = tds[1].get_text(strip=True)
            ea_text = tds[2].get_text(strip=True)
            barcode = tds[3].get_text(strip=True).replace("-", "")
            if not name or not price_text.isdigit():
                continue
            if not _validate_barcode_checksum(barcode):
                continue
            rows.append({
                "barcode": barcode,
                "name": name,
                "price": int(price_text),
                "box_qty": int(ea_text) if ea_text.isdigit() else 0,
            })
    return rows


def _pick_winner(candidates: list[dict]) -> dict:
    """같은 바코드가 여러 표에 겹치면 더 비싼 쪽을 채택한다(모듈 설명 참고)."""
    return max(candidates, key=lambda c: c["price"])


def _get_existing_price(barcode: str) -> int | None:
    conn = db_conn.get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT recommended_price FROM catalog_items WHERE barcode = ?", (barcode,))
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _insert_new(barcode: str, name: str, price: int, box_qty: int, notes: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn = db_conn.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO catalog_items
                (barcode, menu_name, search_keyword, fixed_url, pack_qty, min_order, notes,
                 is_coupang, icecream_box_qty, category, menu_code, recommended_price, updated_at)
            VALUES (?, ?, '', '', 1, 1, ?, ?, ?, '', '', ?, ?)
            """,
            (barcode, name, notes, CATEGORY_ICECREAM, box_qty, price, now),
        )
        conn.commit()
    finally:
        conn.close()


def _fill_empty_price(barcode: str, price: int, box_qty: int, notes: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn = db_conn.get_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE catalog_items
            SET recommended_price = ?, icecream_box_qty = ?, notes = ?, updated_at = ?
            WHERE barcode = ? AND (recommended_price IS NULL OR recommended_price = 0)
            """,
            (price, box_qty, notes, now, barcode),
        )
        conn.commit()
    finally:
        conn.close()


def _overwrite_price(barcode: str, price: int, notes: str) -> None:
    """가격인상안내(바코드 사이트)용 - 덮어쓰기 전에 기존 가격을 먼저 봐서
    실제로 오른 경우만 mapping.record_price_change가 기록한다."""
    now = datetime.now().isoformat(timespec="seconds")
    conn = db_conn.get_conn()
    try:
        cur = conn.cursor()
        cur.execute("SELECT recommended_price FROM catalog_items WHERE barcode = ?", (barcode,))
        row = cur.fetchone()
        old_price = row[0] if row else None
        cur.execute(
            "UPDATE catalog_items SET recommended_price = ?, notes = ?, updated_at = ? WHERE barcode = ?",
            (price, notes, now, barcode),
        )
        conn.commit()
    finally:
        conn.close()
    mapping.record_price_change(barcode, old_price, price)


def import_icemoa_catalog() -> dict:
    """전체 실행 - main.py 스케줄러에서 주기 호출한다."""
    rows = _fetch_rows()
    by_barcode: dict[str, list[dict]] = {}
    for r in rows:
        by_barcode.setdefault(r["barcode"], []).append(r)

    summary: dict[str, list[dict]] = {"added": [], "updated": [], "overwritten": [], "skipped": []}
    today = datetime.now().date().isoformat()

    for barcode, candidates in by_barcode.items():
        winner = _pick_winner(candidates)
        existing_price = _get_existing_price(barcode)

        if existing_price is None:
            notes = f"아이스모아 자동크롤링 · {today}"
            _insert_new(barcode, winner["name"], winner["price"], winner["box_qty"], notes)
            summary["added"].append({"barcode": barcode, "name": winner["name"], "price": winner["price"]})
        elif existing_price == 0:
            notes = f"아이스모아 자동크롤링 · {today}"
            _fill_empty_price(barcode, winner["price"], winner["box_qty"], notes)
            summary["updated"].append({"barcode": barcode, "name": winner["name"], "price": winner["price"]})
        elif existing_price != winner["price"]:
            notes = f"아이스모아 가격 갱신(기존 {existing_price}원 → {winner['price']}원) · {today}"
            _overwrite_price(barcode, winner["price"], notes)
            summary["overwritten"].append({
                "barcode": barcode, "name": winner["name"],
                "old_price": existing_price, "new_price": winner["price"],
            })
        else:
            summary["skipped"].append(barcode)

    return summary


if __name__ == "__main__":
    result = import_icemoa_catalog()
    print(
        f"아이스모아 카탈로그 반영 완료: 신규 {len(result['added'])}개, "
        f"빈 값 채움 {len(result['updated'])}개, 가격 갱신 {len(result['overwritten'])}개, "
        f"변동없음 {len(result['skipped'])}개"
    )
