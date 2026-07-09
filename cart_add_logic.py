# cart_add_logic.py
"""도매처 실제 담기(add_to_cart) 호출 + 품절 시 같은 발주 안에서 이미 쓰인
다른 도매처로 자동 전환하는 로직. 텔레그램 봇과 웹 장바구니(/cart) 양쪽에서
공유해서 쓴다(원래 텔레그램 봇에만 있던 로직을 분리했다)."""
import cafe24_bot
import godomall_bot
import vendors
import yamimall_bot

# 도매처마다 실패 문구가 조금씩 달라서("장바구니 버튼을 찾지 못함" vs "담기
# 버튼(#cartBtn)을 찾지 못함") 공통 부분인 "찾지 못함"만으로 느슨하게 잡는다.
# 품절 상품은 사이트가 담기 버튼/상품 자체를 안 보여주고 "품절" 표시로
# 바꿔치기하는 경우가 많아서, 명시적인 재고 문구 없이 버튼만 사라지는 것도
# 품절 신호로 취급한다.
STOCK_FAILURE_KEYWORDS = ("재고", "품절", "구매할 수 있는", "수량이 늘지 않", "찾지 못함")


def is_stock_failure(reason: str) -> bool:
    reason = reason or ""
    return any(kw in reason for kw in STOCK_FAILURE_KEYWORDS)


def offer_item_key(offer: dict) -> str:
    # price_compare 원본 offer면 "goods_no"를, alt_offers 항목(이미 이 모듈이
    # 만든 딕셔너리)이면 "goods_no" 필드가 아예 없고 "item_key"에 이미 올바른
    # 값(goods_no 또는 product_url)이 들어있다. 이 구분 없이 goods_no만 보고
    # product_url로 폴백하면, 도매처 상세페이지 URL 전체가 goodsNo 파라미터
    # 값으로 들어가 홈으로 리다이렉트되는 버그가 있었다(크나버 웨이퍼 건).
    return offer.get("goods_no") or offer.get("item_key") or offer["product_url"]


def build_alt_offers(chosen_vendor_id: str, all_offers: list[dict]) -> list[dict]:
    """가격비교 그룹 전체 offers 중 선택된 도매처를 제외하고, 담기 자동화가
    되는(CART_SUPPORTED_VENDORS) 후보만 alt_offers로 뽑는다(이미 단가순
    정렬돼 있음 - price_compare.compare 기준)."""
    alt_offers = []
    seen_vendors = {chosen_vendor_id}
    for o in all_offers or []:
        if o["vendor_id"] in seen_vendors or o["vendor_id"] not in vendors.CART_SUPPORTED_VENDORS or not o.get("product_url"):
            continue
        seen_vendors.add(o["vendor_id"])
        alt_offers.append({
            "vendor_id": o["vendor_id"],
            "vendor_name": o["vendor_name"],
            "product_url": o["product_url"],
            "item_key": offer_item_key(o),
            "price": o.get("price"),
        })
    return alt_offers


def add_single_item_to_cart(store_id: str, item: dict) -> dict:
    """item: {vendor_id, product_url, item_key, item_name, qty}.
    qty는 "몇 세트(구매단위의 배수)"를 담을지를 뜻한다 - 각 도매처 봇이
    페이지의 기본 구매단위(예: 1묶음 10개입이면 10) × qty로 실제 입력값을
    계산한다."""
    creds = vendors.get_store_vendor_credentials(store_id, item["vendor_id"])
    if not creds:
        return {"ok": False, "reason": "계정 미등록 (도매처 계정을 먼저 등록해주세요)"}

    login_id, login_pwd = creds
    base_url = vendors.VENDORS[item["vendor_id"]]["base_url"]

    if item["vendor_id"] == "yamimall":
        return yamimall_bot.add_to_cart(store_id, login_id, login_pwd, item["product_url"], item["qty"], keyword=item.get("item_name"))
    if item["vendor_id"] == "moomarket":
        return cafe24_bot.add_to_cart(store_id, base_url, login_id, login_pwd, item["product_url"], item["qty"])
    if item["vendor_id"] == "douyou":
        return yamimall_bot.add_to_cart_via_list(
            store_id, item["vendor_id"], login_id, login_pwd, item["product_url"], item["qty"],
            base_url=base_url, keyword=item.get("item_name"),
        )
    return godomall_bot.add_to_cart(store_id, item["vendor_id"], base_url, login_id, login_pwd, item["item_key"], item["qty"])


def add_item_with_batch_fallback(store_id: str, item: dict, batch_vendors: set) -> tuple[dict, dict, list[dict]]:
    """최초 선택한(보통 최저가) 도매처에서 담기를 시도한다. 품절류로 실패하면,
    이번 발주에 이미 포함된 다른 도매처(batch_vendors) 안에서만 조용히 순서대로
    재시도한다 - 배송을 최대한 한 도매처로 몰아주기 위해, 이번 발주에 안 쓰는
    새 도매처로는 자동으로 넘어가지 않는다. 그 안에서도 전부 품절이면, 배치 밖의
    남은 후보(다른 활성화된 도매처)를 반환해서 사용자가 고를 수 있게 한다.

    item에는 "alt_offers"(build_alt_offers로 만든, 같은 상품의 다른 도매처
    후보 목록)가 들어있어야 자동 전환이 가능하다.

    반환: (최종 결과, 실제로 시도한 item, 사용자가 골라야 할 배치 밖 대안 목록)"""
    tried_vendor_ids = {item["vendor_id"]}
    result = add_single_item_to_cart(store_id, item)
    used_item = item

    if not result.get("ok") and is_stock_failure(result.get("reason", "")):
        for alt in item.get("alt_offers") or []:
            if alt["vendor_id"] not in batch_vendors or alt["vendor_id"] in tried_vendor_ids:
                continue
            tried_vendor_ids.add(alt["vendor_id"])
            alt_item = {
                "item_name": item["item_name"],
                "vendor_id": alt["vendor_id"],
                "vendor_name": alt["vendor_name"],
                "product_url": alt["product_url"],
                "item_key": offer_item_key(alt),
                "price": alt.get("price"),
                "qty": item["qty"],
                "alt_offers": [],
            }
            alt_result = add_single_item_to_cart(store_id, alt_item)
            result, used_item = alt_result, alt_item
            if alt_result.get("ok") or not is_stock_failure(alt_result.get("reason", "")):
                # 성공했거나, 품절이 아닌 다른 이유(로그인 등)면 더 자동 재시도하지 않는다
                break

    remaining_alts = []
    if not result.get("ok") and is_stock_failure(result.get("reason", "")):
        remaining_alts = [o for o in (item.get("alt_offers") or []) if o["vendor_id"] not in tried_vendor_ids]

    return result, used_item, remaining_alts
