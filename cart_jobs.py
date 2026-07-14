# cart_jobs.py
"""Playwright 담기(실제 카트 추가)를 웹 서비스 프로세스에서 분리해 별도
워커(worker.py)가 처리하도록 만든 Postgres 기반 작업 큐. Redis 없이
"SELECT ... FOR UPDATE SKIP LOCKED"로 여러 워커 인스턴스가 같은 작업을
중복으로 집어가지 않게 한다(표준 Postgres 큐 패턴) - 워커를 여러 대 띄우면
그만큼 동시 처리량이 늘어나는 게 이 모듈을 만든 이유다.

kind별 payload_json 모양:
- "telegram_batch": {"items": [...], "resolved_accounts": {...}}
  (텔레그램 "확인" 한 번에 담을 상품 목록 전체 - 처리 후 결과 메시지 하나로 요약)
- "telegram_stockout": {"item": {...}}
  (품절 대안 선택 후 단일 상품 재시도)
- "web_item": {"item": {...}, "with_fallback": bool}
  (웹에서 상품 하나 담기. with_fallback=True면 /cart 페이지의 "담기"처럼
  batch_vendors 내에서 자동 품절 전환까지 시도하고, 처리시점에
  web_cart.list_items(store_id)로 batch_vendors를 새로 계산한다.
  with_fallback=False면 /compare의 대안 도매처 선택처럼 자동 전환 없이
  그 도매처로만 단발 시도한다)
- "load_test_noop": {"duration_seconds": float}
  (실제 도매처/Playwright는 전혀 안 건드리는 가짜 담기 - 워커/큐 처리량만
  재보는 부하 테스트 전용. 실제 서비스 어느 코드 경로에서도 만들어지지 않고
  enqueue_load_test_noop()로만 생성된다)
"""
import json
from datetime import datetime

import db_conn


def get_conn():
    return db_conn.get_conn()


def init_cart_jobs_table() -> None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS cart_jobs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        store_id TEXT NOT NULL,
        chat_id TEXT,
        web_cart_item_id INTEGER,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        result_json TEXT,
        created_at TEXT,
        started_at TEXT,
        finished_at TEXT
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cart_jobs_status ON cart_jobs (status, id)")
    conn.commit()
    conn.close()


def _enqueue(
    kind: str, store_id: str, payload: dict,
    chat_id: str | None = None, web_cart_item_id: int | None = None,
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO cart_jobs (kind, store_id, chat_id, web_cart_item_id, payload_json, status, created_at)
    VALUES (?, ?, ?, ?, ?, 'pending', ?) RETURNING id
    """, (kind, store_id, chat_id, web_cart_item_id, json.dumps(payload, ensure_ascii=False), now))
    job_id = cur.fetchone()[0]
    conn.commit()
    conn.close()
    return job_id


def enqueue_telegram_batch(chat_id: str, store_id: str, items: list[dict], resolved_accounts: dict) -> int:
    return _enqueue("telegram_batch", store_id, {"items": items, "resolved_accounts": resolved_accounts}, chat_id=chat_id)


def enqueue_telegram_stockout(chat_id: str, store_id: str, item: dict) -> int:
    return _enqueue("telegram_stockout", store_id, {"item": item}, chat_id=chat_id)


def enqueue_web_item(store_id: str, web_cart_item_id: int | None, item: dict, with_fallback: bool = False) -> int:
    return _enqueue(
        "web_item", store_id, {"item": item, "with_fallback": with_fallback},
        web_cart_item_id=web_cart_item_id,
    )


def enqueue_load_test_noop(duration_seconds: float = 30) -> int:
    """실제 담기와 동일한 동시성 제약(browser_semaphore)만 받고 Playwright/
    도매처는 전혀 안 건드리는 가짜 job - 워커/큐 처리량 측정용 부하 테스트
    전용. store_id를 "__load_test__"로 고정해 실제 매장 데이터와 절대
    섞이지 않게 한다."""
    return _enqueue("load_test_noop", "__load_test__", {"duration_seconds": duration_seconds})


def claim_next_job() -> dict | None:
    """대기 중인 작업 하나를 원자적으로 집어서 processing으로 표시한다.
    여러 워커 프로세스가 동시에 폴링해도 같은 작업을 중복으로 못 집어가게
    FOR UPDATE SKIP LOCKED로 잠긴 행은 건너뛴다."""
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    UPDATE cart_jobs SET status = 'processing', started_at = ?
    WHERE id = (
        SELECT id FROM cart_jobs WHERE status = 'pending' ORDER BY id ASC
        FOR UPDATE SKIP LOCKED LIMIT 1
    )
    RETURNING id, kind, store_id, chat_id, web_cart_item_id, payload_json
    """, (now,))
    row = cur.fetchone()
    conn.commit()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0], "kind": row[1], "store_id": row[2], "chat_id": row[3],
        "web_cart_item_id": row[4], "payload": json.loads(row[5]),
    }


def mark_done(job_id: int, result: dict) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE cart_jobs SET status = 'done', result_json = ?, finished_at = ? WHERE id = ?",
        (json.dumps(result, ensure_ascii=False), now, job_id),
    )
    conn.commit()
    conn.close()


def mark_failed(job_id: int, reason: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE cart_jobs SET status = 'failed', result_json = ?, finished_at = ? WHERE id = ?",
        (json.dumps({"ok": False, "reason": reason}, ensure_ascii=False), now, job_id),
    )
    conn.commit()
    conn.close()


def get_job(job_id: int) -> dict | None:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, kind, store_id, status, result_json FROM cart_jobs WHERE id = ?",
        (job_id,),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0], "kind": row[1], "store_id": row[2], "status": row[3],
        "result": json.loads(row[4]) if row[4] else None,
    }
