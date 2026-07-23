# cart_add_logic.py
"""도매처 실제 담기(add_to_cart) 호출 + 품절 시 같은 발주 안에서 이미 쓰인
다른 도매처로 자동 전환하는 로직. 텔레그램 봇과 웹 장바구니(/cart) 양쪽에서
공유해서 쓴다(원래 텔레그램 봇에만 있던 로직을 분리했다)."""
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

import cafe24_bot
import godomall_bot
import popularity
import vendors
import yamimall_bot

# 도매처 봇은 매 add_to_cart 호출마다 독립된 Playwright 브라우저를 새로 띄우고
# (vendors.get_session_state로 캐시해둔 쿠키를 storage_state로 로드) 끝나면 바로
# 닫는다 - 같은 (도매처, 계정)이라도 프로세스 내에서 세션 객체를 공유하지 않으므로
# Python 레벨에서는 동시 처리가 안전하다. 그래서 배치 안 상품은 (도매처, 계정)
# 그룹 없이 전부 동시에 처리한다. 실제 동시 브라우저 개수 상한은
# browser_limit.browser_semaphore가 전역으로 한 번 더 막아주므로, 여기서는 상품
# 수만큼 스레드를 넉넉히 열어도 무해하다(자리 없으면 대기).
#
# (※ 2026-07-23: 같은 계정끼리도 완전 병렬로 했다가, 실제 37개짜리 배치에서
# "품절로 오판돼 다른 도매처로 자동 전환됐는데 실제로는 원래 도매처에도 이미
# 담겨서 두 도매처 모두에 중복 발주"가 발생해 한때 순차 처리로 되돌렸었다.
# 원인은 yamimall_bot.add_to_cart의 성공판정이 헤더의 실시간 장바구니 개수
# 배지(.cart_prod_cnt_class) 전후 비교였는데, 같은 계정으로 여러 담기가 동시에
# 벌어지면 이 배지가 신뢰할 수 없어졌기 때문이다. yamimall_bot의 성공판정을
# cart.php에서 상품 코드로 직접 확인하는 방식(+ 서버 반영 지연 흡수용 재시도)
# 으로 교체한 뒤, 실계정으로 13개 동시 담기 x 5회 반복(총 65건) 재검증 - 오판
# 0건 확인 후 다시 완전 병렬로 전환.
MAX_PARALLEL_ITEMS = 8

# 배치 내 다른 도매처로 자동 재시도할 때는 도매처 봇을 여러 번 호출할 수 있어
# 한 상품당 예산을 넉넉히 잡는다 (품절 재시도 최대 2~3곳 가정).
ITEM_CART_ADD_WITH_FALLBACK_TIMEOUT_SECONDS = 240

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
    """item: {vendor_id, product_url, item_key, item_name, qty, account_id?}.
    qty는 "몇 세트(구매단위의 배수)"를 담을지를 뜻한다 - 각 도매처 봇이
    페이지의 기본 구매단위(예: 1묶음 10개입이면 10) × qty로 실제 입력값을
    계산한다. account_id가 없으면 해당 도매처의 기본 계정을 쓴다(계정이
    하나뿐인 지점은 신경 쓸 필요 없음)."""
    account = vendors.resolve_store_vendor_account(store_id, item["vendor_id"], item.get("account_id"))
    if not account:
        return {"ok": False, "reason": "계정 미등록 (도매처 계정을 먼저 등록해주세요)"}

    login_id, login_pwd = account["login_id"], account["login_pwd"]
    base_url = vendors.VENDORS[item["vendor_id"]]["base_url"]
    # 계정별로 로그인 세션(쿠키)을 따로 캐시해야 한다 - store_id만으로 캐시하면
    # 같은 도매처의 다른 계정 세션을 잘못 재사용해 엉뚱한 계정으로 주문이 들어갈
    # 수 있다. 봇 모듈들은 이 값을 세션 캐시 키로만 쓰고 다른 용도로 쓰지 않는다.
    session_key = f"{store_id}#acct{account['id']}"

    if item["vendor_id"] == "yamimall":
        return yamimall_bot.add_to_cart(session_key, login_id, login_pwd, item["product_url"], item["qty"], keyword=item.get("item_name"))
    if item["vendor_id"] == "moomarket":
        return cafe24_bot.add_to_cart(session_key, base_url, login_id, login_pwd, item["product_url"], item["qty"])
    if item["vendor_id"] == "douyou":
        return yamimall_bot.add_to_cart_via_list(
            session_key, item["vendor_id"], login_id, login_pwd, item["product_url"], item["qty"],
            base_url=base_url, keyword=item.get("item_name"),
        )
    return godomall_bot.add_to_cart(session_key, item["vendor_id"], base_url, login_id, login_pwd, item["item_key"], item["qty"])


