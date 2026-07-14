# worker.py
"""cart_jobs 큐를 폴링해서 실제 담기(Playwright)를 처리하는 백그라운드 워커.
웹 서비스와 별도의 Render 서비스로 띄운다 - 워커 인스턴스를 늘리면 그만큼
동시 처리량이 늘어난다(SQLite로는 여러 인스턴스가 같은 DB 파일을 공유할 수
없어 불가능했던 구조 - Postgres 전환이 이 분리의 전제조건이었음).

FastAPI 앱(main.py)은 임포트하지 않고, 이 워커가 실제로 건드리는 테이블의
init_*()만 독립적으로 호출한다."""
import time
import traceback

from dotenv import load_dotenv
load_dotenv()

import cart_add_logic
import cart_jobs
import popularity
import telegram_bot
import telegram_store
import vendors
import web_cart

POLL_INTERVAL_SECONDS = 2


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


_HANDLERS = {
    "telegram_batch": process_telegram_batch,
    "telegram_stockout": process_telegram_stockout,
    "web_item": process_web_item,
}


def run() -> None:
    _init_tables()
    print("[WORKER] 시작 - cart_jobs 큐 폴링 중")
    while True:
        try:
            job = cart_jobs.claim_next_job()
            if not job:
                time.sleep(POLL_INTERVAL_SECONDS)
                continue

            print(f"[WORKER] job {job['id']} ({job['kind']}) 처리 시작")
            try:
                _HANDLERS[job["kind"]](job)
                print(f"[WORKER] job {job['id']} 완료")
            except Exception as e:
                # 담기 실패(로그인 실패, 품절 등)는 정상적인 실패 케이스라
                # mark_failed로 job에만 기록한다 - 여기 걸리는 건 그 실패
                # 처리 자체가 예상 못한 예외로 죽은 경우(진짜 버그)라 알림도 보낸다.
                cart_jobs.mark_failed(job["id"], str(e))
                print(f"[WORKER] job {job['id']} 실패: {e}")
                traceback.print_exc()
                telegram_bot.alert_admin(
                    f"워커에서 job {job['id']} ({job['kind']}) 처리 중 예상 못한 예외\n\n"
                    f"{type(e).__name__}: {e}\n\n{traceback.format_exc()[-1500:]}"
                )
        except Exception as loop_error:
            # claim_next_job 자체가 실패하는 경우(DB 연결 문제 등) - 워커
            # 프로세스가 죽지 않고 재시도하도록 폴링 루프 바깥도 잡아둔다.
            print(f"[WORKER] 폴링 루프에서 예상 못한 오류: {loop_error}")
            traceback.print_exc()
            telegram_bot.alert_admin(
                f"워커 폴링 루프 자체에서 예상 못한 예외\n\n{type(loop_error).__name__}: {loop_error}"
            )
            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    run()
