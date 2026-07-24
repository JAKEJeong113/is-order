# store_reports.py
"""지점별 "자동 메시지 예약" - 오더퀸 판매수량을 예약한 요일/시각에 집계해서
텔레그램으로 발주 리스트(도매처 담기 대상 + 쿠팡 구매링크 + 아이스크림 참고)를
보낸다. 1타(pack_qty/icecream_box_qty) 대비 60% 이상 팔린 상품만 추천하고,
미달로 빠진 도매처 품목은 다음 집계 때 이월(carryover)해서 계속 더한다."""
import json
import math
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import cafe24_bot
import catalog_cache
import db_conn
import godomall_bot
import mapping
import popularity
import price_compare
import product_ranking
import vendors
from orderqueen_bot import download_orderqueen_xlsx_with_retry as download_orderqueen_xlsx
from parser import parse_menu_sales_xlsx

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"

CASE_ORDER_THRESHOLD = 0.6
DAY_NAMES = ["월", "화", "수", "목", "금", "토", "일"]

POPULARITY_CATEGORY_BY_IS_COUPANG = {0: "icecream", 1: "coupang", 2: "wholesale"}


def get_conn():
    return db_conn.get_conn()


def init_store_report_tables() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS store_report_schedules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        store_id TEXT NOT NULL,
        day_of_week INTEGER NOT NULL,
        time TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        last_fired_date TEXT,
        created_at TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS store_report_state (
        store_id TEXT PRIMARY KEY,
        last_aggregated_until TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS store_item_carryover (
        store_id TEXT NOT NULL,
        item_key TEXT NOT NULL,
        category TEXT,
        carried_qty INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT,
        PRIMARY KEY (store_id, item_key)
    )
    """)
    conn.commit()

    # 다점포 점주 지원: 지점(오더퀸 계정)마다 스케줄을 따로 잡을 수 있게
    # account_id(store_vendor_credentials.id, orderqueen 계정)를 nullable로 추가.
    # NULL이면 기본 계정(기존 단일 지점 동작 그대로) 취급.
    existing_cols = {row[1] for row in cur.execute("PRAGMA table_info(store_report_schedules)").fetchall()}
    if "account_id" not in existing_cols:
        cur.execute("ALTER TABLE store_report_schedules ADD COLUMN account_id INTEGER")
        conn.commit()
    conn.close()


def init_manual_report_table() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS store_manual_reports (
        store_id TEXT PRIMARY KEY,
        generated_at TEXT,
        period_from TEXT,
        period_to TEXT,
        safety_stock INTEGER,
        report_json TEXT
    )
    """)
    conn.commit()
    conn.close()


def save_manual_report(
    store_id: str, account_id: int | None, period_from: str, period_to: str, safety_stock: int, report: dict,
) -> str:
    """/order 페이지를 벗어나도 마지막으로 수동 생성한 발주표를 이어볼 수
    있도록 지점(계정)당 최신 1건만 저장한다(이력 보관이 목적이 아니라
    "새로고침해도 안 없어지게"가 목적이라 UPSERT). account_id로 report_key를
    계산해써야 다점포 점주가 지점을 바꿔 생성해도 서로 덮어쓰지 않는다."""
    key = report_key(store_id, account_id)
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO store_manual_reports (store_id, generated_at, period_from, period_to, safety_stock, report_json)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(store_id) DO UPDATE SET
        generated_at=excluded.generated_at, period_from=excluded.period_from, period_to=excluded.period_to,
        safety_stock=excluded.safety_stock, report_json=excluded.report_json
    """, (key, now, period_from, period_to, safety_stock, json.dumps(report, ensure_ascii=False)))
    conn.commit()
    conn.close()
    return now


def get_manual_report(store_id: str, account_id: int | None = None) -> dict | None:
    key = report_key(store_id, account_id)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT generated_at, period_from, period_to, safety_stock, report_json
    FROM store_manual_reports WHERE store_id = ?
    """, (key,))
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "generated_at": row[0], "period_from": row[1], "period_to": row[2],
        "safety_stock": row[3], "report": json.loads(row[4]),
    }