def add_item_with_batch_fallback(
    store_id: str, item: dict, batch_vendors: set, resolved_accounts: dict | None = None,
) -> tuple[dict, dict, list[dict]]:
    """최초 선택한(보통 최저가) 도매처에서 담기를 시도한다. 품절류로 실패하면,
    이번 발주에 이미 포함된 다른 도매처(batch_vendors) 안에서만 조용히 순서대로
    재시도한다 - 배송을 최대한 한 도매처로 몰아주기 위해, 이번 발주에 안 쓰는
    새 도매처로는 자동으로 넘어가지 않는다. 그 안에서도 전부 품절이면, 배치 밖의
    남은 후보(다른 활성화된 도매처)를 반환해서 사용자가 고를 수 있게 한다.

    item에는 "alt_offers"(build_alt_offers로 만든, 같은 상품의 다른 도매처
    후보 목록)가 들어있어야 자동 전환이 가능하다. resolved_accounts는
    {vendor_id: account_id} - 이번 발주에서 이미 계정을 골라둔 도매처로
    자동 전환될 때, 엉뚱하게 그 도매처의 기본 계정이 아니라 실제 고른
    계정으로 담기게 하기 위함이다.

    반환: (최종 결과, 실제로 시도한 item, 사용자가 골라야 할 배치 밖 대안 목록)"""
    resolved_accounts = resolved_accounts or {}
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
            account_id = resolved_accounts.get(alt["vendor_id"])
            if account_id is not None:
                alt_item["account_id"] = account_id
            alt_result = add_single_item_to_cart(store_id, alt_item)
            result, used_item = alt_result, alt_item
            if alt_result.get("ok") or not is_stock_failure(alt_result.get("reason", "")):
                # 성공했거나, 품절이 아닌 다른 이유(로그인 등)면 더 자동 재시도하지 않는다
                break

    remaining_alts = []
    if not result.get("ok") and is_stock_failure(result.get("reason", "")):
        remaining_alts = [o for o in (item.get("alt_offers") or []) if o["vendor_id"] not in tried_vendor_ids]

    return result, used_item, remaining_alts


