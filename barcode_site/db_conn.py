# db_conn.py
"""본체(is-order) 프로젝트의 db_conn.py를 그대로 가져온 경량 버전. 이 사이트는
같은 Postgres(DATABASE_URL)를 읽기 전용으로만 쓰므로, 본체와 똑같은 커넥션
풀링 방식을 재사용하되 풀 크기 기본값만 훨씬 작게 잡는다(본체는 여러 지점의
동시 쓰기까지 받는 무거운 서비스지만, 이 사이트는 바코드 검색 조회 하나뿐)."""
import os
import threading
import time

from psycopg2 import pool as pg_pool
from psycopg2 import InterfaceError, OperationalError
from psycopg2.pool import PoolError

DATABASE_URL = os.getenv("DATABASE_URL")
POOL_MIN = int(os.getenv("DB_POOL_MIN", "1"))
POOL_MAX = int(os.getenv("DB_POOL_MAX", "5"))
POOL_WAIT_TIMEOUT_SECONDS = float(os.getenv("DB_POOL_WAIT_TIMEOUT", "5"))
POOL_WAIT_RETRY_INTERVAL_SECONDS = 0.05

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


class _QmarkCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, params=()):
        # params가 빈 튜플이면 반드시 None으로 넘겨야 한다 - psycopg2는 params가
        # None이 아니면(빈 튜플이어도) SQL 안의 "%" 문자를 전부 치환 대상으로
        # 보는데, LIKE 'x%' 같은 리터럴 %가 있으면 다음 파라미터로 착각해
        # IndexError를 낸다.
        self._cursor.execute(sql.replace("?", "%s"), params or None)
        return self

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _QmarkConnection:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self):
        return _QmarkCursor(self._conn.cursor())

    def close(self):
        p = _get_pool()
        try:
            self._conn.rollback()
        except (OperationalError, InterfaceError):
            p.putconn(self._conn, close=True)
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
            cur.execute("SELECT 1")  # Render가 유휴 연결을 끊었을 수 있어 생존 확인
    except (OperationalError, InterfaceError):
        p.putconn(conn, close=True)
        conn = _borrow_with_wait(p)
    return _QmarkConnection(conn)