def report_key(store_id: str, account_id: int | None) -> str:
    """store_report_state/store_item_carryover는 지점(오더퀸 계정)별로 서로
    다른 판매 데이터를 다루므로 커서/이월을 따로 추적해야 한다 - account_id가
    있으면 store_id에 붙여 별도 키로 만든다(없으면 기존 단일 지점 키 그대로)."""
    if account_id is None:
        return store_id
    return f"{store_id}::acct{account_id}"


# --- 1타 60% 규칙 ---

def apply_case_rule(sold_qty: float, pack_qty: int) -> int:
    """판매수량이 1타 개수의 60% 이상이면 반올림한 타 수를 돌려준다.
    60% 미달이거나 1타 개수를 모르면 0(발주 대상 아님)."""
    if not pack_qty or pack_qty <= 0:
        return 0
    ratio = sold_qty / pack_qty
    if ratio < CASE_ORDER_THRESHOLD:
        return 0
    return max(1, math.floor(ratio + 0.5))


def _pick_best_offer(offers: list[dict], preferred_vendor: str | None) -> dict | None:
    """telegram_bot._pick_best_offer와 비슷하지만(순환 import를 피하려고 복제),
    1타 개수(unit_qty)를 아는 후보를 우선한다 - 도매몰 대부분(현동몰/또요몰/
    삼봉몰/무마켓/과자생각)은 아직 크롤러가 1타 개수를 거의 못 읽어와서, 모르는
    채로 고르면 이후 apply_case_rule()에서 1타=1개로 잘못 가정하는 사고가 난다.
    CART_SUPPORTED_VENDORS 중 담을 수 있는 후보만 놓고(비활성화한 도매처는
    호출부에서 이미 걸러짐 - filter_groups_for_store), 그중 1타 개수를 아는
    것을 우선 최저가순으로, 동률이면 주거래처 우선한다. 활성화된 도매처 중
    실제로 더 싼 곳이 있으면 그쪽을 안내하고, 가격이 같거나 주 도매처가 이미
    최저가일 때만 주 도매처로 고정한다(2026-07-24: 가격 무관 강제 고정으로
    바꿨다가, 활성화된 도매처 중 최저가를 반영해야 한다는 피드백을 받아
    되돌림)."""
    candidates = [o for o in offers if o["vendor_id"] in vendors.CART_SUPPORTED_VENDORS and o.get("product_url")]
    if not candidates:
        return None
    known_qty_candidates = [o for o in candidates if o.get("unit_qty") and o["unit_qty"] > 0]
    pool = known_qty_candidates or candidates
    lowest_unit_price = price_compare._unit_price(pool[0])
    tied = [o for o in pool if price_compare._unit_price(o) == lowest_unit_price]
    if preferred_vendor:
        for o in tied:
            if o["vendor_id"] == preferred_vendor:
                return o
    return pool[0]  # price_compare.compare가 이미 개당가 오름차순으로 정렬해둠


# 목록 크롤링으로는 1타 개수를 거의 못 읽어오는 도매처(현동몰/무마켓)만 지원한다.
# 지원하는 도매처는 상세페이지에서 실제 발주 리포트에 뜬 상품만 그때그때 보충
# 조회하고 결과를 캐시에 저장해둔다(catalog_cache.update_unit_qty) - 카탈로그
# 전체를 상세페이지까지 크롤링하면 상품 수천 개 × 페이지 이동이라 너무 느리고
# 타임아웃 위험도 커서, 실제로 필요한 만큼만 그때그때 채운다.
_UNIT_QTY_DETAIL_FETCHERS = {
    "hdinter": godomall_bot.fetch_unit_qty_from_detail_page,
    "moomarket": cafe24_bot.fetch_unit_qty_from_detail_page,
}


def _fetch_missing_unit_qty(vendor_id: str, product_url: str) -> int | None:
    fetcher = _UNIT_QTY_DETAIL_FETCHERS.get(vendor_id)
    if not fetcher or not product_url:
        return None
    creds = vendors.get_vendor_credentials(vendor_id)
    if not creds:
        return None
    login_id, login_pwd = creds
    base_url = vendors.VENDORS[vendor_id]["base_url"]
    try:
        unit_qty = fetcher(base_url, login_id, login_pwd, product_url)
    except Exception as e:
        print(f"[STORE_REPORTS] {vendor_id} 상세페이지 1타 보충 조회 실패 ({product_url}):", e)
        return None
    if unit_qty and unit_qty > 0:
        catalog_cache.update_unit_qty(vendor_id, product_url, unit_qty)
    return unit_qty