def _process_single_item(
    store_id: str, item: dict, batch_vendors: set, resolved_accounts: dict,
) -> tuple[str, dict | None]:
    """상품 하나를 담고 결과 메시지 한 줄(+ 품절 후속확인이 필요하면 그 항목)을
    만든다. process_batch가 상품마다(동시에) 호출한다."""
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(add_item_with_batch_fallback, store_id, item, batch_vendors, resolved_accounts)
        result, used_item, remaining_alts = future.result(timeout=ITEM_CART_ADD_WITH_FALLBACK_TIMEOUT_SECONDS)
    except FutureTimeoutError:
        result = {"ok": False, "reason": f"{ITEM_CART_ADD_WITH_FALLBACK_TIMEOUT_SECONDS}초 넘게 응답이 없어 건너뜀. 직접 확인해주세요."}
        used_item, remaining_alts = item, []
    except Exception as e:
        result = {"ok": False, "reason": str(e)}
        used_item, remaining_alts = item, []
    finally:
        pool.shutdown(wait=False)

    # 품절 자동 재시도로 최초 견적 때와 다른 도매처에 담기게 되면, 사용자가
    # "왜 최저가가 아니지?"라고 버그로 오해할 수 있어 그 사실을 명시한다.
    switched_note = (
        f" (※최초 선택하신 {item['vendor_name']}이(가) 품절이라 다른 도매처로 자동 변경됨)"
        if used_item["vendor_id"] != item["vendor_id"] else ""
    )

    if result.get("ok"):
        line = f"✓ {used_item['item_name']} - {used_item['vendor_name']} 담기 완료{switched_note}"
        popularity.log_event(store_id, "wholesale", used_item["item_key"], used_item["item_name"], used_item["qty"])
        followup = None
    elif remaining_alts:
        line = f"⚠ {used_item['item_name']} - {item['vendor_name']} 등 이번 발주에 포함된 도매처 모두 품절 (다른 도매처 대안 확인해서 곧 다시 안내드릴게요)"
        followup = {"item_name": used_item["item_name"], "qty": used_item["qty"], "alt_offers": remaining_alts}
    else:
        line = f"✗ {used_item['item_name']} - {used_item['vendor_name']} 실패{switched_note}: {result.get('reason', '')}"
        followup = None

    return line, followup


def process_batch(
    store_id: str, items: list[dict], resolved_accounts: dict | None = None,
    on_progress=None,
) -> tuple[list[str], list[dict]]:
    """텔레그램 "확인" 한 번에 담을 상품 목록 전체를 처리한다(원래
    telegram_bot._execute_cart_adds 안에 있던 루프 - worker.py가 재사용할 수
    있도록 텔레그램 전용 모듈에서 분리했다). 상품마다 완전히 독립된 브라우저
    세션을 쓰므로((도매처, 계정)이 같아도 안전함이 실측으로 확인됨) 전체 상품을
    동시에 처리한다 - 실제 동시 브라우저 개수는 browser_limit.browser_semaphore가
    전역으로 한 번 더 제한하므로 시스템이 바쁠 때는 자동으로 대기하고 여유
    있을 때만 병렬로 진행된다. 도매처마다 실제 브라우저를 띄우는 작업이라 한
    상품이 응답 없이 멈추면 전체가 영원히 멈출 수 있어, 상품당 시간 제한을
    걸어 하나가 멈춰도 나머지는 계속 진행한다.

    on_progress(done, total)이 주어지면 상품을 하나 처리할 때마다 호출한다 -
    동시에 여러 개가 처리되는 중에도 몇 분씩 걸릴 수 있는데, 그동안 아무 응답이
    없으면 멈춘 것처럼 보이는 문제를 호출부(worker.py)가 중간 진행 메시지로
    완화할 수 있게 한다.

    반환: (결과 메시지 줄 목록 - 원래 items 순서 그대로, 품절로 후속 확인이
    필요한 항목 목록 [{item_name, qty, alt_offers}])."""
    resolved_accounts = resolved_accounts or {}
    batch_vendors = {it["vendor_id"] for it in items}
    total = len(items)
    if total == 0:
        return [], []

    ordered_lines: list[str | None] = [None] * total
    needs_followup: list[dict] = []
    followup_lock = threading.Lock()
    progress_lock = threading.Lock()
    done_count = 0

    def run_one(idx: int, item: dict) -> None:
        nonlocal done_count
        line, followup = _process_single_item(store_id, item, batch_vendors, resolved_accounts)
        ordered_lines[idx] = line
        if followup:
            with followup_lock:
                needs_followup.append(followup)
        if on_progress:
            with progress_lock:
                done_count += 1
                current = done_count
            on_progress(current, total)

    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_ITEMS, total)) as pool:
        futures = [pool.submit(run_one, idx, item) for idx, item in enumerate(items)]
        for f in futures:
            f.result()

    return ordered_lines, needs_followup
