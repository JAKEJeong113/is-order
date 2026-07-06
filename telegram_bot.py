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

CART_SUPPORTED_VENDORS = ("yamimall", "ccdome", "3bong")


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


def _execute_cart_adds(chat_id, store_name: str, items: list[dict]) -> None:
    results = []
    for item in items:
        creds = vendors.get_vendor_credentials(item["vendor_id"])
        if not creds:
            results.append(f"✗ {item['item_name']} - {item['vendor_name']} 계정 정보 없음")
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
            popularity.log_event(store_name, "wholesale", item["item_key"], item["item_name"], item["qty"])
        else:
            results.append(f"✗ {item['item_name']} - {item['vendor_name']} 실패: {result.get('reason', '')}")

    send_message(chat_id, "담기 결과:\n\n" + "\n".join(results))


def handle_update(update: dict) -> None:
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    text = (message.get("text") or "").strip()

    if not chat_id or not text:
        return

    approved, store_name = telegram_store.is_approved(chat_id)

    if not approved:
        display_name = chat.get("first_name") or chat.get("username") or chat_id
        telegram_store.register_request(chat_id, display_name)
        send_message(
            chat_id,
            "아직 승인되지 않은 사용자입니다. 대표님께 승인을 요청해주세요.\n"
            f"(내 chat_id: {chat_id})",
        )
        return

    normalized = text.lower()

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
