# godomall_bot.py
"""고도몰(Godomall) 플랫폼 공통 봇: 과자생각(ccdome), 삼봉몰(3bong)에서 재사용."""
import json
import os
import re
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import Page, sync_playwright, TimeoutError as PWTimeoutError

import browser_limit
import vendors

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR))
DEBUG_SCREENSHOT_PATH = DATA_DIR / "debug_login_failure.png"


def login_godomall(page: Page, base_url: str, login_id: str, login_pwd: str) -> None:
    """이 함수는 항상 "새로 로그인해야 하는 상황"에만 불린다. context에 예전
    쿠키가 남아있으면 login.php가 "이미 로그인됨"으로 오판해 다른 페이지로
    리다이렉트시킬 수 있어(야미몰/카페24에서 같은 문제를 겪음), 항상 쿠키를
    비우고 새로 시작한다."""
    page.context.clear_cookies()
    page.goto(f"{base_url}/member/login.php", wait_until="domcontentloaded", timeout=30000)
    page.fill("#loginId", login_id)
    page.fill("#loginPwd", login_pwd)
    page.locator("#formLogin").locator("button, input[type=submit]").first.click()

    # 로그인 성공 시 login.php를 벗어나기까지 걸리는 시간이 스토어마다 달라서(현동몰은
    # 고정 3초 대기보다 늦게 리다이렉트되는 경우가 있어 실패로 오판했었다), 고정 대기
    # 대신 URL이 바뀔 때까지 최대 8초 기다린 뒤에 최종 상태로 판단한다.
    try:
        page.wait_for_url(lambda url: "login.php" not in url, timeout=8000)
    except PWTimeoutError:
        pass

    if "login.php" in page.url:
        # 원인 파악을 위해 화면에 실제로 보이는 에러 메시지와 스크린샷을 같이 남긴다
        detail = ""
        try:
            error_el = page.locator("[class*=caution], .error, [class*=alert]").first
            if error_el.count() > 0 and error_el.is_visible():
                detail = f" / 화면 메시지: {error_el.inner_text().strip()}"
        except Exception:
            pass
        try:
            page.screenshot(path=str(DEBUG_SCREENSHOT_PATH))
        except Exception:
            pass
        raise RuntimeError(f"고도몰 로그인 실패 (아이디/비밀번호를 확인해주세요) / URL: {page.url}{detail}")

    # 비밀번호 변경 안내 팝업("다음에 변경") 무시하고 넘어가기
    later_btn = page.locator("#btnLater, button:has-text('다음에 변경')")
    if later_btn.count() > 0:
        later_btn.first.click(timeout=3000)
        page.wait_for_timeout(1000)


def _parse_price(text: str) -> int | None:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def _match_score(keyword: str, product_text: str) -> int:
    if not keyword or not product_text:
        return 0
    product_norm = _normalize_text(product_text)
    score = 0
    for word in keyword.split():
        if word and _normalize_text(word) in product_norm:
            score += 1
    return score


def _extract_unit_qty(text: str) -> int | None:
    match = re.search(r"[xX×]\s*(\d+)\s*개", text or "")
    return int(match.group(1)) if match else None


