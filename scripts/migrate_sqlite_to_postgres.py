# scripts/migrate_sqlite_to_postgres.py
"""1회용 마이그레이션 스크립트: 로컬 SQLite(inventory.db)의 데이터를 새
PostgreSQL DB로 옮긴다.

전제: 대상 Postgres에 스키마(테이블)가 이미 만들어져 있어야 한다 - 앱을
DATABASE_URL을 가리키게 하고 한 번 기동(또는 import)하면 각 모듈의
init_*()가 알아서 스키마를 만든다(이 스크립트는 DDL을 새로 짜지 않고
그걸 재사용한다). 여기서는 순수하게 행 데이터만 복사한다.

- SQLite 쪽에 있는 테이블 중 Postgres 스키마에도 존재하는 것만 옮긴다
  (예: 예전 beverage_ranking.py가 쓰던 빈 beverage_rankings 테이블처럼
  이미 죽은 테이블은 자동으로 건너뛴다).
- id 컬럼이 있는 테이블은 옮긴 뒤 시퀀스를 MAX(id)로 재조정해서, 마이그레이션
  후 새로 INSERT할 때 기존 id와 충돌하지 않게 한다.
- 테이블별 원본/대상 행 수를 출력해서 눈으로 확인할 수 있게 한다.

사용법:
    venv/Scripts/python.exe scripts/migrate_sqlite_to_postgres.py [소스 sqlite 파일 경로]
(경로를 안 주면 REPO_ROOT/inventory.db. .env의 DATABASE_URL을 자동으로 읽는다)
"""
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv(REPO_ROOT / ".env")

import db_conn

SQLITE_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "inventory.db"


def get_sqlite_tables(conn: sqlite3.Connection) -> list[str]:
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    return sorted(r[0] for r in cur.fetchall())


def get_postgres_tables(pg_conn) -> set[str]:
    cur = pg_conn.cursor()
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
    return {r[0] for r in cur.fetchall()}


def migrate_table(sqlite_conn: sqlite3.Connection, pg_conn, table: str) -> tuple[int, int]:
    s_cur = sqlite_conn.cursor()
    s_cur.execute(f"SELECT * FROM {table}")
    rows = s_cur.fetchall()
    col_names = [d[0] for d in s_cur.description]
    source_count = len(rows)

    # SQLite는 INTEGER로 선언한 컬럼도 크기 제한 없이 저장하지만(느슨한 타입
    # 어피니티), Postgres INTEGER는 32비트라 그 범위를 넘으면 그대로 실패한다.
    # 실측으로 hdinter 크롤러 버그로 price에 억 단위를 넘는 쓰레기 값이 들어간
    # 행이 있었다 - product_cache는 매일 재크롤링되는 캐시라 이런 값은 원본을
    # 지키기보다 NULL로 비우고 다음 크롤링에서 정상값으로 덮어써지게 둔다.
    PG_INT4_MIN, PG_INT4_MAX = -2147483648, 2147483647
    sanitized = 0
    clean_rows = []
    for row in rows:
        new_row = list(row)
        for i, val in enumerate(new_row):
            if isinstance(val, int) and not isinstance(val, bool) and not (PG_INT4_MIN <= val <= PG_INT4_MAX):
                new_row[i] = None
                sanitized += 1
        clean_rows.append(tuple(new_row))
    rows = clean_rows
    if sanitized:
        print(f"    ({table}: 32비트 범위를 벗어난 값 {sanitized}개를 NULL로 정리함)")

    pg_cur = pg_conn.cursor()
    pg_cur.execute(f"DELETE FROM {table}")  # 재실행 시 중복 삽입 방지

    if rows:
        columns = ", ".join(col_names)
        placeholders = ", ".join(["?"] * len(col_names))
        pg_cur.executemany(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", rows)

    if "id" in col_names:
        pg_cur.execute(
            f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE((SELECT MAX(id) FROM {table}), 1))"
        )

    pg_conn.commit()

    pg_cur.execute(f"SELECT COUNT(*) FROM {table}")
    dest_count = pg_cur.fetchone()[0]
    return source_count, dest_count


def main() -> None:
    if not SQLITE_PATH.exists():
        print(f"SQLite 파일을 찾을 수 없습니다: {SQLITE_PATH}")
        sys.exit(1)

    sqlite_conn = sqlite3.connect(SQLITE_PATH)
    pg_conn = db_conn.get_conn()

    sqlite_tables = get_sqlite_tables(sqlite_conn)
    postgres_tables = get_postgres_tables(pg_conn)
    skipped = [t for t in sqlite_tables if t not in postgres_tables]
    to_migrate = [t for t in sqlite_tables if t in postgres_tables]

    if skipped:
        print(f"Postgres 스키마에 없어서 건너뜀: {', '.join(skipped)}")
    print(f"마이그레이션 대상: {len(to_migrate)}개 테이블\n")

    mismatches = []
    for table in to_migrate:
        source_count, dest_count = migrate_table(sqlite_conn, pg_conn, table)
        status = "OK" if source_count == dest_count else "MISMATCH"
        if status == "MISMATCH":
            mismatches.append(table)
        print(f"  {table:<28} 원본 {source_count:>6}행 -> 대상 {dest_count:>6}행  [{status}]")

    sqlite_conn.close()
    pg_conn.close()

    print()
    if mismatches:
        print(f"불일치 테이블 있음: {', '.join(mismatches)} - 확인 필요")
        sys.exit(1)
    print("모든 테이블 행 수 일치. 마이그레이션 완료.")


if __name__ == "__main__":
    main()
