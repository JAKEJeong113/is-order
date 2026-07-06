# telegram_bot.py
"""텔레그램 발주봇: 발주리스트 수신 -> 캐시로 즉시 가격비교 -> 확인 답장 시 실제 담기."""
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

import requests
from apscheduler.schedulers.background import BackgroundScheduler

import godomall_bot
import popularity
import price_compare
import telegram_store
import vendors
import yamimall_bot

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

CONFIRM_WORDS = {"확인", "네", "예", "ok", "okay", "yes", "go", "담아줘", "담아"}
CANCEL_WORDS = {"취소", "아니", "아니오", "no", "cancel"}
CRED_TRIGGER_WORDS = {"계정등록", "도매처등록", "도매처계정등록", "계정 등록"}
HELP_WORDS = {"도움말", "명령어", "help", "도움", "명령"}

CART_SUPPORTED_VENDORS = ("yamimall", "ccdome", "3bong")
KOREAN_TO_VENDOR_ID = {"야미몰": "yamimall", "과자생각": "ccdome", "삼봉몰": "3bong"}
VENDOR_ID_TO_KOREAN = {v: k for k, v in KOREAN_TO_VENDOR_ID.items()}
VENDOR_MENU_TEXT = "등록할 도매처를 입력해주세요: 야미몰 / 과자생각 / 삼봉몰"

HELP_TEXT = """사용 가능한 명령어입니다:

[발주하기]
상품명을 줄바꿈으로 여러 개 보내면 최저가로 자동 비교해드려요.
비교 결과가 오면 '확인'(담기) 또는 '취소'로 답장해주세요.
'하리보'처럼 여러 상품이 잡히면 번호로 골라달라고 먼저 물어봐요.

[계정등록]
도매처(야미몰/과자생각/삼봉몰) 아이디·비밀번호를 등록합니다.

[주거래처 설정 (도매처명)]
가격이 같을 때 우선으로 담을 도매처를 지정합니다. 예: 주거래처 설정 야미몰
- 주거래처 확인: 현재 설정 확인
- 주거래처 해제: 설정 해제

[도매처 활성화 (도매처명)] / [도매처 비활성화 (도매처명)]
가격비교에서 특정 도매처를 켜고 끌 수 있습니다. 예: 도매처 비활성화 삼봉몰
- 도매처 목록: 현재 켜짐/꺼짐 상태 확인

[도움말]
이 안내를 다시 봅니다."""

# 텔레그램 웹훅은 응답이 늦으면(수십 초 이상) 같은 메시지를 재전송하는데, 담기 작업은
# 도매처마다 실제 브라우저를 띄우는 Playwright 호출이라 30초~수분씩 걸릴 수 있다.
# 웹훅 요청 안에서 동기로 기다리면 재전송으로 인한 중복 처리가 발생하므로, 카탈로그
# 크롤링과 동일하게 별도 스레드(APScheduler)에 맡기고 웹훅은 즉시 응답한다.
_scheduler = BackgroundScheduler()
_scheduler.start()


