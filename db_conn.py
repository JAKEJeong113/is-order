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
"""
import os
import re

import psycopg2

DATABASE_URL = os.getenv("DATABASE_URL")

_AUTOINCREMENT_TEXT = "INTEGER PRIMARY KEY AUTOINCREMENT"
_PRAGMA_TABLE_INFO_RE = re.compile(r"^\s*PRAGMA\s+table_info\((\w+)\)\s*$", re.IGNORECASE)


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
            self._cursor.execute(_translate(sql), params)
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

    def __getattr__(self, name):
        return getattr(self._conn, name)


def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL 환경변수가 설정되지 않았습니다.")
    return _QmarkConnection(psycopg2.connect(DATABASE_URL))
