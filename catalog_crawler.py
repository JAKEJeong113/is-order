# catalog_crawler.py
"""활성화된 도매처의 전체상품을 크롤링해서 catalog_cache에 저장한다."""
import catalog_cache
import godomall_bot
import vendors
import yamimall_bot


def crawl_vendor(vendor_id: str) -> dict:
    meta = vendors.VENDORS[vendor_id]
    creds = vendors.get_vendor_credentials(vendor_id)
    if not creds:
        return {"vendor_id": vendor_id, "ok": False, "error": "계정 정보 없음"}

    login_id, login_pwd = creds
    base_url = meta["base_url"]

    try:
        if vendor_id == "yamimall":
            products = yamimall_bot.crawl_full_catalog(login_id, login_pwd)
        elif vendor_id in ("ccdome", "3bong"):
            products = godomall_bot.crawl_full_catalog(base_url, login_id, login_pwd, meta["catalog_category_code"])
        else:
            return {"vendor_id": vendor_id, "ok": False, "error": "이 도매처는 아직 전체상품 수집을 지원하지 않습니다"}
    except Exception as e:
        print(f"[CATALOG_CRAWLER] {vendor_id} 크롤링 실패:", e)
        catalog_cache.record_refresh_error(vendor_id, str(e))
        return {"vendor_id": vendor_id, "ok": False, "error": str(e)}

    catalog_cache.replace_vendor_catalog(vendor_id, products)
    return {"vendor_id": vendor_id, "ok": True, "product_count": len(products)}


def crawl_all_enabled() -> list[dict]:
    enabled_ids = vendors.get_enabled_vendor_ids()
    return [crawl_vendor(vid) for vid in enabled_ids]
