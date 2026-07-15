# telegram_bot.py
"""텔레그램 발주봇: 발주리스트 수신 -> 캐시로 즉시 가격비교 -> 확인 답장 시 실제 담기.
실제 담기(Playwright)는 이 모듈이 직접 실행하지 않는다 - cart_jobs 큐에 등록만
하고, 별도 워커 프로세스(worker.py)가 처리한 뒤 이 모듈의 send_message/
_ask_next_stockout 등을 다시 호출해 결과를 알려준다(웹 서비스와 워커를 분리해
동시 처리량을 늘리기 위함)."""
import os
import re
import threading

import requests

import cart_add_logic
import cart_jobs
import popularity
import price_compare
import product_ranking
import telegram_store
import vendors

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
ADMIN_CHAT_ID = os.getenv("ADMIN_TELEGRAM_CHAT_ID", "")

# 텔레그램이 같은 웹훅 메시지를 재전송(중복 전달)하는 경우, "확인" 처리가 같은
# store_id에 대해 동시에 두 번 실행되면 같은 도매처 계정으로 거의 동시에 두 번
# 로그인하게 되어 사이트의 "중복 로그인 시 이전 세션 만료" 처리로 인해 방금 만든
# 세션이 곧바로 튕겨나가는 문제가 있었다(격리 상태 재현 테스트에서는 100% 정상
# 동작해 동시 중복 처리가 원인으로 확인됨). store_id별로 한 번에 하나의 담기
# 작업만 실행되도록 막는다.
_processing_lock = threading.Lock()
_processing_store_ids: set[str] = set()


def _try_start_processing(store_id: str) -> bool:
    with _processing_lock:
        if store_id in _processing_store_ids:
            return False
        _processing_store_ids.add(store_id)
        return True


def _finish_processing(store_id: str) -> None:
    with _processing_lock:
        _processing_store_ids.discard(store_id)

CONFIRM_WORDS = {"확인", "네", "예", "ok", "okay", "yes", "go", "담아줘", "담아"}
CANCEL_WORDS = {"취소", "아니", "아니오", "no", "cancel", "뒤로가기", "뒤로"}
# "계정추가"는 같은 도매처에 계정을 하나 더 등록할 때 쓰는 명령이지만, 내부
# 흐름(도매처->별명->아이디->비번)은 "계정등록"과 완전히 같다 - 첫 계정이든
# 추가 계정이든 결국 add_store_vendor_account 하나로 저장되기 때문. 뒤에
# 도매처명을 붙이면("계정추가 야미몰") 도매처 메뉴를 건너뛰고 바로 별명부터 묻는다.
CRED_TRIGGER_WORDS = {"계정등록", "도매처등록", "도매처계정등록", "계정 등록", "계정추가"}
DELETE_ACCOUNT_TRIGGER_WORDS = {"계정삭제", "도매처계정삭제"}
ACCOUNT_STATUS_TRIGGER_WORDS = {"계정현황", "계정목록", "계정확인"}
# 발주(도매처 가격비교)와는 별개로, 음료/과자 추천 카드에 등록해둔 쿠팡 링크를
# 바로 찾아주는 명령 - "이 상품 쿠팡 링크 좀" 같은 요청에 대응한다.
PRODUCT_LINK_TRIGGER_WORDS = {"구매링크", "쿠팡링크", "상품검색", "쿠팡검색"}
# 카탈로그 원본(바코드/추천판매가)을 조회하는 명령 - 위 구매링크(추천 카드에
# 등록된 쿠팡 링크)와는 별개로, 대표님이 관리하는 전체 상품 마스터 데이터를 찾는다.
BARCODE_TRIGGER_WORDS = {"바코드", "바코드검색", "바코드조회"}
HELP_WORDS = {"도움말", "명령어", "help", "도움", "명령"}

# 웹 장바구니(/cart)와 공유(vendors.py가 단일 소스).
CART_SUPPORTED_VENDORS = vendors.CART_SUPPORTED_VENDORS
KOREAN_TO_VENDOR_ID = {
    "야미몰": "yamimall", "과자생각": "ccdome", "삼봉몰": "3bong",
    "현동몰": "hdinter", "무마켓": "moomarket", "또요몰": "douyou",
}
VENDOR_ID_TO_KOREAN = {v: k for k, v in KOREAN_TO_VENDOR_ID.items()}
VENDOR_MENU_TEXT = "등록할 도매처를 입력해주세요: 야미몰 / 과자생각 / 삼봉몰 / 현동몰 / 무마켓 / 또요몰\n(취소하려면 '취소')"

