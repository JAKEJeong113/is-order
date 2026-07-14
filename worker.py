# worker.py
"""cart_jobs 큐를 폴링해서 실제 담기(Playwright)를 처리하는 백그라운드 워커.
웹 서비스와 별도의 Render 서비스로 띄운다 - 워커 인스턴스를 늘리면 그만큼
동시 처리량이 늘어난다(SQLite로는 여러 인스턴스가 같은 DB 파일을 공유할 수
없어 불가능했던 구조 - Postgres 전환이 이 분리의 전제조건이었음).

FastAPI 앱(main.py)은 임포트하지 않고, 이 워커가 실제로 건드리는 테이블의
init_*()만 독립적으로 호출한다.

프로세스 하나 안에서도 job을 동시에 여러 개 처리한다 - 폴링 루프를
WORKER_CONCURRENCY(기본 MAX_CONCURRENT_BROWSERS)개의 독립된 스레드로
띄우고, 각 스레드가 각자 claim_next_job()/처리를 반복한다. cart_jobs의
claim_next_job()이 FOR UPDATE SKIP LOCKED로 원자적이라 여러 스레드(그리고
여러 워커 인스턴스)가 동시에 폴링해도 같은 job을 중복으로 집어가지 않는다
(부하 테스트로 실제 job이 한 번에 하나씩만 처리되고 있던 걸 발견 - 이전엔
단일 스레드 루프라 browser_semaphore가 있어도 동시에 경합할 대상 자체가
없었음)."""
import threading
import time
import traceback

from dotenv import load_dotenv
load_dotenv()

import browser_limit
import cart_add_logic
import cart_jobs
import popularity
import telegram_bot
import telegram_store
import vendors
import web_cart

POLL_INTERVAL_SECONDS = 2
WORKER_CONCURRENCY = browser_limit.MAX_CONCURRENT_BROWSERS


def _init_tables() -> None:
    vendors.init_store_vendor_table()
    vendors.init_session_table()
    web_cart.init_web_cart_table()
    telegram_store.init_telegram_tables()
    popularity.init_popularity_table()
    cart_jobs.init_cart_jobs_table()


def process_telegram_batch(job: dict) -> None:
    chat_id = job["chat_id"]
    store_id = job["store_id"]
    items = job["payload"]["items"]
    resolved_accounts = job["payload"]["resolved_accounts"]

    try:
        results, needs_followup = cart_add_logic.process_batch(store_id, items, resolved_accounts)
    except Exception as e:
        results, needs_followup = [f"(처리 중 예상치 못한 오류로 중단됨: {e})"], []

    cart_jobs.mark_done(job["id"], {"results": results, "needs_followup": needs_followup})
    telegram_bot._finish_processing(store_id)
    telegram_bot.send_message(chat_id, "담기 결과:\n\n" + "\n".join(results))

    if needs_followup:
        state = {
            "mode": "stockout", "store_id": store_id, "queue": needs_followup, "results": [], "current": None,
            "resolved_accounts": resolved_accounts,
        }
        telegram_bot._ask_next_stockout(chat_id, state)


def process_telegram_stockout(job: dict) -> None:
    chat_id = job["chat_id"]
    store_id = job["store_id"]
    item = job["payload"]["item"]

    result = cart_add_logic.add_single_item_to_cart(store_id, item)
    cart_jobs.mark_done(job["id"], result)

    state = telegram_store.get_disambig_state(chat_id)
    if not state or state.get("mode") != "stockout" or not state.get("queue"):
        return  # 이미 취소됐거나(사용자가 '취소') 다른 흐름으로 넘어감 - 방어적 처리

    if result.get("ok"):
        state["results"].append(f"✓ {item['item_name']} - {item['vendor_name']} 담기 완료")
        popularity.log_event(store_id, "wholesale", item["item_key"], item["item_name"], item["qty"])
    else:
        state["results"].append(f"✗ {item['item_name']} - {item['vendor_name']} 실패: {result.get('reason', '')}")

    state["queue"].pop(0)
    state["current"] = None
    telegram_bot._ask_next_stockout(chat_id, state)


