# price_compare.py
"""가격비교: catalog_cache(사전 크롤링된 로컬 DB)만 조회하므로 즉시 응답한다.
캐시를 최신 상태로 유지하려면 catalog_crawler.crawl_all_enabled()를 주기적으로 실행해야 한다."""
import catalog_cache
import product_match
import vendors

CANDIDATES_PER_VENDOR = 8


def _fetch_one(vendor_id: str, keyword: str) -> dict:
    meta = vendors.VENDORS[vendor_id]
    base = {
        "vendor_id": vendor_id,
        "vendor_name": meta["name"],
        "free_shipping_threshold": meta["free_shipping_threshold"],
        "candidates": [],
        "error": None,
    }

    tokens = [t for t in keyword.split() if t] or [keyword]
    candidates = catalog_cache.search_cached_products(vendor_id, tokens, limit=CANDIDATES_PER_VENDOR)

    if not candidates:
        base["error"] = "일치하는 상품 없음 (캐시가 오래되었으면 새로고침 필요)"
        return base

    base["candidates"] = candidates
    return base


def _vendor_meta(vid: str, results_by_id: dict[str, dict]) -> dict:
    r = results_by_id.get(vid)
    if r:
        return {"vendor_name": r["vendor_name"], "free_shipping_threshold": r["free_shipping_threshold"]}
    meta = vendors.VENDORS[vid]
    return {"vendor_name": meta["name"], "free_shipping_threshold": meta["free_shipping_threshold"]}


def compare(keyword: str) -> dict:
    enabled_ids = vendors.get_enabled_vendor_ids()
    if not enabled_ids:
        return {"vendors": [], "groups": []}

    results = [_fetch_one(vid, keyword) for vid in enabled_ids]
    results_by_id = {r["vendor_id"]: r for r in results}

    vendor_candidates = {r["vendor_id"]: r["candidates"] for r in results if r["candidates"]}
    raw_groups = product_match.pick_matching_groups(vendor_candidates)

    groups = []
    for g in raw_groups:
        offers = []
        for vid, cand in g["members"].items():
            offers.append({"vendor_id": vid, **_vendor_meta(vid, results_by_id), **cand})

        priced = sorted((o for o in offers if o.get("price")), key=lambda o: o["price"])
        unpriced = [o for o in offers if not o.get("price")]
        for i, o in enumerate(priced):
            o["is_cheapest"] = (i == 0)
        for o in unpriced:
            o["is_cheapest"] = False

        offers = priced + unpriced
        best = offers[0]
        groups.append({
            "representative_name": best["name"],
            "best_price": best.get("price"),
            "best_vendor_name": best["vendor_name"],
            "vendor_count": len(offers),
            "offers": offers,
        })

    return {
        "vendors": results,
        "groups": groups,
    }