HELP_TEXT = """사용 가능한 명령어입니다:

[발주하기]
상품명을 줄바꿈으로 여러 개 보내면 최저가로 자동 비교해드려요.
비교 결과가 오면 '확인'(담기) 또는 '취소'로 답장해주세요.
'하리보'처럼 여러 상품이 잡히면 번호로 골라달라고 먼저 물어봐요.
여러 개를 고르려면 쉼표로 (예: 2,4), 수량은 상품명 뒤에 숫자로 (예: 하리보 골드베렌 4).

[계정등록]
도매처(야미몰/과자생각/삼봉몰) 아이디·비밀번호를 등록합니다.

[계정추가 (도매처명)]
한 도매처에 계정을 추가로 등록합니다(다매장 운영 시 등). 예: 계정추가 야미몰
계정마다 별명을 붙일 수 있고, 도매처에 계정이 2개 이상이면 '확인' 후 어떤
계정으로 담을지 물어봐요.

[계정삭제 (도매처명)]
등록된 계정을 삭제합니다. 예: 계정삭제 야미몰 (번호로 어떤 계정인지 골라요)

[계정현황]
도매처별로 등록된 계정 목록을 확인합니다.

[취소 / 뒤로가기]
계정등록 등 진행 중인 절차를 언제든 중단하고 빠져나갈 수 있어요.

[구매링크 (상품명)]
음료/과자 추천에 등록된 상품의 쿠팡 구매 링크를 찾아드려요. 예: 구매링크 스프라이트

[바코드]
전체 상품 카탈로그에서 제품명이나 바코드로 찾아 추천판매가를 알려드려요.
'바코드'라고 보내면 제품명이나 바코드를 물어봐요.

[주거래처 설정 (도매처명)]
가격이 같을 때 우선으로 담을 도매처를 지정합니다. 예: 주거래처 설정 야미몰
- 주거래처 확인: 현재 설정 확인
- 주거래처 해제: 설정 해제

[도매처 활성화 (도매처명)] / [도매처 비활성화 (도매처명)]
가격비교에서 특정 도매처를 켜고 끌 수 있습니다. 예: 도매처 비활성화 삼봉몰
- 도매처 목록: 현재 켜짐/꺼짐 상태 확인

[도움말]
이 안내를 다시 봅니다."""

