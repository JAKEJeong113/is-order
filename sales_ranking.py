# sales_ranking.py
"""판매처(아이스크림/쿠팡/도매몰)별 주간·월간 인기 판매 품목 순위.

여러 매장의 오더퀸 실제 판매 데이터를 모아서, 각 매장이 "요즘 뭐가 잘
팔리나"를 매입 판단에 참고할 수 있게 제공하는 기능이다(사용자 요청).

popularity.order_events를 그대로 재사용하지 않는다 - 그 테이블의
"wholesale" 카테고리는 이 앱을 통한 도매몰 발주(구매)량과 오더퀸에서 긁어온
실제 판매량이 섞여 있어서(cart_add_logic.py/worker.py는 발주량을,
store_reports.py/main.py는 판매량을 같은 카테고리로 기록함), 순수하게
"얼마나 팔렸는지"만 봐야 하는 이 기능에는 맞지 않는다. 그래서 이 모듈은
자체 테이블(oq_sales_events)에 오더퀸 판매량만 따로 쌓는다.

수집 대상은 오더퀸 계정 저장 시 "판매 데이터를 전체 가맹점 순위 산출에
활용"에 동의한 계정만이다(vendors.sales_data_consent, 사용자 확인된
개인정보 처리 방침 - 목적 외 이용 금지 원칙에 따름). 동의 여부와 무관하게
바코드 등록 등 계정의 다른 기능은 그대로 쓸 수 있다.

수집은 매일 배치로 돌며(main.py 스케줄러), 계정별로 "마지막으로 수집한
날짜" 다음날부터 어제까지의 판매 데이터를 오더퀸 매출 리포트(SAL03020.itp)
에서 받아와 카테고리별로 분류해 저장한다. 오늘자 데이터는 아직 집계가 끝나지
않았을 수 있어 제외한다(어제까지만).
"""
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import db_conn
import mapping
import vendors
from orderqueen_bot import download_orderqueen_xlsx_with_retry
from parser import parse_menu_sales_xlsx

BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = BASE_DIR / "downloads"

CATEGORY_BY_IS_COUPANG = {0: "icecream", 1: "coupang", 2: "wholesale"}
CATEGORIES = ("icecream", "coupang", "wholesale")
PERIODS = ("week", "month")


def get_conn():
    return db_conn.get_conn()


def init_sales_ranking_tables() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS oq_sales_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        store_id TEXT NOT NULL,
        account_id INTEGER NOT NULL,
        category TEXT NOT NULL,
        item_key TEXT NOT NULL,
        item_name TEXT NOT NULL,
        qty INTEGER NOT NULL,
        sale_date TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_oq_sales_events_cat_date ON oq_sales_events (category, sale_date)")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS oq_sales_ranking_progress (
        store_id TEXT NOT NULL,
        account_id INTEGER NOT NULL,
        last_collected_date TEXT,
        PRIMARY KEY (store_id, account_id)
    )
    """)
    conn.commit()
    conn.close()


def _get_last_collected_date(store_id: str, account_id: int) -> date | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT last_collected_date FROM oq_sales_ranking_progress WHERE store_id = ? AND account_id = ?",
        (store_id, account_id),
    )
    row = cur.fetchone()
    conn.close()
    if not row or not row[0]:
        return None
    return date.fromisoformat(row[0])


def _set_last_collected_date(store_id: str, account_id: int, d: date) -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO oq_sales_ranking_progress (store_id, account_id, last_collected_date)
    VALUES (?, ?, ?)
    ON CONFLICT(store_id, account_id) DO UPDATE SET last_collected_date = excluded.last_collected_date
    """, (store_id, account_id, d.isoformat()))
    conn.commit()
    conn.close()


def _collect_one_account(store_id: str, account_id: int) -> None:
    creds = vendors.resolve_store_vendor_account(store_id, "orderqueen", account_id)
    if not creds:
        return

    last_date = _get_last_collected_date(store_id, account_id)
    period_from = (last_date + timedelta(days=1)) if last_date else (date.today() - timedelta(days=1))
    period_to = date.today() - timedelta(days=1)
    if period_from > period_to:
        return  # 어제까지 이미 수집 완료

    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    xlsx_path = str(DOWNLOAD_DIR / f"salesrank_{uuid.uuid4().hex[:8]}.xlsx")
    try:
        download_orderqueen_xlsx_with_retry(
            login_id=creds["login_id"], login_pw=creds["login_pwd"],
            period_from=period_from, period_to=period_to, save_path=xlsx_path,
        )
        _, _, top_items = parse_menu_sales_xlsx(xlsx_path, period_from, period_to)
    finally:
        try:
            Path(xlsx_path).unlink(missing_ok=True)
        except Exception:
            pass

    catalog = mapping.load_catalog()
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    for row in top_items:
        barcode = str(row.get("바코드번호", "") or "").strip().replace(".0", "")
        name = str(row.get("메뉴명", "") or "").strip()
        qty = int(row.get("판매수량", 0) or 0)
        if qty <= 0 or not name:
            continue

        cat = catalog.get(barcode) if barcode else None
        category = CATEGORY_BY_IS_COUPANG.get(int(cat.is_coupang)) if cat else None
        if not category:
            continue  # 미분류/문구완구 등은 순위 대상 아님

        item_key = barcode or name
        cur.execute("""
        INSERT INTO oq_sales_events (store_id, account_id, category, item_key, item_name, qty, sale_date, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (store_id, account_id, category, item_key, name, qty, period_to.isoformat(), now))
    conn.commit()
    conn.close()

    _set_last_collected_date(store_id, account_id, period_to)


def collect_all_pending() -> dict:
    """동의한 계정 전체를 순회하며 밀린 판매 데이터를 수집한다(main.py의
    일 1회 스케줄러가 호출). 계정 하나가 실패해도(오더퀸 일시 장애 등)
    나머지는 계속 진행한다."""
    accounts = vendors.list_consented_sales_data_accounts("orderqueen")
    collected = 0
    failed = 0
    errors = []
    for acc in accounts:
        try:
            _collect_one_account(acc["store_id"], acc["id"])
            collected += 1
        except Exception as e:
            failed += 1
            errors.append({"store_id": acc["store_id"], "account_id": acc["id"], "error": str(e)})
    return {"total": len(accounts), "collected": collected, "failed": failed, "errors": errors}


def _period_range(period: str, today: date | None = None) -> tuple[date, date]:
    today = today or date.today()
    if period == "week":
        start = today - timedelta(days=today.weekday())  # 이번 주 월요일
    elif period == "month":
        start = today.replace(day=1)
    else:
        raise ValueError(f"알 수 없는 기간: {period}")
    return start, today


def get_ranking(category: str, period: str, limit: int = 20) -> list[dict]:
    if category not in CATEGORIES:
        raise ValueError(f"알 수 없는 카테고리: {category}")
    start, end = _period_range(period)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT item_key, MAX(item_name) AS item_name, SUM(qty) AS total_qty
    FROM oq_sales_events
    WHERE category = ? AND sale_date >= ? AND sale_date <= ?
    GROUP BY item_key
    ORDER BY total_qty DESC
    LIMIT ?
    """, (category, start.isoformat(), end.isoformat(), limit))
    rows = cur.fetchall()
    conn.close()
    return [{"item_key": r[0], "item_name": r[1], "total_qty": r[2]} for r in rows]
