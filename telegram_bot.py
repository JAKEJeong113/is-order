# telegram_bot.py
"""텔레그램 발주봇: 발주리스트 수신 -> 캐시로 즉시 가격비교 -> 확인 답장 시 실제 담기."""
import os

import requests

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

CART_SUPPORTED_VENDORS = ("yamimall", "ccdome", "3bong")
KOREAN_TO_VENDOR_ID = {"야미몰": "yamimall", "과자생각": "ccdome", "삼봉몰": "3bong"}
VENDOR_MENU_TEXT = "등록할 도매처를 입력해주세요: 야미몰 / 과자생각 / 삼봉몰"


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


def _resolve_order_list(text: str) -> tuple[list[dict], list[str]]:
    """줄바꿈으로 구분된 상품명을 각각 가격비교해서 최저가 항목을 고른다."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    matched = []
    not_found = []

    for keyword in lines:
        result = price_compare.compare(keyword)
        groups = result.get("groups", [])
        if not groups:
            not_found.append(keyword)
            continue

        best_offer = groups[0]["offers"][0]
        if best_offer["vendor_id"] not in CART_SUPPORTED_VENDORS or not best_offer.get("product_url"):
            not_found.append(keyword)
            continue

        matched.append({
            "item_name": keyword,
            "vendor_id": best_offer["vendor_id"],
            "vendor_name": best_offer["vendor_name"],
            "product_url": best_offer["product_url"],
            "item_key": best_offer.get("goods_no") or best_offer["product_url"],
            "price": best_offer.get("price"),
            "qty": 1,
        })

    return matched, not_found


def _execute_cart_adds(chat_id, store_id: str, items: list[dict]) -> None:
    results = []
    for item in items:
        creds = vendors.get_store_vendor_credentials(store_id, item["vendor_id"])
        if not creds:
            results.append(
                f"✗ {item['item_name']} - {item['vendor_name']} 계정 미등록 "
                f"('계정등록'이라고 보내서 먼저 등록해주세요)"
            )
            continue

        login_id, login_pwd = creds
        try:
            if item["vendor_id"] == "yamimall":
                result = yamimall_bot.add_to_cart(login_id, login_pwd, item["product_url"], item["qty"])
            else:
                base_url = vendors.VENDORS[item["vendor_id"]]["base_url"]
                goods_no = item["item_key"]
                result = godomall_bot.add_to_cart(base_url, login_id, login_pwd, goods_no, item["qty"])
        except Exception as e:
            result = {"ok": False, "reason": str(e)}

        if result.get("ok"):
            results.append(f"✓ {item['item_name']} - {item['vendor_name']} 담기 완료")
            popularity.log_event(store_id, "wholesale", item["item_key"], item["item_name"], item["qty"])
        else:
            results.append(f"✗ {item['item_name']} - {item['vendor_name']} 실패: {result.get('reason', '')}")

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

        _execute_cart_adds(chat_id, store_name, pending)
        return

    # 새 발주 목록으로 처리
    matched, not_found = _resolve_order_list(text)
    if matched:
        telegram_store.save_pending_items(chat_id, matched)
    send_message(chat_id, _format_comparison(matched, not_found))
