# price_compare.py
"""가격비교: catalog_cache(사전 크롤링된 로컬 DB)만 조회하므로 즉시 응답한다.
캐시를 최신 상태로 유지하려면 catalog_crawler.crawl_all_enabled()를 주기적으로 실행해야 한다."""
from concurrent.futures import ThreadPoolExecutor

import catalog_cache
import product_match
import vendors

CANDIDATES_PER_VENDOR = 8


def _unit_price(offer: dict) -> float:
    """1개(1타/1개입 등)당 가격. unit_qty를 모르면 비교 기준으로 표시가(1박스/1봉 가격)를 그대로 쓴다."""
    price = offer.get("price")
    if not price:
        return float("inf")
    unit_qty = offer.get("unit_qty")
    return price / unit_qty if unit_qty and unit_qty > 0 else float(price)


def _fetch_one(vendor_id: str, keyword: str) -> dict:
    meta = vendors.VENDORS[vendor_id]
    base = {
        "vendor_id": vendor_id,
        "vendor_name": meta["name"],
        "free_shipping_threshold": meta["free_shipping_threshold"],
        "candidates": [],
        "error": None,
    }

    candidates = catalog_cache.search_cached_products(vendor_id, keyword, limit=CANDIDATES_PER_VENDOR)

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

    # 도매처별 조회는 서로 독립적인 읽기 전용 DB 조회라 병렬로 돌려도 안전하다 -
    # 순차로 하면 요청 하나가 도매처 수만큼(6개) DB 왕복을 이어서 물게 되어
    # 동시 요청이 몰릴 때 지연이 그대로 누적된다(부하 테스트로 확인).
    with ThreadPoolExecutor(max_workers=len(enabled_ids)) as ex:
        results = list(ex.map(lambda vid: _fetch_one(vid, keyword), enabled_ids))
    results_by_id = {r["vendor_id"]: r for r in results}

    vendor_candidates = {r["vendor_id"]: r["candidates"] for r in results if r["candidates"]}
    raw_groups = product_match.pick_matching_groups(vendor_candidates)

    groups = []
    for g in raw_groups:
        offers = []
        for vid, cand in g["members"].items():
            offers.append({"vendor_id": vid, **_vendor_meta(vid, results_by_id), **cand})

        priced = sorted((o for o in offers if o.get("price")), key=_unit_price)
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


def filter_groups_for_store(groups: list[dict], disabled_vendors: set) -> list[dict]:
    """가맹점이 비활성화한 도매처는 가격비교/후보 목록에서 아예 안 보이게 거른다.
    걸러내고 나서 살 수 있는 도매처가 하나도 안 남는 그룹은 통째로 제거하고,
    대표 이름/가격도 남은 오퍼 기준으로 다시 뽑는다(offers는 이미 단가순
    정렬돼 있음). 텔레그램 봇/웹 compare 페이지 양쪽에서 공유해서 쓴다."""
    if not disabled_vendors:
        return groups

    filtered = []
    for g in groups:
        offers = [o for o in g["offers"] if o["vendor_id"] not in disabled_vendors]
        if not offers:
            continue
        best = offers[0]
        filtered.append({
            **g,
            "offers": offers,
            "representative_name": best["name"],
            "best_price": best.get("price"),
            "best_vendor_name": best["vendor_name"],
            "vendor_count": len(offers),
        })
    return filtered
