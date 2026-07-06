# product_match.py
"""여러 도매처 검색 후보들 중에서 '같은 상품'으로 추정되는 조합을 찾는다.

상품명이 도매처마다 100% 동일할 수 없다는 전제 하에, 글자 단위 유사도(bigram)와
용량/1타수량 같은 구조적 정보를 함께 써서 가장 매칭이 잘 되는 조합을 고른다.
"""
import re

MIN_GROUP_SCORE = 0.25

WEIGHT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(g|ml|kg|l)\b", re.IGNORECASE)
UNIT_QTY_RE = re.compile(r"(\d+)\s*개입|[xX×*]\s*(\d+)\s*개")


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def _bigrams(text: str) -> set[str]:
    t = _normalize(text)
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def keyword_containment_score(keyword: str, name: str) -> float:
    """검색어 bigram이 상품명 안에 얼마나 포함되는지(0~1). 대칭 Jaccard와 달리
    상품명에 브랜드/용량 등 부가 텍스트가 많이 붙어도 불리해지지 않아서,
    짧은 검색어로 긴 상품명을 찾을 때(오타/띄어쓰기 한두 글자 차이 포함) 더 적합하다."""
    kb = _bigrams(keyword)
    if not kb:
        return 0.0
    nb = _bigrams(name)
    return len(kb & nb) / len(kb)


def _extract_weight(text: str) -> tuple[float, str] | None:
    m = WEIGHT_RE.search(text or "")
    if not m:
        return None
    return float(m.group(1)), m.group(2).lower()


def _extract_unit_qty(text: str) -> int | None:
    m = UNIT_QTY_RE.search(text or "")
    if not m:
        return None
    return int(m.group(1) or m.group(2))


def similarity(name_a: str, name_b: str) -> float:
    """0~1 사이 유사도. bigram Jaccard + 용량/수량 일치 보너스."""
    ba, bb = _bigrams(name_a), _bigrams(name_b)
    if not ba or not bb:
        text_sim = 0.0
    else:
        inter = len(ba & bb)
        union = len(ba | bb)
        text_sim = inter / union if union else 0.0

    score = text_sim

    wa, wb = _extract_weight(name_a), _extract_weight(name_b)
    if wa and wb:
        score += 0.15 if wa == wb else -0.1

    qa, qb = _extract_unit_qty(name_a), _extract_unit_qty(name_b)
    if qa and qb:
        score += 0.15 if qa == qb else -0.1

    return max(0.0, min(1.0, score))


def pick_matching_groups(vendor_candidates: dict[str, list[dict]]) -> list[dict]:
    """
    vendor_candidates: {vendor_id: [candidate, ...]} (candidate는 최소 "name" 키를 가짐)

    검색 결과 전체를 "같은 상품(맛/용량 등)"으로 추정되는 여러 그룹으로 묶는다.
    한 그룹 내에는 도매처마다 최대 1개 후보만 들어간다.

    반환: [{"members": {vendor_id: {**candidate, "match_score": float}}}, ...]
          가격이 있는 멤버 기준 최저가 그룹부터 정렬.
          도매처 1곳에서만 발견되어 비교 대상이 없는 후보도 단독 그룹으로 포함.
    """
    pool = [
        [vid, cand]
        for vid, cands in vendor_candidates.items()
        for cand in cands
    ]
    used = [False] * len(pool)
    groups: list[dict] = []

    while True:
        best_i, best_j, best_score = -1, -1, -1.0
        for i in range(len(pool)):
            if used[i]:
                continue
            for j in range(i + 1, len(pool)):
                if used[j] or pool[i][0] == pool[j][0]:
                    continue
                s = similarity(pool[i][1].get("name", ""), pool[j][1].get("name", ""))
                if s > best_score:
                    best_score = s
                    best_i, best_j = i, j

        if best_i == -1 or best_score < MIN_GROUP_SCORE:
            break

        used[best_i] = True
        used[best_j] = True
        vid_a, cand_a = pool[best_i]
        vid_b, cand_b = pool[best_j]
        members = {
            vid_a: {**cand_a, "match_score": 1.0},
            vid_b: {**cand_b, "match_score": round(best_score, 2)},
        }
        seed_names = [cand_a.get("name", ""), cand_b.get("name", "")]

        for vid in vendor_candidates:
            if vid in members:
                continue
            best_k, best_k_score = -1, -1.0
            for k in range(len(pool)):
                if used[k] or pool[k][0] != vid:
                    continue
                avg = sum(similarity(seed, pool[k][1].get("name", "")) for seed in seed_names) / len(seed_names)
                if avg > best_k_score:
                    best_k_score = avg
                    best_k = k
            if best_k != -1 and best_k_score >= MIN_GROUP_SCORE:
                used[best_k] = True
                members[vid] = {**pool[best_k][1], "match_score": round(best_k_score, 2)}

        groups.append({"members": members})

    # 어느 그룹에도 묶이지 못한 후보들은 단독 그룹으로 추가 (비교 대상 없이 그 자체로 노출)
    for i in range(len(pool)):
        if used[i]:
            continue
        vid, cand = pool[i]
        groups.append({"members": {vid: {**cand, "match_score": 1.0}}})

    def group_min_price(g: dict) -> float:
        prices = [m["price"] for m in g["members"].values() if m.get("price")]
        return min(prices) if prices else float("inf")

    groups.sort(key=group_min_price)
    return groups
