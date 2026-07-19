# store_reports.py
"""지점별 "자동 메시지 예약" - 오더퀸 판매수량을 예약한 요일/시각에 집계해서
텔레그램으로 발주 리스트(도매처 담기 대상 + 쿠팡 구매링크 + 아이스크림 참고)를
보낸다. 1타(pack_qty/icecream_box_qty) 대비 60% 이상 팔린 상품만 추천하고,
미달로 빠진 도매처 품목은 다음 집계 때 이월(carryover)해서 계속 더한다."""
import math
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import db_conn
import mapping
import popularity
import price_compare
import product_ranking
import vendors
from orderqueen_bot import download_orderqueen_xlsx
from parser import parse_menu_sales_xlsx

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
COUPANG_CATALOG_XLSX_PATH = BASE_DIR / "coupang_catalog_sample_2.xlsx"

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
    conn.close()


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
    """telegram_bot._pick_best_offer와 동일한 로직(순환 import를 피하려고 복제) -
    CART_SUPPORTED_VENDORS 중 담을 수 있는 후보만 놓고 개당 최저가 우선,
    동률이면 주거래처 우선."""
    candidates = [o for o in offers if o["vendor_id"] in vendors.CART_SUPPORTED_VENDORS and o.get("product_url")]
    if not candidates:
        return None
    lowest_unit_price = price_compare._unit_price(candidates[0])
    tied = [o for o in candidates if price_compare._unit_price(o) == lowest_unit_price]
    if preferred_vendor:
        for o in tied:
            if o["vendor_id"] == preferred_vendor:
                return o
    return candidates[0]


# --- 스케줄 CRUD ---

def add_schedule(store_id: str, day_of_week: int, time_str: str) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO store_report_schedules (store_id, day_of_week, time, enabled, created_at)
    VALUES (?, ?, ?, 1, ?) RETURNING id
    """, (store_id, day_of_week, time_str, now))
    conn.commit()
    new_id = cur.fetchone()[0]
    conn.close()
    return new_id


def list_schedules(store_id: str) -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT id, day_of_week, time, enabled, last_fired_date
    FROM store_report_schedules WHERE store_id = ? ORDER BY day_of_week ASC, time ASC
    """, (store_id,))
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "id": r[0], "day_of_week": r[1], "day_name": DAY_NAMES[r[1]], "time": r[2],
            "enabled": bool(r[3]), "last_fired_date": r[4],
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
    SELECT id, store_id, time FROM store_report_schedules
    WHERE enabled = 1 AND day_of_week = ? AND (last_fired_date IS NULL OR last_fired_date != ?)
    """, (dow, today_str))
    rows = cur.fetchall()
    conn.close()

    due = []
    for schedule_id, store_id, time_str in rows:
        try:
            hh, mm = (int(x) for x in time_str.split(":"))
        except ValueError:
            continue
        sched_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if sched_dt <= now < sched_dt + timedelta(minutes=window_minutes):
            due.append({"id": schedule_id, "store_id": store_id, "time": time_str})
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


def resolve_carryover_after_reply(store_id: str, item: dict, outcome: str, final_qty: int | None = None) -> None:
    """텔레그램 확인/스킵/제외 응답 이후 도매처 품목의 이월값을 정산한다.
    outcome="confirmed": 실제 담은 만큼 소진하고 잔여만 이월.
    outcome="skipped": 이번엔 안 담았으므로 pending_qty 그대로 이월."""
    pending_qty = int(item.get("pending_qty", 0))
    pack_qty = int(item.get("pack_qty", 1) or 1)
    item_key = item["item_key"]
    category = item.get("category", "wholesale")

    if outcome == "confirmed":
        qty = final_qty if final_qty is not None else int(item.get("cases", 0))
        remainder = max(0, pending_qty - qty * pack_qty)
        set_carryover(store_id, item_key, category, remainder)
    else:
        set_carryover(store_id, item_key, category, pending_qty)


# --- 리포트 생성 ---

def generate_report(store_id: str) -> dict:
    creds = vendors.get_store_vendor_credentials(store_id, "orderqueen")
    if not creds:
        return {"ok": False, "reason": "오더퀸 계정이 등록되어 있지 않습니다. '내 도매처 계정'에서 먼저 등록해주세요."}
    login_id, login_pw = creds

    period_from = get_last_aggregated_until(store_id) + timedelta(days=1)
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

    catalog = mapping.load_coupang_catalog_xlsx(str(COUPANG_CATALOG_XLSX_PATH))
    disabled_vendors, preferred_vendor = vendors.get_store_vendor_prefs(store_id)

    wholesale_items = []
    coupang_items = []
    icecream_items = []
    other_items = []

    for row in top_items:
        barcode = str(row.get("바코드번호", "") or "").strip().replace(".0", "")
        name = str(row.get("메뉴명", "") or "").strip()
        sold_qty = int(row.get("판매수량", 0) or 0)
        if sold_qty <= 0 or not name:
            continue

        cat = catalog.get(barcode) if barcode else None
        is_coupang = int(cat.is_coupang) if cat else 99
        item_key = barcode or name

        pop_category = POPULARITY_CATEGORY_BY_IS_COUPANG.get(is_coupang)
        if pop_category:
            popularity.log_event(store_id, pop_category, item_key, name, sold_qty)

        if is_coupang == 0:
            box_qty = int(getattr(cat, "icecream_box_qty", 0) or 0) if cat else 0
            cases = apply_case_rule(sold_qty, box_qty)
            if cases > 0:
                icecream_items.append({
                    "item_key": item_key, "name": name, "sold_qty": sold_qty,
                    "box_qty": box_qty, "cases": cases,
                })
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
            carried = get_carryover(store_id, item_key)
            pending_qty = carried + sold_qty
            keyword = (cat.search_keyword or cat.menu_name or name) if cat else name

            compare_result = price_compare.compare(keyword)
            groups = price_compare.filter_groups_for_store(compare_result.get("groups", []), disabled_vendors)
            offer = None
            for group in groups:
                offer = _pick_best_offer(group.get("offers", []), preferred_vendor)
                if offer:
                    break

            if not offer:
                set_carryover(store_id, item_key, "wholesale", pending_qty)
                continue

            pack_qty = int(offer.get("unit_qty") or 1)
            cases = apply_case_rule(pending_qty, pack_qty)
            if cases <= 0:
                set_carryover(store_id, item_key, "wholesale", pending_qty)
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
            other_items.append({"item_key": item_key, "name": name, "sold_qty": sold_qty})

    set_last_aggregated_until(store_id, period_to)

    return {
        "ok": True,
        "period_from": period_from.isoformat(),
        "period_to": period_to.isoformat(),
        "wholesale_items": wholesale_items,
        "coupang_items": coupang_items,
        "icecream_items": icecream_items,
        "other_items": other_items,
    }
