# db_conn.py
"""SQLite에서 PostgreSQL로 옮기면서 만든 공용 연결 모듈. 기존 ~10개 모듈은 전부
"?" 플레이스홀더 스타일(sqlite3)로 SQL을 써왔는데, 그 SQL 문자열은 손대지 않고
그대로 쓸 수 있게 psycopg2 커서/커넥션을 얇게 감싼다. 실제로 바꾸는 건 각
모듈의 get_conn() 딱 한 곳뿐이다.

변환하는 SQLite 전용 문법은 이 프로젝트에서 실제로 쓰인 3가지뿐이다(전수
조사 완료):
1. "?" 플레이스홀더 -> Postgres의 "%s"
2. "INTEGER PRIMARY KEY AUTOINCREMENT" -> "SERIAL PRIMARY KEY"
3. "PRAGMA table_info(table)" -> information_schema.columns 조회, 기존
   호출부가 row[1]로 컬럼명을 꺼내 쓰는 걸 그대로 쓸 수 있도록 SQLite의
   (cid, name, type, notnull, dflt_value, pk) 모양을 맞춰서 반환한다.

커넥션은 매번 새로 여는 게 아니라 프로세스당 하나씩 두는 풀(ThreadedConnectionPool)에서
빌려 쓴다 - SQLite 시절엔 매 호출마다 sqlite3.connect()해도 파일 기반이라
무해했지만, Postgres에서 그대로 했다가는(실제로 이렇게 되어 있었음) 매장 수가
늘면서 동시 요청이 많아질 때 max_connections를 순식간에 소진해 전체 서비스가
죽는다. 웹 서비스/워커는 별도 프로세스라 풀도 프로세스별로 하나씩 생긴다."""
import os
import re
import threading
import time

from psycopg2 import pool as pg_pool
from psycopg2 import InterfaceError, OperationalError
from psycopg2.pool import PoolError

DATABASE_URL = os.getenv("DATABASE_URL")
POOL_MIN = int(os.getenv("DB_POOL_MIN", "2"))
POOL_MAX = int(os.getenv("DB_POOL_MAX", "30"))
# psycopg2 풀은 다 찼을 때 기다리지 않고 바로 PoolError를 던진다 - 순간적으로
# 몰린 요청이 그대로 500으로 튕겨나가는 걸 막기 위해, 짧은 간격으로 재시도하며
# 이 시간만큼은 자리가 날 때까지 기다려본다(그래도 안 나면 그때 진짜로 포기).
POOL_WAIT_TIMEOUT_SECONDS = float(os.getenv("DB_POOL_WAIT_TIMEOUT", "5"))
POOL_WAIT_RETRY_INTERVAL_SECONDS = 0.05

_AUTOINCREMENT_TEXT = "INTEGER PRIMARY KEY AUTOINCREMENT"
_PRAGMA_TABLE_INFO_RE = re.compile(r"^\s*PRAGMA\s+table_info\((\w+)\)\s*$", re.IGNORECASE)

_pool = None
_pool_lock = threading.Lock()


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                if not DATABASE_URL:
                    raise RuntimeError("DATABASE_URL 환경변수가 설정되지 않았습니다.")
                _pool = pg_pool.ThreadedConnectionPool(POOL_MIN, POOL_MAX, DATABASE_URL)
    return _pool


def _translate(sql: str) -> str:
    return sql.replace(_AUTOINCREMENT_TEXT, "SERIAL PRIMARY KEY").replace("?", "%s")


class _QmarkCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, params=()):
        # sqlite3.Cursor.execute()는 자기 자신을 반환해서 기존 코드가
        # cur.execute(...).fetchall() 형태로 체이닝해 쓰는데, psycopg2 커서의
        # execute()는 None을 반환하므로 여기서 self를 명시적으로 돌려줘야 한다.
        match = _PRAGMA_TABLE_INFO_RE.match(sql)
        if match:
            self._cursor.execute("""
                SELECT ordinal_position - 1, column_name, data_type, 0, column_default, 0
                FROM information_schema.columns
                WHERE table_name = %s
                ORDER BY ordinal_position
            """, (match.group(1),))
        else:
            # params가 빈 튜플(파라미터 없이 호출된 경우)이면 반드시 None으로
            # 넘겨야 한다 - psycopg2는 params가 None이 아니면(빈 튜플이어도)
            # SQL 문자열에 있는 "%" 문자를 전부 치환 대상으로 보고 해석을
            # 시도하는데, LIKE 'x%' 같은 리터럴 %가 있으면 다음 문의
            # 파라미터로 착각해 IndexError를 낸다.
            self._cursor.execute(_translate(sql), params or None)
        return self

    def executemany(self, sql, seq_of_params):
        self._cursor.executemany(_translate(sql), seq_of_params)
        return self

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _QmarkConnection:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return _QmarkCursor(self._conn.cursor())

    def execute(self, sql, params=()):
        # 커서 없이 conn.execute(...)를 직접 호출하던 기존 코드용.
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def close(self):
        # 기존 호출부는 전부 "쓰고 나면 close()"로 끝나던 SQLite 패턴이라,
        # 여기서 실제 연결을 끊는 대신 풀에 반납해서 재사용한다. 반납 전
        # rollback()으로 비운다 - 기존 코드는 쓰기 후 항상 명시적으로
        # commit()을 부르므로 정상 경로에선 아무 트랜잭션도 안 남아 no-op이고,
        # execute() 중간에 예외가 나서 commit 없이 close()만 불린 경우(방어적
        # 처리) 그 미완료 트랜잭션이 다음에 이 커넥션을 빌려 쓰는 호출자에게
        # 새는 것을 막아준다.
        p = _get_pool()
        try:
            self._conn.rollback()
        except (OperationalError, InterfaceError):
            p.putconn(self._conn, close=True)  # 이미 죽은 연결은 재사용하지 않고 폐기
            return
        p.putconn(self._conn)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _borrow_with_wait(p):
    deadline = time.monotonic() + POOL_WAIT_TIMEOUT_SECONDS
    while True:
        try:
            return p.getconn()
        except PoolError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(POOL_WAIT_RETRY_INTERVAL_SECONDS)


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL 환경변수가 설정되지 않았습니다.")
    p = _get_pool()
    conn = _borrow_with_wait(p)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")  # Render가 유휴 연결을 서버 쪽에서 끊어놨을 수 있어 생존 확인
    except (OperationalError, InterfaceError):
        p.putconn(conn, close=True)
        conn = _borrow_with_wait(p)  # 죽은 연결 대신 새로 하나 더 빌림
    return _QmarkConnection(conn)
