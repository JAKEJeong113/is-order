# usage_stats.py
"""가맹점(웹 계정/텔레그램 지점)별 기능 사용 통계.

검색/조회처럼 다른 곳에 흔적이 안 남는 행동만 여기 usage_events에 새로
기록하고(가격비교 검색, 바코드 검색, 음료·과자 추천 조회, 웹 로그인),
발주 실행(cart_jobs)·판매 데이터 처리(popularity.order_events)처럼 이미
다른 모듈이 정확한 이력을 남기고 있는 행동은 그대로 재사용한다 - 같은
사실을 두 군데에 중복 기록하지 않기 위함.

주의: web_sessions은 로그아웃/비밀번호 변경 시 행이 삭제되므로 누적
로그인 횟수 집계에 못 쓴다(현재 활성 세션 수만 알 수 있음) - 그래서
로그인은 반드시 이 모듈의 usage_events로 별도 기록한다.
store_manual_reports도 지점당 최신 1건만 남기는 UPSERT라 생성 "횟수"는
셀 수 없고, 마지막 생성 시각만 믿을 수 있는 값이다."""
from datetime import datetime, timedelta

import db_conn

FEATURES = ("compare_search", "barcode_search", "beverage_view", "snack_view", "web_login")


def get_conn():
    return db_conn.get_conn()


def init_usage_events_table() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS usage_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        store_id TEXT,
        feature TEXT,
        created_at TEXT
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_store ON usage_events (store_id, feature)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_usage_events_feature ON usage_events (feature, created_at)")
    conn.commit()
    conn.close()


def log_event(store_id: str, feature: str) -> None:
    if feature not in FEATURES or not store_id:
        return
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO usage_events (store_id, feature, created_at) VALUES (?, ?, ?)",
        (store_id, feature, now),
    )
    conn.commit()
    conn.close()


def _since_date(period: str) -> str | None:
    if period == "7d":
        return (datetime.now() - timedelta(days=7)).isoformat(timespec="seconds")
    if period == "30d":
        return (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")
    return None


def get_usage_summary(period: str = "all") -> list[dict]:
    """웹 계정 하나당 한 행 - 연결된 텔레그램 지점이 있으면 그쪽 활동(주로
    텔레그램 발주 실행)까지 합쳐서 보여준다. 웹 계정이 없는 텔레그램 전용
    지점(가입은 했지만 아직 웹 회원가입은 안 한 경우)은 뒤에 별도 행으로
    붙인다."""
    import telegram_store
    import web_auth

    since = _since_date(period)
    conn = get_conn()
    cur = conn.cursor()

    def _count(table: str, col: str, value, extra_sql: str = "") -> int:
        if not value:
            return 0
        sql = f"SELECT COUNT(*) FROM {table} WHERE {col} = ?"
        params: list = [value]
        if extra_sql:
            sql += f" AND {extra_sql}"
        if since:
            sql += " AND created_at >= ?"
            params.append(since)
        cur.execute(sql, params)
        return cur.fetchone()[0]

    def _last(table: str, col: str, value, time_col: str = "generated_at"):
        if not value:
            return None
        cur.execute(f"SELECT {time_col} FROM {table} WHERE {col} = ?", (value,))
        row = cur.fetchone()
        return row[0] if row else None

    users = web_auth.list_users()
    tg_stores = {s["store_name"]: s for s in telegram_store.list_stores()}
    linked_store_names = {u["linked_store_name"] for u in users if u["linked_store_name"]}

    def _row_for(web_email: str | None, display_name: str, tg: dict | None) -> dict:
        web_store_id = f"web:{web_email}" if web_email else None
        tg_store_id = tg["store_name"] if tg else None
        return {
            "email": web_email,
            "display_name": display_name,
            "linked_store_name": tg_store_id,
            "telegram_status": (
                "미연동" if not tg else ("승인됨" if tg["approved"] else "승인대기")
            ),
            "web_login": _count("usage_events", "store_id", web_store_id, "feature = 'web_login'"),
            "cart_web": _count("cart_jobs", "store_id", web_store_id, "kind = 'web_item'"),
            "cart_telegram": _count("cart_jobs", "store_id", tg_store_id, "kind IN ('telegram_batch','telegram_stockout')"),
            "compare_search": _count("usage_events", "store_id", web_store_id, "feature = 'compare_search'"),
            "barcode_search": _count("usage_events", "store_id", web_store_id, "feature = 'barcode_search'"),
            "beverage_view": _count("usage_events", "store_id", web_store_id, "feature = 'beverage_view'"),
            "snack_view": _count("usage_events", "store_id", web_store_id, "feature = 'snack_view'"),
            "sales_events": _count("order_events", "store_id", web_store_id),
            "last_manual_report_at": _last("store_manual_reports", "store_id", web_store_id),
        }

    rows = []
    for u in users:
        tg = tg_stores.get(u["linked_store_name"]) if u["linked_store_name"] else None
        rows.append(_row_for(u["email"], u["display_name"] or u["email"], tg))

    for store_name, tg in tg_stores.items():
        if store_name in linked_store_names:
            continue
        rows.append(_row_for(None, tg["display_name"] or store_name, tg))

    conn.close()

    for r in rows:
        r["total"] = (
            r["web_login"] + r["cart_web"] + r["cart_telegram"] + r["compare_search"]
            + r["barcode_search"] + r["beverage_view"] + r["snack_view"] + r["sales_events"]
        )
    rows.sort(key=lambda r: -r["total"])
    return rows