def process_web_item(job: dict) -> None:
    store_id = job["store_id"]
    web_cart_item_id = job["web_cart_item_id"]
    item = job["payload"]["item"]
    with_fallback = job["payload"].get("with_fallback", False)

    if with_fallback:
        # /cart 페이지의 "담기" - 지금 장바구니에 이미 담긴 다른 상품들이 쓰는
        # 도매처 중에서만 조용히 자동 전환을 시도한다(텔레그램 봇과 동일한 로직).
        batch_vendors = {it["vendor_id"] for it in web_cart.list_items(store_id)}
        result, used_item, remaining_alts = cart_add_logic.add_item_with_batch_fallback(store_id, item, batch_vendors)
    else:
        # /compare의 대안 도매처 선택 - 이미 사용자가 특정 도매처를 골랐으므로
        # 자동 전환 없이 그 도매처로만 단발 시도한다.
        result = cart_add_logic.add_single_item_to_cart(store_id, item)
        used_item, remaining_alts = item, []

    if result.get("ok"):
        popularity.log_event(store_id, "wholesale", used_item["item_key"], used_item["item_name"], used_item["qty"])
        if web_cart_item_id is not None:
            web_cart.delete_item(store_id, web_cart_item_id)

    cart_jobs.mark_done(job["id"], {
        "ok": result.get("ok", False),
        "reason": result.get("reason"),
        "used_vendor_id": used_item["vendor_id"],
        "used_vendor_name": used_item.get("vendor_name", ""),
        "switched": used_item["vendor_id"] != item["vendor_id"],
        "remaining_alts": remaining_alts,
    })


def process_load_test_noop(job: dict) -> None:
    """실제 도매처/Playwright는 전혀 안 건드리는 가짜 담기 - 워커/큐 처리량만
    재보는 부하 테스트 전용. 실제 담기와 동일하게 browser_semaphore(동시 4개
    제한)는 그대로 받아서, 처리량 측정치가 실제 담기의 동시성 제약을
    똑같이 반영하도록 한다. cart_jobs.enqueue_load_test_noop()로만 만들어지며
    실제 서비스 코드 경로에서는 절대 생성되지 않는다."""
    duration = job["payload"].get("duration_seconds", 30)
    with browser_limit.browser_semaphore:
        time.sleep(duration)
    cart_jobs.mark_done(job["id"], {"ok": True, "note": "load test noop"})


_HANDLERS = {
    "telegram_batch": process_telegram_batch,
    "telegram_stockout": process_telegram_stockout,
    "web_item": process_web_item,
    "load_test_noop": process_load_test_noop,
}


def _poll_loop(worker_id: int) -> None:
    tag = f"[WORKER-{worker_id}]"
    while True:
        try:
            job = cart_jobs.claim_next_job()
            if not job:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            print(f"{tag} job {job['id']} ({job['kind']}) 처리 시작")
            try:
                _HANDLERS[job["kind"]](job)
                print(f"{tag} job {job['id']} 완료")
            except Exception as e:
                # 담기 실패(로그인 실패, 품절 등)는 정상적인 실패 케이스라
                # mark_failed로 job에만 기록한다 - 여기 걸리는 건 그 실패
                # 처리 자체가 예상 못한 예외로 죽은 경우(진짜 버그)라 알림도 보낸다.
                cart_jobs.mark_failed(job["id"], str(e))
                print(f"{tag} job {job['id']} 실패: {e}")
                traceback.print_exc()
                telegram_bot.alert_admin(
                    f"워커에서 job {job['id']} ({job['kind']}) 처리 중 예상 못한 예외\n\n"
                    f"{type(e).__name__}: {e}\n\n{traceback.format_exc()[-1500:]}"
                )
        except Exception as loop_error:
            # claim_next_job 자체가 실패하는 경우(DB 연결 문제 등) - 워커
            # 스레드가 죽지 않고 재시도하도록 폴링 루프 바깥도 잡아둔다.
            print(f"{tag} 폴링 루프에서 예상 못한 오류: {loop_error}")
            traceback.print_exc()
            telegram_bot.alert_admin(
                f"워커 폴링 루프 자체에서 예상 못한 예외\n\n{type(loop_error).__name__}: {loop_error}"
            )
            time.sleep(POLL_INTERVAL_SECONDS)


def run() -> None:
    _init_tables()
    print(f"[WORKER] 시작 - cart_jobs 큐 폴링 중 (동시 처리 {WORKER_CONCURRENCY}개)")
    threads = [
        threading.Thread(target=_poll_loop, args=(i,), name=f"poller-{i}", daemon=True)
        for i in range(WORKER_CONCURRENCY)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


if __name__ == "__main__":
    run()