def send_message(chat_id, text: str) -> None:
    if not BOT_TOKEN:
        print("[TELEGRAM] TELEGRAM_BOT_TOKEN이 설정되지 않아 메시지를 보낼 수 없습니다.")
        return
    try:
        requests.post(
            f"{API_BASE}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
    except Exception as e:
        print("[TELEGRAM] 메시지 전송 실패:", e)


def _format_comparison(matched: list[dict], not_found: list[str]) -> str:
    lines = ["아래 상품으로 최저가 담기를 준비했습니다:\n"]
    for m in matched:
        price_text = f"{m['price']:,}원" if m.get("price") else "가격 확인 필요"
        lines.append(f"• {m['item_name']} → {m['vendor_name']} {price_text}")

    if not_found:
        lines.append("\n찾지 못한 상품 (직접 확인 필요):")
        for name in not_found:
            lines.append(f"• {name}")

    if matched:
        lines.append("\n담을까요? '확인'이라고 답장해주세요. 취소하려면 '취소'라고 답장해주세요.")
    else:
        lines.append("\n담을 수 있는 상품이 없습니다.")

    return "\n".join(lines)


def _pick_best_offer(offers: list[dict], preferred_vendor: str | None) -> dict | None:
    """CART_SUPPORTED_VENDORS 중 담을 수 있는 후보만 놓고, 개당 최저가가 동률이면 주거래처를 우선한다."""
    candidates = [o for o in offers if o["vendor_id"] in CART_SUPPORTED_VENDORS and o.get("product_url")]
    if not candidates:
        return None

    # offers는 이미 개당 가격 오름차순 정렬되어 있음 (price_compare.compare 참고)
    lowest_unit_price = price_compare._unit_price(candidates[0])
    tied = [o for o in candidates if price_compare._unit_price(o) == lowest_unit_price]

    if preferred_vendor:
        for o in tied:
            if o["vendor_id"] == preferred_vendor:
                return o

    return candidates[0]


def _offer_to_item(item_name: str, best_offer: dict) -> dict:
    return {
        "item_name": item_name,
        "vendor_id": best_offer["vendor_id"],
        "vendor_name": best_offer["vendor_name"],
        "product_url": best_offer["product_url"],
        "item_key": best_offer.get("goods_no") or best_offer["product_url"],
        "price": best_offer.get("price"),
        "qty": 1,
    }


def _store_prefs(chat_id: str) -> tuple[set, str | None]:
    reg = telegram_store.get_registration(chat_id) or {}
    return set(reg.get("disabled_vendors") or []), reg.get("preferred_vendor")


def _classify_order_list(text: str, chat_id: str) -> dict:
    """줄바꿈으로 구분된 상품명을 분류한다.
    - matched: 후보가 하나뿐이라 바로 확정된 항목
    - ambiguous: 서로 다른 상품이 여러 개 잡혀서 사용자가 골라야 하는 키워드
    - not_found: 아예 후보를 찾지 못한 키워드"""
    disabled_vendors, preferred_vendor = _store_prefs(chat_id)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    matched, not_found, ambiguous = [], [], []

    for keyword in lines:
        groups = price_compare.compare(keyword).get("groups", [])
        if not groups:
            not_found.append(keyword)
            continue

        if len(groups) > 1:
            ambiguous.append(keyword)
            continue

        offers = [o for o in groups[0]["offers"] if o["vendor_id"] not in disabled_vendors]
        best_offer = _pick_best_offer(offers, preferred_vendor)
        if not best_offer:
            not_found.append(keyword)
            continue

        matched.append(_offer_to_item(keyword, best_offer))

    return {"matched": matched, "not_found": not_found, "ambiguous": ambiguous}


def _format_disambig_prompt(keyword: str, groups: list[dict]) -> str:
    shown = groups[:8]
    lines = [f"'{keyword}'에 해당하는 상품이 여러 개예요. 번호로 답장해주세요:\n"]
    for i, g in enumerate(shown, start=1):
        price_text = f"{g['best_price']:,}원" if g.get("best_price") else "가격 확인 필요"
        lines.append(f"{i}. {g['representative_name'].strip()} ({g['best_vendor_name']} {price_text})")
    if len(groups) > len(shown):
        lines.append(f"\n(그 외 {len(groups) - len(shown)}개 더 있어요. 상품명을 더 구체적으로 적어주시면 좁혀져요.)")
    lines.append("\n해당하는 상품이 없으면 '스킵'이라고 답장해주세요.")
    return "\n".join(lines)


def _ask_next_disambiguation(chat_id: str, state: dict) -> None:
    """큐에 남은 모호한 항목 중 다음 것을 물어본다. 큐가 비었으면 최종 확정한다."""
    if not state["queue"]:
        telegram_store.set_disambig_state(chat_id, None)
        matched = state["resolved"]
        not_found = state["not_found"]
        if matched:
            telegram_store.save_pending_items(chat_id, matched)
        send_message(chat_id, _format_comparison(matched, not_found))
        return

    keyword = state["queue"][0]
    groups = price_compare.compare(keyword).get("groups", [])

    if not groups:
        # 두 메시지 사이 캐시가 바뀌는 등 방어적 처리
        state["queue"].pop(0)
        state["not_found"].append(keyword)
        _ask_next_disambiguation(chat_id, state)
        return

    state["current"] = keyword
    telegram_store.set_disambig_state(chat_id, state)
    send_message(chat_id, _format_disambig_prompt(keyword, groups))


def _handle_disambiguation_reply(chat_id: str, state: dict, text: str) -> None:
    stripped = text.strip()
    keyword = state["current"]

    if stripped.lower() in CANCEL_WORDS:
        telegram_store.set_disambig_state(chat_id, None)
        send_message(chat_id, "발주 선택을 취소했습니다.")
        return

    if stripped in ("스킵", "skip"):
        state["queue"].pop(0)
        state["not_found"].append(keyword)
        state["current"] = None
        _ask_next_disambiguation(chat_id, state)
        return

    groups = price_compare.compare(keyword).get("groups", [])

    if not stripped.isdigit() or not (1 <= int(stripped) <= len(groups)):
        send_message(chat_id, f"1~{len(groups)} 사이의 번호로 답장해주세요. (해당 상품이 없으면 '스킵')")
        return

    disabled_vendors, preferred_vendor = _store_prefs(chat_id)
    chosen_group = groups[int(stripped) - 1]
    offers = [o for o in chosen_group["offers"] if o["vendor_id"] not in disabled_vendors]
    best_offer = _pick_best_offer(offers, preferred_vendor)

    state["queue"].pop(0)
    if best_offer:
        state["resolved"].append(_offer_to_item(keyword, best_offer))
    else:
        state["not_found"].append(keyword)

    state["current"] = None
    _ask_next_disambiguation(chat_id, state)


ITEM_CART_ADD_TIMEOUT_SECONDS = 90


def _add_single_item_to_cart(store_id: str, item: dict) -> dict:
    creds = vendors.get_store_vendor_credentials(store_id, item["vendor_id"])
    if not creds:
        return {
            "ok": False,
            "reason": "계정 미등록 ('계정등록'이라고 보내서 먼저 등록해주세요)",
        }

    login_id, login_pwd = creds
    if item["vendor_id"] == "yamimall":
        return yamimall_bot.add_to_cart(login_id, login_pwd, item["product_url"], item["qty"])

    base_url = vendors.VENDORS[item["vendor_id"]]["base_url"]
    return godomall_bot.add_to_cart(base_url, login_id, login_pwd, item["item_key"], item["qty"])


def _execute_cart_adds(chat_id, store_id: str, items: list[dict]) -> None:
    """도매처마다 실제 브라우저를 띄우는 작업이라 한 상품이 응답 없이 멈추면 전체가
    영원히 멈출 수 있다. 상품당 시간 제한을 걸어 하나가 멈춰도 나머지는 계속 진행하고,
    무슨 일이 있어도 최종 결과 메시지는 반드시 보낸다."""
    results = []
    try:
        for item in items:
            pool = ThreadPoolExecutor(max_workers=1)
            try:
                future = pool.submit(_add_single_item_to_cart, store_id, item)
                result = future.result(timeout=ITEM_CART_ADD_TIMEOUT_SECONDS)
            except FutureTimeoutError:
                result = {"ok": False, "reason": f"{ITEM_CART_ADD_TIMEOUT_SECONDS}초 넘게 응답이 없어 건너뜀. 직접 확인해주세요."}
            except Exception as e:
                result = {"ok": False, "reason": str(e)}
            finally:
                pool.shutdown(wait=False)

            if result.get("ok"):
                results.append(f"✓ {item['item_name']} - {item['vendor_name']} 담기 완료")
                popularity.log_event(store_id, "wholesale", item["item_key"], item["item_name"], item["qty"])
            else:
                results.append(f"✗ {item['item_name']} - {item['vendor_name']} 실패: {result.get('reason', '')}")
    except Exception as e:
        results.append(f"(처리 중 예상치 못한 오류로 중단됨: {e})")
    finally:
        send_message(chat_id, "담기 결과:\n\n" + "\n".join(results))


REGISTRATION_PROMPTS = {
    "store_name": ("store_name", "phone", "연락처(전화번호)를 입력해주세요."),
    "phone": ("phone", "business_number", "사업자등록번호를 입력해주세요."),
    "business_number": ("business_number", None, None),
}


def _handle_registration(chat_id: str, reg: dict, text: str) -> None:
    step = reg["registration_step"]
    field, next_step, next_prompt = REGISTRATION_PROMPTS[step]
    telegram_store.save_registration_field(chat_id, field, text, next_step)

    if next_step:
        send_message(chat_id, next_prompt)
    else:
        send_message(
            chat_id,
            "등록 신청이 완료되었습니다. 대표님 승인을 기다려주세요.\n"
            f"(내 chat_id: {chat_id})",
        )


def _handle_credential_flow(chat_id: str, store_id: str, reg: dict, text: str) -> None:
    step = reg["cred_step"]

    if step == "vendor":
        vendor_id = KOREAN_TO_VENDOR_ID.get(text.strip())
        if not vendor_id:
            send_message(chat_id, "찾을 수 없는 도매처예요.\n" + VENDOR_MENU_TEXT)
            return
        telegram_store.start_credential_registration(chat_id, vendor_id)
        vendor_name = vendors.VENDORS[vendor_id]["name"]
        send_message(chat_id, f"{vendor_name} 아이디를 입력해주세요.")
        return

    if step == "id":
        telegram_store.save_credential_id(chat_id, text.strip())
        vendor_name = vendors.VENDORS[reg["cred_vendor"]]["name"]
        send_message(chat_id, f"{vendor_name} 비밀번호를 입력해주세요.")
        return

    if step == "pwd":
        vendor_id = reg["cred_vendor"]
        vendor_name = vendors.VENDORS[vendor_id]["name"]
        vendors.set_store_vendor_credentials(store_id, vendor_id, reg["cred_temp_id"], text.strip())
        telegram_store.clear_credential_registration(chat_id)
        send_message(
            chat_id,
            f"{vendor_name} 계정이 등록되었습니다.\n"
            "다른 도매처도 등록하려면 '계정등록'이라고 다시 보내주세요.",
        )
        return


def _handle_preferred_vendor_command(chat_id: str, text: str) -> None:
    tokens = text.strip().split()  # tokens[0] == "주거래처"
    rest = " ".join(t for t in tokens[1:] if t != "설정").strip()

    if not rest or rest == "확인":
        reg = telegram_store.get_registration(chat_id) or {}
        preferred = reg.get("preferred_vendor")
        if preferred:
            send_message(chat_id, f"현재 주거래처는 {VENDOR_ID_TO_KOREAN.get(preferred, preferred)}입니다.")
        else:
            send_message(chat_id, "설정된 주거래처가 없습니다.\n'주거래처 설정 (도매처명)'으로 지정해주세요.")
        return

    if rest in ("해제", "취소"):
        telegram_store.set_preferred_vendor(chat_id, None)
        send_message(chat_id, "주거래처 설정을 해제했습니다.")
        return

    vendor_id = KOREAN_TO_VENDOR_ID.get(rest)
    if not vendor_id:
        send_message(chat_id, f"찾을 수 없는 도매처예요: {rest}\n{VENDOR_MENU_TEXT}")
        return

    telegram_store.set_preferred_vendor(chat_id, vendor_id)
    send_message(
        chat_id,
        f"주거래처를 {vendors.VENDORS[vendor_id]['name']}(으)로 설정했습니다. (가격이 같을 때 우선 담습니다)",
    )


def _handle_vendor_toggle_command(chat_id: str, text: str) -> None:
    tokens = text.strip().split()  # tokens[0] == "도매처"
    if len(tokens) < 2:
        send_message(chat_id, "사용법: 도매처 활성화 (도매처명) / 도매처 비활성화 (도매처명) / 도매처 목록")
        return

    action = tokens[1]

    if action in ("목록", "확인", "상태"):
        reg = telegram_store.get_registration(chat_id) or {}
        disabled = set(reg.get("disabled_vendors") or [])
        lines = ["현재 도매처 상태:"]
        for vid in CART_SUPPORTED_VENDORS:
            name = vendors.VENDORS[vid]["name"]
            state = "꺼짐" if vid in disabled else "켜짐"
            lines.append(f"• {name}: {state}")
        send_message(chat_id, "\n".join(lines))
        return

    if action not in ("활성화", "비활성화"):
        send_message(chat_id, "사용법: 도매처 활성화 (도매처명) / 도매처 비활성화 (도매처명) / 도매처 목록")
        return

    vendor_name = " ".join(tokens[2:]).strip()
    vendor_id = KOREAN_TO_VENDOR_ID.get(vendor_name)
    if not vendor_id:
        send_message(chat_id, f"찾을 수 없는 도매처예요: {vendor_name}\n{VENDOR_MENU_TEXT}")
        return

    enabled = action == "활성화"
    telegram_store.set_vendor_enabled_for_store(chat_id, vendor_id, enabled)
    send_message(chat_id, f"{vendors.VENDORS[vendor_id]['name']}를 {action}했습니다.")


def handle_update(update: dict) -> None:
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    text = (message.get("text") or "").strip()

    if not chat_id or not text:
        return

    reg = telegram_store.get_registration(chat_id)

    if reg is None:
        display_name = chat.get("first_name") or chat.get("username") or chat_id
        telegram_store.start_registration(chat_id, display_name)
        send_message(chat_id, "가맹점 등록을 시작할게요.\n지점명을 입력해주세요.")
        return

    if not reg["approved"]:
        if reg["registration_step"]:
            _handle_registration(chat_id, reg, text)
        else:
            send_message(
                chat_id,
                "등록은 완료됐고 대표님 승인을 기다리는 중입니다.\n"
                f"(내 chat_id: {chat_id})",
            )
        return

    store_name = reg["store_name"]
    normalized = text.lower()

    if reg.get("cred_step"):
        _handle_credential_flow(chat_id, store_name, reg, text)
        return

    disambig_state = telegram_store.get_disambig_state(chat_id)
    if disambig_state and disambig_state.get("current"):
        _handle_disambiguation_reply(chat_id, disambig_state, text)
        return

    if text.strip() in CRED_TRIGGER_WORDS:
        telegram_store.start_credential_menu(chat_id)
        send_message(chat_id, VENDOR_MENU_TEXT)
        return

    if normalized in CANCEL_WORDS:
        telegram_store.clear_pending(chat_id)
        send_message(chat_id, "취소되었습니다.")
        return

    if normalized in CONFIRM_WORDS:
        pending = telegram_store.get_pending_items(chat_id)
        if not pending:
            send_message(chat_id, "대기 중인 발주 목록이 없습니다. 먼저 상품 목록을 보내주세요.")
            return

        send_message(chat_id, "장바구니에 담는 중입니다. 잠시만 기다려주세요...")
        telegram_store.clear_pending(chat_id)

        # 담기는 시간이 걸려 웹훅 안에서 동기로 기다리면 텔레그램이 같은 메시지를 재전송해
        # 중복 처리가 생기므로, 백그라운드 스레드에 맡기고 웹훅은 바로 끝낸다.
        _scheduler.add_job(_execute_cart_adds, args=[chat_id, store_name, pending])
        return

    if text.strip() in HELP_WORDS:
        send_message(chat_id, HELP_TEXT)
        return

    if text.strip().startswith("주거래처"):
        _handle_preferred_vendor_command(chat_id, text)
        return

    if text.strip().startswith("도매처"):
        _handle_vendor_toggle_command(chat_id, text)
        return

    # 새 발주 목록으로 처리
    classified = _classify_order_list(text, chat_id)
    if classified["ambiguous"]:
        state = {
            "queue": classified["ambiguous"],
            "resolved": classified["matched"],
            "not_found": classified["not_found"],
            "current": None,
        }
        _ask_next_disambiguation(chat_id, state)
        return

    matched, not_found = classified["matched"], classified["not_found"]
    if matched:
        telegram_store.save_pending_items(chat_id, matched)
    send_message(chat_id, _format_comparison(matched, not_found))
