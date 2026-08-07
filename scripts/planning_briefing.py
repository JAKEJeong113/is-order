# scripts/planning_briefing.py
"""기획팀 파일럿: isorder 운영 데이터(기능 사용, 발주, 크롤링 상태)에서
신호를 뽑아 사람이 읽을 브리핑으로 요약한다. 읽기 전용 SELECT만 수행하며
아무 것도 쓰지 않는다.

여기서 나온 신호는 그 자체로 기획 결론이 아니라 사람이 다음 기획
아이템을 고를 때 참고하는 원재료다 - 무엇을 만들지는 사람이 정한다.

사용법:
    venv/Scripts/python.exe scripts/planning_briefing.py
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import db_conn


def q(sql, params=()):
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.close()
    return rows


def section(title):
    print(f"\n=== {title} ===")


def main():
    section("1. 기능 사용 빈도 (최근 30일, usage_events)")
    try:
        rows = q("""
            SELECT feature, COUNT(*) as cnt, COUNT(DISTINCT store_id) as stores
            FROM usage_events
            WHERE created_at::timestamp >= NOW() - INTERVAL '30 days'
            GROUP BY feature ORDER BY cnt DESC
        """)
        for r in rows:
            print(f"  {r['feature']:20s} 호출 {r['cnt']:>6}회  이용 지점 {r['stores']:>4}곳")
    except Exception as e:
        print("  에러:", e)

    section("2. 발주 상위 카테고리 (order_events, 최근 60일)")
    try:
        rows = q("""
            SELECT category, COUNT(*) as events, SUM(qty) as total_qty, COUNT(DISTINCT store_id) as stores
            FROM order_events
            WHERE created_at::timestamp >= NOW() - INTERVAL '60 days'
            GROUP BY category ORDER BY total_qty DESC
        """)
        for r in rows:
            print(f"  {r['category']:15s} 이벤트 {r['events']:>6}  수량 {r['total_qty']:>8}  지점 {r['stores']:>4}곳")
    except Exception as e:
        print("  에러:", e)

    section("3. 카탈로그 크롤링 상태 (vendor별 최근 갱신)")
    try:
        rows = q("""
            SELECT vendor_id, product_count, refreshed_at, ok, error
            FROM catalog_refresh_log
            ORDER BY refreshed_at DESC
        """)
        for r in rows:
            status = "OK" if r["ok"] else f"에러: {r['error']}"
            print(f"  {r['vendor_id']:15s} 상품수 {r['product_count']:>6}  최근갱신 {r['refreshed_at']}  {status}")
    except Exception as e:
        print("  에러:", e)

    section("4. 활성 지점 수 (usage_events 기준 distinct store_id, 전체 기간)")
    try:
        rows = q("SELECT COUNT(DISTINCT store_id) as n FROM usage_events")
        print(f"  누적 활성 지점: {rows[0]['n']}곳")
    except Exception as e:
        print("  에러:", e)

    section("5. 미해결 가격 알림 (pending_price_alerts)")
    try:
        rows = q("SELECT status, COUNT(*) as n FROM pending_price_alerts GROUP BY status")
        for r in rows:
            print(f"  {r['status']:15s} {r['n']}건")
    except Exception as e:
        print("  에러:", e)


if __name__ == "__main__":
    main()