def send_message(chat_id, text: str) -> bool:
    if not BOT_TOKEN:
        print("[TELEGRAM] TELEGRAM_BOT_TOKEN이 설정되지 않아 메시지를 보낼 수 없습니다.")
        return False
    try:
        resp = requests.post(
            f"{API_BASE}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
        return resp.ok
    except Exception as e:
        print("[TELEGRAM] 메시지 전송 실패:", e)
        return False


def alert_admin(message: str) -> None:
    """서버(웹 서비스/워커)에서 예상 못한 예외가 터졌을 때 대표님 개인
    텔레그램으로 즉시 알린다. 담기 실패(로그인 실패, 품절 등 정상적인
    실패)는 대상이 아니다 - 그건 이미 봇 대화로 확인 가능해서 매번 알림이
    오면 스팸이 된다. 여기서는 "코드가 예상 못한 예외로 죽었다" 같은
    진짜 버그일 때만 부른다."""
    if not ADMIN_CHAT_ID:
        print("[ALERT] ADMIN_TELEGRAM_CHAT_ID가 설정되지 않아 관리자 알림을 보낼 수 없습니다:", message)
        return
    send_message(ADMIN_CHAT_ID, f"⚠️ 서버 에러 발생\n\n{message}")


def _format_comparison(matched: list[dict], not_found: list[str]) -> str:
    lines = ["아래 상품으로 최저가 담기를 준비했습니다:\n"]
    for m in matched:
        price_text = f"{m['price']:,}원" if m.get("price") else "가격 확인 필요"
        qty_text = f" x{m['qty']}개" if m.get("qty", 1) != 1 else ""
        lines.append(f"• {m['item_name']}{qty_text} → {m['vendor_name']} {price_text}")

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


def _offer_to_item(item_name: str, best_offer: dict, qty: int = 1, all_offers: list[dict] | None = None) -> dict:
    """all_offers를 넘기면(그룹 전체 후보), 선택된 도매처가 품절일 때 시도해볼 다른
    도매처 후보들을 alt_offers로 같이 담아둔다(이미 개당가 오름차순 정렬돼 있음)."""
    alt_offers = cart_add_logic.build_alt_offers(best_offer["vendor_id"], all_offers) if all_offers else []

    return {
        "item_name": item_name,
        "vendor_id": best_offer["vendor_id"],
        "vendor_name": best_offer["vendor_name"],
        "product_url": best_offer["product_url"],
        "item_key": cart_add_logic.offer_item_key(best_offer),
        "price": best_offer.get("price"),
        "qty": qty,
        "alt_offers": alt_offers,
    }


def _match_cred_trigger(text: str) -> tuple[bool, str | None]:
    """"계정등록"/"계정추가" 류 명령인지 확인하고, 뒤에 도매처명이 함께 왔으면
    (예: "계정추가 야미몰") 그 부분도 같이 돌려준다. 반환: (트리거 여부, 도매처 텍스트)."""
    stripped = text.strip()
    for trigger in CRED_TRIGGER_WORDS:
        if stripped == trigger:
            return True, None
        if stripped.startswith(trigger + " "):
            return True, stripped[len(trigger):].strip()
    return False, None


def _match_delete_trigger(text: str) -> tuple[bool, str | None]:
    """"계정삭제 (도매처명)" 명령인지 확인하고, 도매처명 부분을 같이 돌려준다."""
    stripped = text.strip()
    for trigger in DELETE_ACCOUNT_TRIGGER_WORDS:
        if stripped == trigger:
            return True, None
        if stripped.startswith(trigger + " "):
            return True, stripped[len(trigger):].strip()
    return False, None


def _match_product_link_trigger(text: str) -> tuple[bool, str | None]:
    """"구매링크 (상품명)" 류 명령인지 확인하고, 상품명 부분을 같이 돌려준다."""
    stripped = text.strip()
    for trigger in PRODUCT_LINK_TRIGGER_WORDS:
        if stripped == trigger:
            return True, None
        if stripped.startswith(trigger + " "):
            return True, stripped[len(trigger):].strip()
    return False, None


def _format_product_search_results(keyword: str, results: list[dict]) -> str:
    lines = [f"'{keyword}' 검색 결과:\n"]
    for r in results:
        price_text = f"{r['price']:,}원" if r.get("price") else "가격 확인 필요"
        lines.append(f"• {r['item_name']} ({price_text})\n{r['partners_link']}")
    return "\n\n".join(lines)


def _format_barcode_results(results: list[dict]) -> str:
    lines = []
    for r in results:
        price_text = f"{r['recommended_price']:,}원" if r.get("recommended_price") else "가격 정보 없음"
        lines.append(f"제품명: {r['menu_name']}\n바코드: {r['barcode']}\n추천판매가: {price_text}")
    return "\n\n".join(lines)


def _handle_barcode_lookup_reply(chat_id: str, text: str) -> None:
    telegram_store.set_disambig_state(chat_id, None)
    query = text.strip()
    if query.lower() in CANCEL_WORDS:
        send_message(chat_id, "바코드 조회를 취소했습니다.")
        return

    results = product_ranking.search_catalog(query)
    if not results:
        send_message(chat_id, f"'{query}'에 해당하는 상품을 찾지 못했어요.")
        return
    send_message(chat_id, _format_barcode_results(results))


def _store_prefs(chat_id: str) -> tuple[set, str | None]:
    reg = telegram_store.get_registration(chat_id) or {}
    return set(reg.get("disabled_vendors") or []), reg.get("preferred_vendor")


# 웹 compare 페이지와 공유(price_compare.py가 단일 소스).
_filter_groups_for_store = price_compare.filter_groups_for_store


# 상품명 뒤에 (공백 유무 상관없이) 순수 숫자(1~99)만 오면 수량으로 해석한다.
# "80g", "500ml"처럼 단위 글자가 붙어 있으면 애초에 끝이 숫자가 아니라 매칭 안 되고,
# "스위트러브100"처럼 3자리 이상 숫자로 끝나면(직전이 숫자인 1~2자리 부분만 떼어
# 수량으로 오인하지 않도록) (?<!\d)로 숫자 뭉치 전체 길이가 1~2자리일 때만 매칭한다.
QTY_SUFFIX_RE = re.compile(r"^(.*\S)\s*(?<!\d)(\d{1,2})$")


def _parse_item_line(line: str) -> tuple[str, int]:
    """'하리보 골드베렌 4' -> ('하리보 골드베렌', 4). 수량 표기가 없으면 1개로 취급."""
    m = QTY_SUFFIX_RE.match(line.strip())
    if m and 1 <= int(m.group(2)) <= 99:
        return m.group(1).strip(), int(m.group(2))
    return line.strip(), 1


def _classify_order_list(text: str, chat_id: str) -> dict:
    """줄바꿈으로 구분된 상품명을 분류한다.
    - matched: 후보가 하나뿐이라 바로 확정된 항목
    - ambiguous: 서로 다른 상품이 여러 개 잡혀서 사용자가 골라야 하는 항목 ({"keyword","qty"})
    - not_found: 아예 후보를 찾지 못한 키워드"""
    disabled_vendors, preferred_vendor = _store_prefs(chat_id)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    matched, not_found, ambiguous = [], [], []

    for line in lines:
        keyword, qty = _parse_item_line(line)
        groups = price_compare.compare(keyword).get("groups", [])
        groups = _filter_groups_for_store(groups, disabled_vendors)
        if not groups:
            not_found.append(keyword)
            continue

        if len(groups) > 1:
            ambiguous.append({"keyword": keyword, "qty": qty})
            continue

        offers = groups[0]["offers"]
        best_offer = _pick_best_offer(offers, preferred_vendor)
        if not best_offer:
            not_found.append(keyword)
            continue

        display_name = (best_offer.get("name") or keyword).strip()
        matched.append(_offer_to_item(display_name, best_offer, qty, all_offers=offers))

    return {"matched": matched, "not_found": not_found, "ambiguous": ambiguous}


# 상품명에 포장단위가 "(1묶음 10개입)"/"(1타 16개입)"처럼 별도 줄로 붙어있는 경우가
# 많아, 개당가/전체가를 같이 보여주려고 이름 텍스트에서 개수만 뽑아본다. 장바구니
# 담기 수량 계산에 쓰는 unit_qty 추출 로직과는 별개(표시 전용) - 그쪽은 이미 검증
# 완료된 로직이라 건드리지 않는다.
_DISPLAY_PACK_QTY_PATTERNS = [
    re.compile(r"[xX×]\s*(\d+)\s*개"),
    re.compile(r"(\d+)\s*개입"),
]


def _extract_display_pack_qty(text: str) -> int | None:
    for pattern in _DISPLAY_PACK_QTY_PATTERNS:
        match = pattern.search(text or "")
        if match:
            return int(match.group(1))
    return None


def _format_price_pair(price: int | None, name: str) -> str:
    if not price:
        return "가격 확인 필요"
    pack_qty = _extract_display_pack_qty(name)
    if pack_qty and pack_qty > 1:
        return f"개당 {price // pack_qty:,}원 ({pack_qty}개 {price:,}원)"
    return f"{price:,}원"


def _format_disambig_prompt(keyword: str, groups: list[dict]) -> str:
    shown = groups[:8]
    lines = [f"'{keyword}'에 해당하는 상품이 여러 개예요. 번호로 답장해주세요:\n"]
    for i, g in enumerate(shown, start=1):
        name = g["representative_name"].strip()
        # 포장단위 표기 때문에 상품명이 줄바꿈되는 경우, 이어지는 줄을 들여써서
        # 다음 번호와 헷갈리지 않게 한다.
        name_indented = name.replace("\n", "\n     ")
        price_text = _format_price_pair(g.get("best_price"), name)
        lines.append(f"{i}. {name_indented} — {g['best_vendor_name']} {price_text}")
    if len(groups) > len(shown):
        lines.append(f"\n(그 외 {len(groups) - len(shown)}개 더 있어요. 상품명을 더 구체적으로 적어주시면 좁혀져요.)")
    lines.append("\n여러 개를 담으려면 쉼표로 구분해서 답장해주세요. (예: 2,4)")
    lines.append("해당하는 상품이 없으면 '스킵'이라고 답장해주세요.")
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

    entry = state["queue"][0]
    keyword = entry["keyword"]
    disabled_vendors, _ = _store_prefs(chat_id)
    groups = price_compare.compare(keyword).get("groups", [])
    groups = _filter_groups_for_store(groups, disabled_vendors)

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
    entry = state["queue"][0]
    keyword, qty = entry["keyword"], entry["qty"]

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

    disabled_vendors, preferred_vendor = _store_prefs(chat_id)
    groups = price_compare.compare(keyword).get("groups", [])
    groups = _filter_groups_for_store(groups, disabled_vendors)

    # "2" 또는 "2,4" 처럼 쉼표(또는 공백)로 구분된 여러 번호를 한 번에 선택할 수 있다.
    raw_parts = [p for p in re.split(r"[,\s]+", stripped) if p]
    if not raw_parts or not all(p.isdigit() for p in raw_parts):
        send_message(chat_id, f"1~{len(groups)} 사이의 번호로 답장해주세요. 여러 개는 쉼표로 (예: 2,4) (해당 상품이 없으면 '스킵')")
        return

    indices = [int(p) - 1 for p in raw_parts]
    if any(i < 0 or i >= len(groups) for i in indices):
        send_message(chat_id, f"1~{len(groups)} 사이의 번호로 답장해주세요.")
        return

    added_any = False
    for idx in dict.fromkeys(indices):  # 중복 번호 제거, 순서는 유지
        offers = groups[idx]["offers"]
        best_offer = _pick_best_offer(offers, preferred_vendor)
        if best_offer:
            display_name = (best_offer.get("name") or keyword).strip()
            state["resolved"].append(_offer_to_item(display_name, best_offer, qty, all_offers=offers))
            added_any = True

    if not added_any:
        state["not_found"].append(keyword)

    state["queue"].pop(0)
    state["current"] = None
    _ask_next_disambiguation(chat_id, state)


def _format_stockout_prompt(entry: dict) -> str:
    lines = [f"'{entry['item_name']}'은(는) 선택된 도매처에서 품절이에요. 다른 도매처에서 담을까요? 번호로 답장해주세요:\n"]
    for i, o in enumerate(entry["alt_offers"], start=1):
        price_text = f"{o['price']:,}원" if o.get("price") else "가격 확인 필요"
        lines.append(f"{i}. {o['vendor_name']} {price_text}")
    lines.append("\n담지 않으려면 '스킵'이라고 답장해주세요.")
    return "\n".join(lines)


def _ask_next_stockout(chat_id: str, state: dict) -> None:
    """품절로 대기 중인 상품 큐에서 다음 것을 물어본다. 큐가 비었으면 그동안의
    대안 담기 결과를 모아서 보낸다."""
    if not state["queue"]:
        telegram_store.set_disambig_state(chat_id, None)
        if state["results"]:
            send_message(chat_id, "품절 대안 담기 결과:\n\n" + "\n".join(state["results"]))
        return

    entry = state["queue"][0]
    state["current"] = entry["item_name"]
    telegram_store.set_disambig_state(chat_id, state)
    send_message(chat_id, _format_stockout_prompt(entry))


def _handle_stockout_reply(chat_id: str, state: dict, text: str) -> None:
    stripped = text.strip()
    entry = state["queue"][0]

    if stripped.lower() in CANCEL_WORDS:
        telegram_store.set_disambig_state(chat_id, None)
        send_message(chat_id, "품절 대안 선택을 취소했습니다.")
        return

    if stripped in ("스킵", "skip"):
        state["queue"].pop(0)
        state["results"].append(f"✗ {entry['item_name']} - 품절(건너뜀)")
        state["current"] = None
        _ask_next_stockout(chat_id, state)
        return

    if not stripped.isdigit() or not (1 <= int(stripped) <= len(entry["alt_offers"])):
        send_message(chat_id, f"1~{len(entry['alt_offers'])} 사이의 번호로 답장해주세요. (해당 상품이 없으면 '스킵')")
        return

    alt_offer = entry["alt_offers"][int(stripped) - 1]
    item = _offer_to_item(entry["item_name"], alt_offer, entry["qty"])
    account_id = state.get("resolved_accounts", {}).get(item["vendor_id"])
    if account_id is not None:
        item["account_id"] = account_id

    telegram_store.set_disambig_state(chat_id, state)
    send_message(chat_id, "장바구니에 담는 중입니다. 잠시만 기다려주세요...")
    cart_jobs.enqueue_telegram_stockout(chat_id, state["store_id"], item)


def _format_account_choice_prompt(store_id: str, vendor_id: str) -> str:
    vendor_name = vendors.VENDORS[vendor_id]["name"]
    accounts = vendors.list_store_vendor_accounts(store_id, vendor_id)
    lines = [f"{vendor_name}에 등록된 계정이 여러 개예요. 어떤 계정으로 담을까요? 번호로 답장해주세요:\n"]
    for i, acc in enumerate(accounts, start=1):
        tag = " (기본)" if acc["is_default"] else ""
        lines.append(f"{i}. {acc['nickname']}{tag}")
    return "\n".join(lines)


def _ask_next_account_choice(chat_id: str, state: dict) -> None:
    """계정 선택이 필요한 도매처 큐에서 다음 것을 물어본다. 큐가 비면 그동안
    고른 계정들을 각 상품에 반영해서 실제 담기를 시작한다."""
    if not state["queue"]:
        telegram_store.set_disambig_state(chat_id, None)
        store_id = state["store_id"]
        if not _try_start_processing(store_id):
            send_message(chat_id, "다른 담기 작업이 진행 중이에요. 잠시 후 다시 '확인'을 보내주세요.")
            return

        items = state["pending_items"]
        for it in items:
            account_id = state["resolved_accounts"].get(it["vendor_id"])
            if account_id is not None:
                it["account_id"] = account_id

        send_message(chat_id, "장바구니에 담는 중입니다. 잠시만 기다려주세요...")
        cart_jobs.enqueue_telegram_batch(chat_id, store_id, items, state["resolved_accounts"])
        _finish_processing(store_id)
        return

    vendor_id = state["queue"][0]
    state["current"] = vendor_id
    telegram_store.set_disambig_state(chat_id, state)
    send_message(chat_id, _format_account_choice_prompt(state["store_id"], vendor_id))


def _handle_account_choice_reply(chat_id: str, state: dict, text: str) -> None:
    stripped = text.strip()
    vendor_id = state["queue"][0]

    if stripped.lower() in CANCEL_WORDS:
        telegram_store.set_disambig_state(chat_id, None)
        send_message(chat_id, "담기를 취소했습니다.")
        return

    accounts = vendors.list_store_vendor_accounts(state["store_id"], vendor_id)
    idx = int(stripped) - 1 if stripped.isdigit() else None
    if idx is None:
        for i, acc in enumerate(accounts):
            if acc["nickname"] == stripped:
                idx = i
                break

    if idx is None or not (0 <= idx < len(accounts)):
        send_message(chat_id, f"1~{len(accounts)} 사이의 번호로 답장해주세요.")
        return

    state["resolved_accounts"][vendor_id] = accounts[idx]["id"]
    state["queue"].pop(0)
    state["current"] = None
    _ask_next_account_choice(chat_id, state)


def _format_account_status(store_id: str) -> str:
    lines = ["도매처별 등록 계정 현황:"]
    for vendor_id in CART_SUPPORTED_VENDORS:
        vendor_name = vendors.VENDORS[vendor_id]["name"]
        accounts = vendors.list_store_vendor_accounts(store_id, vendor_id)
        lines.append(f"\n{vendor_name}")
        if not accounts:
            lines.append("(등록된 계정 없음)")
            continue
        for i, acc in enumerate(accounts, start=1):
            tag = " (기본)" if acc["is_default"] else ""
            lines.append(f"{i}. {acc['nickname'] or '기본'}{tag}")
    return "\n".join(lines)


def _format_account_delete_prompt(vendor_id: str, accounts: list[dict]) -> str:
    vendor_name = vendors.VENDORS[vendor_id]["name"]
    lines = [f"{vendor_name}에서 삭제할 계정을 번호로 답장해주세요:\n"]
    for i, acc in enumerate(accounts, start=1):
        tag = " (기본)" if acc["is_default"] else ""
        lines.append(f"{i}. {acc['nickname']}{tag}")
    lines.append("\n취소하려면 '취소'라고 답장해주세요.")
    return "\n".join(lines)


def _handle_account_delete_reply(chat_id: str, state: dict, text: str) -> None:
    stripped = text.strip()

    if stripped.lower() in CANCEL_WORDS:
        telegram_store.set_disambig_state(chat_id, None)
        send_message(chat_id, "계정 삭제를 취소했습니다.")
        return

    accounts = state["accounts"]
    idx = int(stripped) - 1 if stripped.isdigit() else None
    if idx is None:
        for i, acc in enumerate(accounts):
            if acc["nickname"] == stripped:
                idx = i
                break

    if idx is None or not (0 <= idx < len(accounts)):
        send_message(chat_id, f"1~{len(accounts)} 사이의 번호로 답장해주세요. (취소하려면 '취소')")
        return

    account = accounts[idx]
    telegram_store.set_disambig_state(chat_id, None)
    vendors.delete_store_vendor_account(state["store_id"], state["vendor_id"], account["id"])

    vendor_name = vendors.VENDORS[state["vendor_id"]]["name"]
    note = ""
    if account["is_default"]:
        remaining = vendors.list_store_vendor_accounts(state["store_id"], state["vendor_id"])
        new_default = next((a for a in remaining if a["is_default"]), None)
        if new_default:
            note = f" (이제 {new_default['nickname']}이(가) 기본 계정입니다)"
    send_message(chat_id, f"{vendor_name} 계정({account['nickname']})을 삭제했습니다.{note}")


REGISTRATION_PROMPTS = {
    "store_name": ("store_name", "phone", "연락처(전화번호)를 입력해주세요.\n(취소하려면 '취소')"),
    "phone": ("phone", "business_number", "사업자등록번호를 입력해주세요.\n(취소하려면 '취소')"),
    "business_number": ("business_number", None, None),
}


def _handle_registration(chat_id: str, reg: dict, text: str) -> None:
    if text.strip().lower() in CANCEL_WORDS:
        telegram_store.delete_store(chat_id)
        send_message(chat_id, "가맹점 등록을 취소했습니다. 다시 시작하려면 아무 메시지나 보내주세요.")
        return

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


def _send_nickname_prompt(chat_id: str, store_id: str, vendor_id: str) -> None:
    vendor_name = vendors.VENDORS[vendor_id]["name"]
    existing = vendors.list_store_vendor_accounts(store_id, vendor_id)
    hint = f"\n(현재 등록된 계정: {', '.join(a['nickname'] for a in existing)})" if existing else ""
    send_message(
        chat_id,
        f"{vendor_name} 계정 별명을 입력해주세요 (예: 본점, 2호점). "
        f"생략하려면 '건너뛰기'라고 입력해주세요.{hint}\n(취소하려면 '취소')",
    )


def _handle_credential_flow(chat_id: str, store_id: str, reg: dict, text: str) -> None:
    step = reg["cred_step"]

    if text.strip().lower() in CANCEL_WORDS:
        telegram_store.clear_credential_registration(chat_id)
        send_message(chat_id, "계정 등록을 취소했습니다.")
        return

    if step == "vendor":
        vendor_id = KOREAN_TO_VENDOR_ID.get(text.strip())
        if not vendor_id:
            send_message(chat_id, "찾을 수 없는 도매처예요.\n" + VENDOR_MENU_TEXT)
            return
        telegram_store.start_credential_registration(chat_id, vendor_id)
        _send_nickname_prompt(chat_id, store_id, vendor_id)
        return

    if step == "nickname":
        nickname = text.strip()
        if nickname in ("건너뛰기", "스킵", "skip"):
            nickname = ""
        telegram_store.save_credential_nickname(chat_id, nickname)
        vendor_name = vendors.VENDORS[reg["cred_vendor"]]["name"]
        send_message(chat_id, f"{vendor_name} 아이디를 입력해주세요.\n(취소하려면 '취소')")
        return

    if step == "id":
        telegram_store.save_credential_id(chat_id, text.strip())
        vendor_name = vendors.VENDORS[reg["cred_vendor"]]["name"]
        send_message(chat_id, f"{vendor_name} 비밀번호를 입력해주세요.\n(취소하려면 '취소')")
        return

    if step == "pwd":
        vendor_id = reg["cred_vendor"]
        vendor_name = vendors.VENDORS[vendor_id]["name"]
        account_id = vendors.add_store_vendor_account(
            store_id, vendor_id, reg.get("cred_nickname") or "", reg["cred_temp_id"], text.strip(),
        )
        telegram_store.clear_credential_registration(chat_id)
        accounts = vendors.list_store_vendor_accounts(store_id, vendor_id)
        account_nickname = next((a["nickname"] for a in accounts if a["id"] == account_id), "기본")
        send_message(
            chat_id,
            f"{vendor_name} 계정({account_nickname})이 등록되었습니다.\n"
            f"같은 도매처에 계정을 추가로 등록하려면 '계정추가 {vendor_name}'이라고 보내주세요.",
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


def _handle_admin_price_alert_reply(chat_id: str, text: str) -> bool:
    """대표님 개인 chat_id(ADMIN_CHAT_ID)에서 "전체발송"/"생략" 응답이 오면
    처리한다. 대기 중인(status='notified', 오늘 다이제스트로 이미 알려드린)
    최저가 알림이 없으면 아무 것도 안 하고 False를 돌려줘서 평소 로직(발주 등)
    으로 그대로 넘어가게 한다 - 일반 문구에서 우연히 같은 단어를 썼을 때
    오작동을 막기 위함."""
    if text not in ("전체발송", "생략", "스킵"):
        return False

    status = "broadcast" if text == "전체발송" else "skipped"
    alerts = product_ranking.resolve_pending_alerts(status)
    if not alerts:
        return False

    if status == "skipped":
        send_message(chat_id, "최저가 알림을 넘어갔습니다.")
        return True

    lines = ["🎉 최저가 소식!\n"]
    for a in alerts:
        old_low_text = f"{a['old_low']:,}원 → " if a["old_low"] else ""
        lines.append(f"• {a['item_name']} {old_low_text}{a['new_price']:,}원")
    message = "\n".join(lines)

    stores = [s for s in telegram_store.list_stores() if s["approved"]]
    sent = sum(1 for s in stores if send_message(s["chat_id"], message))
    send_message(chat_id, f"{sent}/{len(stores)}개 매장에 발송했습니다.")
    return True


def handle_update(update: dict) -> None:
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    text = (message.get("text") or "").strip()

    if not chat_id or not text:
        return

    if chat_id == ADMIN_CHAT_ID and _handle_admin_price_alert_reply(chat_id, text):
        return

    reg = telegram_store.get_registration(chat_id)

    if reg is None:
        display_name = chat.get("first_name") or chat.get("username") or chat_id
        telegram_store.start_registration(chat_id, display_name)
        send_message(chat_id, "가맹점 등록을 시작할게요.\n지점명을 입력해주세요.\n(취소하려면 '취소')")
        return

    if not reg["approved"]:
        if reg["registration_step"]:
            _handle_registration(chat_id, reg, text)
        elif reg.get("reject_reason"):
            send_message(
                chat_id,
                "가맹점 등록이 반려됐습니다.\n"
                f"사유: {reg['reject_reason']}\n"
                "문의사항이 있으면 대표님께 직접 연락해주세요.",
            )
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
        mode = disambig_state.get("mode")
        if mode == "stockout":
            _handle_stockout_reply(chat_id, disambig_state, text)
        elif mode == "account_select":
            _handle_account_choice_reply(chat_id, disambig_state, text)
        elif mode == "account_delete":
            _handle_account_delete_reply(chat_id, disambig_state, text)
        elif mode == "barcode_lookup":
            _handle_barcode_lookup_reply(chat_id, text)
        else:
            _handle_disambiguation_reply(chat_id, disambig_state, text)
        return

    is_cred_trigger, inline_vendor_text = _match_cred_trigger(text)
    if is_cred_trigger:
        if inline_vendor_text:
            vendor_id = KOREAN_TO_VENDOR_ID.get(inline_vendor_text)
            if not vendor_id:
                telegram_store.start_credential_menu(chat_id)
                send_message(chat_id, f"찾을 수 없는 도매처예요: {inline_vendor_text}\n" + VENDOR_MENU_TEXT)
                return
            telegram_store.start_credential_registration(chat_id, vendor_id)
            _send_nickname_prompt(chat_id, store_name, vendor_id)
        else:
            telegram_store.start_credential_menu(chat_id)
            send_message(chat_id, VENDOR_MENU_TEXT)
        return

    is_delete_trigger, delete_vendor_text = _match_delete_trigger(text)
    if is_delete_trigger:
        if not delete_vendor_text:
            send_message(chat_id, f"사용법: 계정삭제 (도매처명)\n예: 계정삭제 야미몰\n{VENDOR_MENU_TEXT}")
            return
        vendor_id = KOREAN_TO_VENDOR_ID.get(delete_vendor_text)
        if not vendor_id:
            send_message(chat_id, f"찾을 수 없는 도매처예요: {delete_vendor_text}\n{VENDOR_MENU_TEXT}")
            return
        accounts = vendors.list_store_vendor_accounts(store_name, vendor_id)
        if not accounts:
            send_message(chat_id, f"{vendors.VENDORS[vendor_id]['name']}에 등록된 계정이 없습니다.")
            return
        state = {
            "mode": "account_delete", "store_id": store_name, "vendor_id": vendor_id,
            "accounts": accounts, "current": vendor_id,
        }
        telegram_store.set_disambig_state(chat_id, state)
        send_message(chat_id, _format_account_delete_prompt(vendor_id, accounts))
        return

    if text.strip() in ACCOUNT_STATUS_TRIGGER_WORDS:
        send_message(chat_id, _format_account_status(store_name))
        return

    if text.strip() in BARCODE_TRIGGER_WORDS:
        state = {"mode": "barcode_lookup", "current": True}
        telegram_store.set_disambig_state(chat_id, state)
        send_message(chat_id, "제품명이나 바코드를 입력해주세요.")
        return

    is_link_trigger, link_keyword = _match_product_link_trigger(text)
    if is_link_trigger:
        if not link_keyword:
            send_message(chat_id, "사용법: 구매링크 (상품명)\n예: 구매링크 스프라이트")
            return
        results = product_ranking.search_products(link_keyword)
        if not results:
            send_message(chat_id, f"'{link_keyword}'에 해당하는 음료/과자 추천 상품을 찾지 못했어요.")
            return
        send_message(chat_id, _format_product_search_results(link_keyword, results))
        return

    if normalized in CANCEL_WORDS:
        telegram_store.clear_pending(chat_id)
        send_message(chat_id, "취소되었습니다.")
        return

    if normalized in CONFIRM_WORDS:
        # 텔레그램이 같은 "확인" 메시지를 재전송(중복 웹훅)하면 아래 get_pending_items/
        # clear_pending 사이의 순간에 두 요청이 동시에 들어와 같은 목록을 두 번 처리할 수
        # 있다 - 이 경우 같은 도매처 계정으로 거의 동시에 두 번 로그인하게 되어 사이트의
        # 중복 로그인 세션 만료 처리로 방금 만든 세션이 곧바로 튕겨나가는 문제가 있었다
        # (예: 크나버 웨이퍼 건 - 격리 재현 시엔 100% 정상이라 동시 중복 처리가 원인으로
        # 확인됨). store_id별로 한 번에 하나의 담기 작업만 실행되도록 막는다.
        if not _try_start_processing(store_name):
            return

        pending = telegram_store.get_pending_items(chat_id)
        if not pending:
            _finish_processing(store_name)
            send_message(chat_id, "대기 중인 발주 목록이 없습니다. 먼저 상품 목록을 보내주세요.")
            return

        telegram_store.clear_pending(chat_id)

        # 이번 발주에 쓰인 도매처 중 계정이 2개 이상 등록된 곳이 있으면 어느
        # 계정으로 담을지부터 물어봐야 한다. 그 사이엔 실제 담기가 시작되는 게
        # 아니므로 락을 쥐고 있을 필요가 없다(사용자가 답장하는 동안 다른 정상
        # 요청까지 막아버릴 수 있음) - 여기서는 풀어두고, 계정을 다 고른 뒤
        # _ask_next_account_choice에서 실행 스케줄링 시점에 다시 잡는다.
        vendor_ids_needing_choice = sorted({
            it["vendor_id"] for it in pending
            if len(vendors.list_store_vendor_accounts(store_name, it["vendor_id"])) >= 2
        })
        if vendor_ids_needing_choice:
            _finish_processing(store_name)
            state = {
                "mode": "account_select", "store_id": store_name,
                "queue": vendor_ids_needing_choice, "current": None,
                "pending_items": pending, "resolved_accounts": {},
            }
            _ask_next_account_choice(chat_id, state)
            return

        send_message(chat_id, "장바구니에 담는 중입니다. 잠시만 기다려주세요...")

        # 담기는 시간이 걸려 웹훅 안에서 동기로 기다리면 텔레그램이 같은 메시지를 재전송해
        # 중복 처리가 생기므로, 큐에 등록만 하고 웹훅은 바로 끝낸다 - 별도 워커
        # 프로세스(worker.py)가 처리한다.
        cart_jobs.enqueue_telegram_batch(chat_id, store_name, pending, {})
        _finish_processing(store_name)
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