def search_candidates(page: Page, base_url: str, keyword: str, top_n: int = 3) -> list[dict]:
    """로그인된 상태에서 keyword로 검색해 검색어 일치도 상위 top_n개 후보를 반환."""
    url = f"{base_url}/goods/goods_search.php?keyword={quote(keyword)}"
    page.goto(url, wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(800)

    items = page.locator("li:has(a[href*='goods_view.php']):has(.item_price)")
    count = items.count()
    if count == 0:
        return []

    scored = []
    for i in range(min(count, 30)):
        item = items.nth(i)
        try:
            name = item.locator(".item_name").first.inner_text().strip()
            price_text = item.locator(".item_price").first.inner_text().strip()
            href = item.locator("a[href*='goods_view.php']").first.get_attribute("href")
        except Exception:
            continue

        score = _match_score(keyword, name)
        if score <= 0:
            continue

        goods_no_match = re.search(r"goodsNo=(\d+)", href or "")
        goods_no = goods_no_match.group(1) if goods_no_match else None

        scored.append({
            "name": name,
            "price": _parse_price(price_text),
            "price_text": price_text,
            "unit_qty": _extract_unit_qty(name),
            "goods_no": goods_no,
            "product_url": f"{base_url}/goods/goods_view.php?goodsNo={goods_no}" if goods_no else None,
            "score": score,
        })

    deduped = {}
    for r in scored:
        key = (r["name"], r["price"])
        if key not in deduped:
            deduped[key] = r

    result = sorted(deduped.values(), key=lambda r: r["score"], reverse=True)
    return result[:top_n]


def _extract_page_items(page: Page, base_url: str) -> list[dict]:
    items = page.locator("li:has(a[href*='goods_view.php']):has(.item_price)")
    count = items.count()
    results = []

    for i in range(count):
        item = items.nth(i)
        try:
            name = item.locator(".item_name").first.inner_text().strip()
            price_text = item.locator(".item_price").first.inner_text().strip()
            href = item.locator("a[href*='goods_view.php']").first.get_attribute("href")
        except Exception:
            continue

        goods_no_match = re.search(r"goodsNo=(\d+)", href or "")
        goods_no = goods_no_match.group(1) if goods_no_match else None

        results.append({
            "name": name,
            "price": _parse_price(price_text),
            "unit_qty": _extract_unit_qty(name),
            "goods_no": goods_no,
            "product_url": f"{base_url}/goods/goods_view.php?goodsNo={goods_no}" if goods_no else None,
        })

    return results


def _block_heavy_resources(page: Page) -> None:
    """크롤링은 텍스트/가격만 필요하므로 이미지·폰트·미디어를 차단해 메모리 사용을 줄인다."""
    page.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in ("image", "media", "font")
        else route.continue_(),
    )


def crawl_full_catalog(
    base_url: str, login_id: str, login_pwd: str, category_codes: str | list[str], max_pages: int = 100,
) -> list[dict]:
    """'전체상품' 개념의 카테고리(들)를 끝까지 페이지를 넘기며 전부 수집한다.
    스토어에 따라 전체상품이 단일 cateCd 하나로 되는 곳도 있고(과자생각/삼봉몰),
    그런 코드 없이 대분류 여러 개를 각각 순회해야 하는 곳도 있어(현동몰)
    category_codes로 문자열 하나 또는 리스트를 모두 받는다."""
    codes = [category_codes] if isinstance(category_codes, str) else category_codes
    all_products: dict[str, dict] = {}

    with browser_limit.browser_semaphore, sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-setuid-sandbox"],
        )
        # 페이지 하나로 카테고리를 전부(수십 페이지 이동) 돌면 브라우저 메모리가 계속
        # 쌓여서 Render 인스턴스가 OOM으로 재시작되는 문제가 있었다. 로그인 세션은
        # context 단위로 유지되니, 카테고리마다 페이지를 새로 만들어 메모리를 정리한다.
        context = browser.new_context()
        page = context.new_page()
        _block_heavy_resources(page)

        try:
            login_godomall(page, base_url, login_id, login_pwd)

            for category_code in codes:
                # (실제 원인 확인: 삼봉몰 "에낙" 시리즈 중 2종이 61페이지에 있었는데
                # max_pages가 60이라 아예 못 갔던 것 - 60 -> 100으로 늘렸다.)
                # 정렬 기준이 실시간 인기도 등으로 안정적일 거라는 보장이 없어서, 혹시
                # 크롤링 도중 상품 순서가 바뀌어 인접한 두 페이지가 우연히 같은 상품들을
                # 보여주더라도 바로 멈추지 않도록, 연속으로 여러 번 새 상품이 없을 때만
                # 끝난 것으로 보는 안전장치도 같이 둔다.
                consecutive_empty_pages = 0
                for page_no in range(1, max_pages + 1):
                    page.goto(
                        f"{base_url}/goods/goods_list.php?cateCd={category_code}&page={page_no}",
                        wait_until="domcontentloaded",
                        timeout=30000,
                    )
                    page.wait_for_timeout(600)

                    items = _extract_page_items(page, base_url)
                    if not items:
                        break

                    new_count = 0
                    for it in items:
                        key = it["goods_no"] or it["name"]
                        if key not in all_products:
                            all_products[key] = it
                            new_count += 1

                    if new_count == 0:
                        consecutive_empty_pages += 1
                        if consecutive_empty_pages >= 3:
                            break
                    else:
                        consecutive_empty_pages = 0

                    # 카테고리가 하나뿐인 스토어(과자생각/삼봉몰)는 카테고리 사이에만
                    # 페이지를 재생성하면 이 카테고리 하나만으로도 max_pages(최대 100)를
                    # 다 돌 때까지 페이지 하나로 버텨야 해서 OOM 위험이 있다. 카테고리
                    # 안에서도 일정 페이지마다 재생성한다.
                    if page_no % 20 == 0:
                        page.close()
                        page = context.new_page()
                        _block_heavy_resources(page)

                page.close()
                page = context.new_page()
                _block_heavy_resources(page)
        finally:
            browser.close()

    return list(all_products.values())