# --- 스케줄 CRUD ---

def add_schedule(store_id: str, day_of_week: int, time_str: str, account_id: int | None = None) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO store_report_schedules (store_id, day_of_week, time, enabled, account_id, created_at)
    VALUES (?, ?, ?, 1, ?, ?) RETURNING id
    """, (store_id, day_of_week, time_str, account_id, now))
    conn.commit()
    new_id = cur.fetchone()[0]
    conn.close()
    return new_id


def list_schedules(store_id: str) -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT id, day_of_week, time, enabled, last_fired_date, account_id
    FROM store_report_schedules WHERE store_id = ? ORDER BY day_of_week ASC, time ASC
    """, (store_id,))
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "id": r[0], "day_of_week": r[1], "day_name": DAY_NAMES[r[1]], "time": r[2],
            "enabled": bool(r[3]), "last_fired_date": r[4], "account_id": r[5],
        }
        for r in rows
    ]


def delete_schedule(store_id: str, schedule_id: int) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM store_report_schedules WHERE id = ? AND store_id = ?", (schedule_id, store_id))
    deleted = cur.rowcount > 0
    conn.commit()
    conn.close()
    return deleted


def set_schedule_enabled(store_id: str, schedule_id: int, enabled: bool) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE store_report_schedules SET enabled = ? WHERE id = ? AND store_id = ?",
        (1 if enabled else 0, schedule_id, store_id),
    )
    updated = cur.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def list_due_schedules(now: datetime, window_minutes: int = 15) -> list[dict]:
    """지금 실행돼야 할 예약들 - 오늘 요일과 일치하고, 예약 시각이 지금부터
    window_minutes분 이내로 지났고, 오늘 아직 발송 안 한 것만."""
    today_str = now.date().isoformat()
    dow = now.weekday()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT id, store_id, time, account_id FROM store_report_schedules
    WHERE enabled = 1 AND day_of_week = ? AND (last_fired_date IS NULL OR last_fired_date != ?)
    """, (dow, today_str))
    rows = cur.fetchall()
    conn.close()

    due = []
    for schedule_id, store_id, time_str, account_id in rows:
        try:
            hh, mm = (int(x) for x in time_str.split(":"))
        except ValueError:
            continue
        sched_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if sched_dt <= now < sched_dt + timedelta(minutes=window_minutes):
            due.append({"id": schedule_id, "store_id": store_id, "time": time_str, "account_id": account_id})
    return due


def mark_schedule_fired(schedule_id: int, fired_date: str) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE store_report_schedules SET last_fired_date = ? WHERE id = ?", (fired_date, schedule_id))
    conn.commit()
    conn.close()


# --- 집계 기간 커서(같은 판매데이터 중복 집계 방지) ---

def get_last_aggregated_until(store_id: str) -> date:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT last_aggregated_until FROM store_report_state WHERE store_id = ?", (store_id,))
    row = cur.fetchone()
    conn.close()
    if row and row[0]:
        return date.fromisoformat(row[0])
    return date.today() - timedelta(days=7)


def set_last_aggregated_until(store_id: str, until: date) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO store_report_state (store_id, last_aggregated_until) VALUES (?, ?)
    ON CONFLICT(store_id) DO UPDATE SET last_aggregated_until = excluded.last_aggregated_until
    """, (store_id, until.isoformat()))
    conn.commit()
    conn.close()


# --- 이월(캐리오버): "아직 실제 발주로 이어지지 않은 누적 판매량" ---

def get_carryover(store_id: str, item_key: str) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT carried_qty FROM store_item_carryover WHERE store_id = ? AND item_key = ?",
        (store_id, item_key),
    )
    row = cur.fetchone()
    conn.close()
    return row[0] if row else 0


