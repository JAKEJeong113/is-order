# catalog_crawler.py
"""활성화된 도매처의 전체상품을 크롤링해서 catalog_cache에 저장한다."""
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

import cafe24_bot
import catalog_cache
import godomall_bot
import vendors
import yamimall_bot

_crawl_lock = threading.Lock()
_is_crawling = False

# 도매처 하나가 응답 없이 멈추면(사이트 이슈 등) 전체 크롤링이 무한정 멈추는 걸
# 막기 위한 상한선. 이 시간을 넘기면 그 도매처만 실패 처리하고 다음으로 넘어간다.
VENDOR_CRAWL_TIMEOUT_SECONDS = 20 * 60


def is_crawl_running() -> bool:
    return _is_crawling


def _crawl_vendor_products(vendor_id: str, meta: dict, login_id: str, login_pwd: str) -> list[dict]:
    base_url = meta["base_url"]

    if vendor_id == "yamimall":
        return yamimall_bot.crawl_full_catalog(login_id, login_pwd)
    if vendor_id in ("ccdome", "3bong", "hdinter"):
        return godomall_bot.crawl_full_catalog(base_url, login_id, login_pwd, meta["catalog_category_code"])
    if vendor_id == "moomarket":
        return cafe24_bot.crawl_full_catalog(base_url, login_id, login_pwd, meta["catalog_category_code"])
    if vendor_id == "douyou":
        return yamimall_bot.crawl_full_catalog(
            login_id, login_pwd, base_url=base_url, category_codes=meta["catalog_category_code"],
        )
    raise ValueError("이 도매처는 아직 전체상품 수집을 지원하지 않습니다")


def crawl_vendor(vendor_id: str) -> dict:
    meta = vendors.VENDORS[vendor_id]
    creds = vendors.get_vendor_credentials(vendor_id)
    if not creds:
        return {"vendor_id": vendor_id, "ok": False, "error": "계정 정보 없음"}

    login_id, login_pwd = creds

    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(_crawl_vendor_products, vendor_id, meta, login_id, login_pwd)
        products = future.result(timeout=VENDOR_CRAWL_TIMEOUT_SECONDS)
    except FutureTimeoutError:
        error = f"{VENDOR_CRAWL_TIMEOUT_SECONDS // 60}분 넘게 응답이 없어 건너뜀"
        print(f"[CATALOG_CRAWLER] {vendor_id} 크롤링 실패:", error)
        catalog_cache.record_refresh_error(vendor_id, error)
        return {"vendor_id": vendor_id, "ok": False, "error": error}
    except Exception as e:
        print(f"[CATALOG_CRAWLER] {vendor_id} 크롤링 실패:", e)
        catalog_cache.record_refresh_error(vendor_id, str(e))
        return {"vendor_id": vendor_id, "ok": False, "error": str(e)}
    finally:
        pool.shutdown(wait=False)

    # 예외 없이 "성공"으로 끝났는데 상품이 0개면 진짜로 그 도매처가 텅 빈 게
    # 아니라 로그인 세션이 미묘하게 깨졌거나 사이트 구조가 바뀌는 등 크롤링
    # 자체가 조용히 실패한 경우일 가능성이 훨씬 크다(실측: 과자생각이 이 경로로
    # 캐시가 통째로 0건이 됨). 이 상태를 그대로 replace_vendor_catalog에 넘기면
    # 멀쩡했던 기존 캐시까지 빈 걸로 덮어써버리므로, 0건이면 기존 캐시를 그대로
    # 두고 실패로 기록해서 다음 크롤링 때 회복을 노린다.
    if not products:
        error = "크롤링 결과 0건 - 기존 캐시를 유지합니다(사이트 구조 변경/로그인 세션 문제 가능성)"
        print(f"[CATALOG_CRAWLER] {vendor_id} 크롤링 실패:", error)
        catalog_cache.record_refresh_error(vendor_id, error)
        return {"vendor_id": vendor_id, "ok": False, "error": error}

    catalog_cache.replace_vendor_catalog(vendor_id, products)
    return {"vendor_id": vendor_id, "ok": True, "product_count": len(products)}


def crawl_all_enabled() -> list[dict]:
    global _is_crawling
    if not _crawl_lock.acquire(blocking=False):
        print("[CATALOG_CRAWLER] 이미 크롤링이 진행 중이라 이번 실행은 건너뜁니다.")
        return []

    try:
        _is_crawling = True
        enabled_ids = vendors.get_enabled_vendor_ids()
        return [crawl_vendor(vid) for vid in enabled_ids]
    finally:
        _is_crawling = False
        _crawl_lock.release()