def fetch_candidates(base_url: str, login_id: str, login_pwd: str, keywords: list[str], top_n: int = 3) -> dict[str, list[dict]]:
    """여러 키워드에 대해 로그인 1회 후 후보 목록을 조회. {keyword: [candidate, ...]} 반환."""
    results: dict[str, list[dict]] = {}

    with browser_limit.browser_semaphore, sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-setuid-sandbox"],
        )
        page = browser.new_page()

        try:
            login_godomall(page, base_url, login_id, login_pwd)

            for keyword in keywords:
                try:
                    results[keyword] = search_candidates(page, base_url, keyword, top_n=top_n)
                except PWTimeoutError:
                    results[keyword] = []
                except Exception as e:
                    print(f"[GODOMALL] {base_url} 검색 실패 ({keyword}):", e)
                    results[keyword] = []
        finally:
            browser.close()

    return results


def add_to_cart(
    store_id: str, vendor_id: str, base_url: str, login_id: str, login_pwd: str, goods_no: str, qty: int,
) -> dict:
    """지점별 로그인 세션(쿠키)을 캐시해서, 저장된 세션이 있으면 로그인 과정을 건너뛴다.
    캐시된 세션이 만료됐으면(담기 버튼을 못 찾으면) 새로 로그인해서 한 번 더 시도한다."""
    with browser_limit.browser_semaphore, sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-setuid-sandbox"],
        )
        cached_state = vendors.get_session_state(store_id, vendor_id)
        context = browser.new_context(storage_state=cached_state) if cached_state else browser.new_context()
        page = context.new_page()

        # 야미몰/무마켓에서 클릭 성공(예외 없음)만 믿었다가 실제로는 하나도 안
        # 담기는 문제를 겪은 뒤로, 여기도 처음부터 alert 캡처 + 실제 장바구니
        # 개수 확인을 같이 한다.
        alert_messages: list[str] = []

        def _on_dialog(dialog):
            alert_messages.append(dialog.message)
            dialog.dismiss()

        page.on("dialog", _on_dialog)

        def _read_cart_count() -> int:
            try:
                text = page.locator("li[class*='cart' i]").first.inner_text(timeout=2000)
                digits = re.sub(r"[^\d]", "", text or "")
                return int(digits) if digits else 0
            except Exception:
                return -1

        def _try_add() -> str:
            alert_messages.clear()
            page.goto(
                f"{base_url}/goods/goods_view.php?goodsNo={goods_no}",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            # goto 직후(t=0) 시점 스냅샷 - 이미 이때부터 리다이렉트된 상태인지, 아니면
            # 잠시 뒤(지연된 JS 리다이렉트 등)에 벌어지는 일인지 구분하기 위해 기록
            # (크나버 건: 격리 재현 시엔 정상인데 실제 흐름에선 계속 실패해서 타이밍
            # 차이를 의심하게 됨).
            t0_url = page.url
            t0_cart_btn_count = page.locator("#cartBtn").count()

            # 상품에 따라 #cartBtn이 뜨는 데 고정 500ms보다 오래 걸릴 수 있어(실사용
            # 확인: 실제로는 구매 가능한 상품인데 렌더링이 늦어서 "담기 버튼을 찾지
            # 못함"으로 잘못 실패 처리된 사례 발견). 버튼이 실제로 나타날 때까지 최대
            # 8초 기다린 뒤에야 없다고 판단한다(진짜 품절 상품은 그래도 안 나타남).
            try:
                page.wait_for_selector("#cartBtn", timeout=8000)
            except PWTimeoutError:
                pass

            # 수량 입력(아래 target_qty 채우기) 전에 이미 #cartBtn이 없는 건지,
            # 아니면 수량을 채우는 순간(예: 재고 초과) 리다이렉트가 발생하는
            # 건지 구분하기 위한 스냅샷 (크나버 건: 완전히 새 세션으로도 실패가
            # 재현돼 세션 문제가 아닐 가능성이 커져서 추가).
            pre_qty_cart_btn_count = page.locator("#cartBtn").count()
            pre_qty_url = page.url

            before_count = _read_cart_count()

            # 실제 필드명은 "goodsCnt[]"(대괄호 포함, 배열 표기)라 input[name='goodsCnt']로는
            # 매칭이 안 돼 항상 수량이 기본값 1로만 담기고 있었다(진단으로 확인). onchange
            # 핸들러(goodsViewController.input_count_change)가 붙어있어 fill()이 발생시키는
            # input/change 이벤트로 정상적으로 반영된다.
            #
            # 상품에 따라 이 입력칸의 기본값이 1이 아니라 "구매 최소수량"(포장단위)으로 이미
            # 채워져 있는 경우가 있다(예: 16) - 무마켓/또요몰에서 겪은 것과 같은 패턴. qty는
            # "몇 세트를 담을지"를 의미하므로, 기본값을 그대로 두면 안 되고 "기본값 × qty"를
            # 채워야 한다(기본값이 1이면 결과도 그대로 qty와 같아서 일반 상품은 영향 없음).
            qty_input = page.locator("input[name='goodsCnt[]'], input[class*='goodsCnt'], input.qty_input").first
            unit_qty = None
            target_qty = None
            if qty_input.count() > 0:
                try:
                    unit_qty = int(qty_input.input_value() or "1")
                except Exception:
                    unit_qty = 1
                target_qty = max(unit_qty, 1) * qty
                qty_input.fill(str(target_qty))

            # 상세페이지의 실제 담기 버튼은 #cartBtn. (.btn_basket_cart는 상세페이지 하단
            # '함께 보면 좋은 상품' 추천 위젯용이라 다른 상품 goods_no를 가리킴 - 사용 금지)
            cart_btn = page.locator("#cartBtn")
            if cart_btn.count() == 0:
                # 파일로 남기는 진단은 이전에 이유 모르게 계속 기록이 안 남아서
                # (디스크/타이밍 등 원인 특정 실패), 텔레그램 실패 메시지에 바로
                # 보이도록 URL/본문 일부를 결과 문자열에 직접 실어보낸다.
                diag_url = ""
                diag_body = ""
                try:
                    diag_url = page.url
                except Exception:
                    pass
                try:
                    diag_body = page.locator("body").inner_text(timeout=2000)[:200].replace("\n", " ")
                except Exception:
                    pass
                pre_qty_note = (
                    f"(attempt={attempt_label} cached_state={bool(cached_state)} / "
                    f"t0 cartBtn={t0_cart_btn_count} url={t0_url} / "
                    f"수량입력전 cartBtn={pre_qty_cart_btn_count} url={pre_qty_url}, "
                    f"unit_qty={unit_qty}, target_qty={target_qty})"
                )
                return f"no_cart_button|{diag_url}|{pre_qty_note} {diag_body}"

            try:
                cart_btn.click(timeout=5000)
            except Exception:
                pass
            page.wait_for_timeout(1500)

            if "login.php" in page.url or any("로그인" in m for m in alert_messages):
                return "login_required"

            # "상품이 장바구니에 담겼습니다" 확인 팝업 닫기 (취소 = 현재 페이지 유지).
            # 이 시점엔 이미 담기 자체는 끝난 뒤라, 팝업이 다른 위젯의 숨겨진 버튼과
            # 텍스트가 겹쳐 클릭에 실패하더라도(관찰된 사례: 비밀번호 변경 팝업의
            # 숨겨진 버튼과 매칭) 담기 성공 자체를 실패로 보고하면 안 된다.
            try:
                close_btn = page.locator("button:has-text('취소'), button:has-text('확인')")
                if close_btn.count() > 0:
                    close_btn.first.click(timeout=3000)
                    page.wait_for_timeout(500)
            except Exception:
                pass

            # 클릭 성공/alert 없음만으로는 실제로 담겼는지 신뢰할 수 없다는 게 다른
            # 도매처 사례로 확인됐다. 새로고침 후 실제 장바구니 개수 배지로 최종 확인한다.
            try:
                page.reload(wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            after_count = _read_cart_count()

            if before_count >= 0 and after_count >= 0 and after_count <= before_count:
                if alert_messages:
                    return f"blocked:{alert_messages[-1]}"
                return "not_added"

            return "ok"

        try:
            logged_in_fresh = False
            attempt_label = "cached_session" if cached_state else "fresh_login"
            if not cached_state:
                login_godomall(page, base_url, login_id, login_pwd)
                logged_in_fresh = True

            outcome = _try_add()
            if (outcome.startswith("no_cart_button") or outcome in ("login_required", "not_added")) and cached_state:
                # 캐시된 세션이 만료됐을 수 있으니 새로 로그인해서 한 번 더 시도.
                # login_godomall은 쿠키만 지우고 같은 page 객체를 재사용하는데,
                # 실사용 중 쿠키를 지우고 재로그인해도 상품 상세페이지 대신 홈으로
                # 리다이렉트되며 재시도까지 실패하는 사례가 있었다(크나버 건) -
                # localStorage/sessionStorage 등 쿠키 외의 잔여 상태가 원인일 수
                # 있어, 재시도는 쿠키 지우기 대신 아예 새 컨텍스트로 시작한다.
                context.close()
                context = browser.new_context()
                page = context.new_page()
                page.on("dialog", _on_dialog)
                login_godomall(page, base_url, login_id, login_pwd)
                logged_in_fresh = True
                attempt_label = "fresh_context_retry"
                outcome = _try_add()

            if outcome.startswith("no_cart_button"):
                _, _, diag = outcome.partition("|")
                diag_note = f" [{diag[:200]}]" if diag else ""
                return {"ok": False, "goods_no": goods_no, "qty": qty, "reason": f"담기 버튼(#cartBtn)을 찾지 못함{diag_note}"}
            if outcome == "login_required":
                return {"ok": False, "goods_no": goods_no, "qty": qty, "reason": "로그인이 필요합니다 (아이디/비밀번호를 확인해주세요)"}
            if outcome == "not_added":
                return {"ok": False, "goods_no": goods_no, "qty": qty, "reason": "담기를 시도했지만 장바구니 수량이 늘지 않음 (재고/최소수량 등 확인 필요)"}
            if outcome.startswith("blocked:"):
                return {"ok": False, "goods_no": goods_no, "qty": qty, "reason": outcome[len("blocked:"):]}

            if logged_in_fresh:
                vendors.save_session_state(store_id, vendor_id, context.storage_state())

            return {"ok": True, "goods_no": goods_no, "qty": qty}
        except Exception as e:
            return {"ok": False, "goods_no": goods_no, "qty": qty, "reason": str(e)}
        finally:
            browser.close()