def set_carryover(store_id: str, item_key: str, category: str, carried_qty: int) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO store_item_carryover (store_id, item_key, category, carried_qty, updated_at)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(store_id, item_key) DO UPDATE SET
        carried_qty = excluded.carried_qty, updated_at = excluded.updated_at
    """, (store_id, item_key, category, max(0, int(carried_qty)), now))
    conn.commit()
    conn.close()


def apply_manual_pull_carryover(
    store_id: str, account_id: int | None, period_from: date, period_to: date, items: list[dict],
) -> None:
    """예약 주기를 기다리지 않고 사용자가 /order에서 수동으로 판매 데이터를
    불러왔을 때도 도매몰/아이스크림 판매량이 자동 리포트의 이월에서 누락되지
    않게 반영한다. items는 {"item_key", "qty", "category"}(category는
    "wholesale" 또는 "icecream") - 두 카테고리를 한 번에 넘겨야 한다, 커서
    전진(set_last_aggregated_until)이 호출당 한 번뿐이라 카테고리별로 따로
    부르면 두 번째 호출이 "이미 반영된 구간"으로 오판해 조용히 건너뛴다.

    account_id로 report_key를 계산해써야 generate_report()가 쓰는 것과 같은
    이월/커서 버킷을 공유한다 - 안 그러면 다점포 점주가 지점을 바꿔가며 수동
    조회할 때 서로 다른 지점의 판매량이 한 버킷에 섞여버린다.

    period_from이 마지막 자동 집계 커서 다음날보다 이후(갭 발생)면 커서를 건드리지
    않는다 - 그 갭 구간은 다음 자동 리포트가 [커서+1 ~ 오늘]을 다시 훑을 때 정상
    포함되므로, 여기서 섣불리 커서를 전진시켜 그 구간을 영영 건너뛰게 만들면 안 된다."""
    key = report_key(store_id, account_id)
    cursor = get_last_aggregated_until(key)
    if period_from > cursor + timedelta(days=1) or period_to <= cursor:
        return
    for item in items:
        item_key = item.get("item_key")
        qty = int(item.get("qty", 0) or 0)
        category = item.get("category", "wholesale")
        if not item_key or qty <= 0:
            continue
        carried = get_carryover(key, item_key)
        set_carryover(key, item_key, category, carried + qty)
    set_last_aggregated_until(key, period_to)


def resolve_carryover_after_reply(key: str, item: dict, outcome: str, final_qty: int | None = None) -> None:
    """텔레그램 확인/스킵/제외 응답 이후 도매처 품목의 이월값을 정산한다.
    outcome="confirmed": 실제 담은 만큼 소진하고 잔여만 이월.
    outcome="skipped": 이번엔 안 담았으므로 pending_qty 그대로 이월.
    key는 report_key(store_id, account_id) - 지점(오더퀸 계정)별로 분리된 값이다."""
    pending_qty = int(item.get("pending_qty", 0))
    pack_qty = int(item.get("pack_qty", 1) or 1)
    item_key = item["item_key"]
    category = item.get("category", "wholesale")

    if outcome == "confirmed":
        qty = final_qty if final_qty is not None else int(item.get("cases", 0))
        remainder = max(0, pending_qty - qty * pack_qty)
        set_carryover(key, item_key, category, remainder)
    else:
        set_carryover(key, item_key, category, pending_qty)


# --- 리포트 생성 ---

def _classify_report_items(
    store_id: str, key: str, top_items: list[dict], catalog: dict,
    disabled_vendors: set, preferred_vendor: str | None,
    compute_wholesale_pending_qty, compute_icecream_pending_qty,
    write_carryover_on_reject: bool, log_popularity: bool,
) -> dict:
    """오더퀸 판매 데이터(top_items)를 카테고리별로 분류하는 공용 로직 - 예약
    자동 리포트(generate_report)와 수동 발송(build_manual_wholesale_report)이
    똑같이 쓴다. 도매몰/아이스크림 둘 다 pending_qty(이월+판매량)를 콜백으로
    계산한다:
    - 자동: 이월값 + 이번 집계 판매량을 새로 더한다.
    - 수동 발송: apply_manual_pull_carryover()가 이미 이번 판매량을 이월에
      합산해둔 뒤라(같은 /import/orderqueen 호출 안에서 먼저 실행됨), 현재
      이월값을 그대로 쓴다 - 안 그러면 판매량이 중복 집계된다.

    아이스크림은 도매몰과 달리 텔레그램 확인/스킵 절차가 없는 정보성 리포트라
    - 리스트에 뜨는 순간(60% 이상) 사용자가 그 즉시 전부 발주했다고 간주하고
    이월을 바로 0으로 소진한다(도매몰처럼 별도 확인 응답을 기다리지 않음).
    60% 미달이면 도매몰과 동일하게 pending_qty를 그대로 이월한다."""
    wholesale_items = []
    coupang_items = []
    icecream_items = []
    other_items = []
    unknown_pack_items = []

    for row in top_items:
        barcode = str(row.get("바코드번호", "") or "").strip().replace(".0", "")
        name = str(row.get("메뉴명", "") or "").strip()
        sold_qty = int(row.get("판매수량", 0) or 0)
        if sold_qty <= 0 or not name:
            continue

        cat = catalog.get(barcode) if barcode else None
        is_coupang = int(cat.is_coupang) if cat else 99
        item_key = barcode or name

        if log_popularity:
            pop_category = POPULARITY_CATEGORY_BY_IS_COUPANG.get(is_coupang)
            if pop_category:
                popularity.log_event(store_id, pop_category, item_key, name, sold_qty)

        if is_coupang == 0:
            box_qty = int(getattr(cat, "icecream_box_qty", 0) or 0) if cat else 0
            pending_qty = compute_icecream_pending_qty(item_key, sold_qty)
            cases = apply_case_rule(pending_qty, box_qty)
            if cases > 0:
                icecream_items.append({
                    "item_key": item_key, "name": name, "sold_qty": sold_qty,
                    "pending_qty": pending_qty, "box_qty": box_qty, "cases": cases,
                })
                # 확인/스킵 절차가 없으므로 리스트에 뜨면 즉시 전부 발주된 것으로
                # 간주하고 이월을 소진한다.
                set_carryover(key, item_key, "icecream", 0)
            elif write_carryover_on_reject:
                set_carryover(key, item_key, "icecream", pending_qty)
        elif is_coupang == 1:
            keyword = (cat.search_keyword or cat.menu_name or name) if cat else name
            results = product_ranking.search_products(keyword, limit=1)
            if results:
                r = results[0]
                coupang_items.append({
                    "item_key": item_key, "name": name, "sold_qty": sold_qty,
                    "price": r.get("price"), "partners_link": r.get("partners_link"),
                })
        elif is_coupang == 2:
            pending_qty = compute_wholesale_pending_qty(item_key, sold_qty)
            if pending_qty <= 0:
                continue
            keyword = (cat.search_keyword or cat.menu_name or name) if cat else name

            compare_result = price_compare.compare(keyword)
            groups = price_compare.filter_groups_for_store(compare_result.get("groups", []), disabled_vendors)
            offer = None
            for group in groups:
                offer = _pick_best_offer(group.get("offers", []), preferred_vendor)
                if offer:
                    break

            if not offer:
                if write_carryover_on_reject:
                    set_carryover(key, item_key, "wholesale", pending_qty)
                continue

            pack_qty = int(offer.get("unit_qty") or 0)
            if pack_qty <= 0:
                # 목록 크롤링으로는 1타 개수를 못 읽어온 상품 - 여기서 1개입으로
                # 잘못 가정하면 "판매수량 = 타수"로 엉뚱한 발주 추천이 나간다.
                # 지원하는 도매처(현동몰/무마켓)는 실제로 이 리포트에 뜬 상품에
                # 한해서만 상세페이지를 한 번 더 조회해 채워본다.
                pack_qty = _fetch_missing_unit_qty(offer["vendor_id"], offer.get("product_url")) or 0

            if pack_qty <= 0:
                # 그래도 못 채웠으면(지원 안 하는 도매처거나 상세페이지 조회도
                # 실패) 케이스 수를 추정하지 않고 참고용으로만 보여주고, 판매량은
                # 이월에 그대로 남겨서 나중에 1타 개수가 확인되면 정상 반영되게 한다.
                if write_carryover_on_reject:
                    set_carryover(key, item_key, "wholesale", pending_qty)
                unknown_pack_items.append({
                    "item_key": item_key, "name": name, "sold_qty": sold_qty,
                    "vendor_name": offer.get("vendor_name", offer["vendor_id"]),
                })
                continue

            cases = apply_case_rule(pending_qty, pack_qty)
            if cases <= 0:
                if write_carryover_on_reject:
                    set_carryover(key, item_key, "wholesale", pending_qty)
                continue

            wholesale_items.append({
                "item_key": item_key, "name": name, "sold_qty": sold_qty,
                "pending_qty": pending_qty, "pack_qty": pack_qty, "cases": cases,
                "vendor_id": offer["vendor_id"], "vendor_name": offer.get("vendor_name", offer["vendor_id"]),
                "product_url": offer.get("product_url"), "category": "wholesale",
            })
            # 이월값은 여기서 갱신하지 않는다 - 텔레그램 확인/스킵 결과가 나온 뒤
            # resolve_carryover_after_reply()가 확정한다.
        else:
            if is_coupang == 99 and barcode:
                mapping.queue_unclassified_item(barcode, name, store_id)
            other_items.append({"item_key": item_key, "name": name, "sold_qty": sold_qty})

    return {
        "wholesale_items": wholesale_items,
        "coupang_items": coupang_items,
        "icecream_items": icecream_items,
        "other_items": other_items,
        "unknown_pack_items": unknown_pack_items,
    }


def build_manual_wholesale_report(store_id: str, account_id: int | None, top_items: list[dict]) -> dict:
    """웹에서 수동으로 불러온 오더퀸 판매 데이터(top_items)를 텔레그램
    확인/스킵/수정 가능한 리포트로 재구성한다. apply_manual_pull_carryover()가
    이미 이번 판매량을 이월에 합산해둔 뒤라(같은 /import/orderqueen 호출에서
    먼저 실행됨), 현재 이월값을 그대로 pending_qty로 쓰고 별도로 더하지 않는다."""
    key = report_key(store_id, account_id)
    catalog = mapping.load_catalog()
    disabled_vendors, preferred_vendor = vendors.get_store_vendor_prefs(store_id)
    account = vendors.resolve_store_vendor_account(store_id, "orderqueen", account_id)

    classified = _classify_report_items(
        store_id, key, top_items, catalog, disabled_vendors, preferred_vendor,
        compute_wholesale_pending_qty=lambda item_key, sold_qty: get_carryover(key, item_key),
        compute_icecream_pending_qty=lambda item_key, sold_qty: get_carryover(key, item_key),
        write_carryover_on_reject=False,
        log_popularity=False,
    )
    return {
        "ok": True,
        "account_id": account_id,
        "account_nickname": account["nickname"] if account else None,
        "report_key": key,
        **classified,
    }


def generate_report(store_id: str, account_id: int | None = None) -> dict:
    creds = vendors.get_store_vendor_credentials(store_id, "orderqueen", account_id)
    if not creds:
        return {"ok": False, "reason": "오더퀸 계정이 등록되어 있지 않습니다. '내 도매처 계정'에서 먼저 등록해주세요."}
    login_id, login_pw = creds
    account = vendors.resolve_store_vendor_account(store_id, "orderqueen", account_id)
    account_nickname = account["nickname"] if account else None

    key = report_key(store_id, account_id)
    period_from = get_last_aggregated_until(key) + timedelta(days=1)
    period_to = date.today()
    if period_from > period_to:
        return {"ok": False, "reason": "집계할 새 판매 데이터가 아직 없습니다."}

    job_id = uuid.uuid4().hex[:8]
    sales_xlsx_path = str(DOWNLOAD_DIR / f"report_{job_id}.xlsx")
    try:
        download_orderqueen_xlsx(
            login_id=login_id, login_pw=login_pw,
            period_from=period_from, period_to=period_to,
            save_path=sales_xlsx_path,
        )
        _, _, top_items = parse_menu_sales_xlsx(sales_xlsx_path, period_from, period_to)
    except Exception as e:
        return {"ok": False, "reason": f"오더퀸 판매 데이터 조회 실패: {e}"}

    catalog = mapping.load_catalog()
    disabled_vendors, preferred_vendor = vendors.get_store_vendor_prefs(store_id)

    classified = _classify_report_items(
        store_id, key, top_items, catalog, disabled_vendors, preferred_vendor,
        compute_wholesale_pending_qty=lambda item_key, sold_qty: get_carryover(key, item_key) + sold_qty,
        compute_icecream_pending_qty=lambda item_key, sold_qty: get_carryover(key, item_key) + sold_qty,
        write_carryover_on_reject=True,
        log_popularity=True,
    )

    set_last_aggregated_until(key, period_to)

    return {
        "ok": True,
        "period_from": period_from.isoformat(),
        "period_to": period_to.isoformat(),
        "account_id": account_id,
        "account_nickname": account_nickname,
        "report_key": key,
        **classified,
    }
