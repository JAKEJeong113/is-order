# catalog_cache.py
"""도매처 전체상품을 미리 크롤링해서 저장해두는 로컬 캐시.
가격비교 검색은 이 캐시만 조회하므로 즉시 응답한다."""
from datetime import datetime

import product_match

import db_conn

MIN_SEARCH_SCORE = 0.5
EXACT_SEARCH_SCORE = 0.999


def get_conn():
    return db_conn.get_conn()


def init_catalog_table():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS product_cache (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        vendor_id TEXT,
        name TEXT,
        price INTEGER,
        unit_qty INTEGER,
        product_url TEXT,
        goods_no TEXT,
        scraped_at TEXT
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_product_cache_vendor ON product_cache (vendor_id)")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS catalog_refresh_log (
        vendor_id TEXT PRIMARY KEY,
        product_count INTEGER,
        refreshed_at TEXT,
        ok INTEGER,
        error TEXT
    )
    """)
    conn.commit()
    conn.close()


def replace_vendor_catalog(vendor_id: str, products: list[dict]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("DELETE FROM product_cache WHERE vendor_id = ?", (vendor_id,))
    cur.executemany(
        """
        INSERT INTO product_cache (vendor_id, name, price, unit_qty, product_url, goods_no, scraped_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                vendor_id,
                p.get("name"),
                p.get("price"),
                p.get("unit_qty"),
                p.get("product_url"),
                p.get("goods_no"),
                now,
            )
            for p in products
        ],
    )

    cur.execute("""
    INSERT INTO catalog_refresh_log (vendor_id, product_count, refreshed_at, ok, error)
    VALUES (?, ?, ?, 1, NULL)
    ON CONFLICT(vendor_id) DO UPDATE SET
        product_count = excluded.product_count,
        refreshed_at = excluded.refreshed_at,
        ok = 1,
        error = NULL
    """, (vendor_id, len(products), now))

    conn.commit()
    conn.close()


def record_refresh_error(vendor_id: str, error: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO catalog_refresh_log (vendor_id, product_count, refreshed_at, ok, error)
    VALUES (?, 0, ?, 0, ?)
    ON CONFLICT(vendor_id) DO UPDATE SET
        refreshed_at = excluded.refreshed_at,
        ok = 0,
        error = excluded.error
    """, (vendor_id, now, error))
    conn.commit()
    conn.close()


def get_refresh_status() -> list[dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT vendor_id, product_count, refreshed_at, ok, error FROM catalog_refresh_log")
    rows = cur.fetchall()
    conn.close()
    return [
        {
            "vendor_id": r[0],
            "product_count": r[1],
            "refreshed_at": r[2],
            "ok": bool(r[3]),
            "error": r[4],
        }
        for r in rows
    ]


def _dedupe_by_name(items: list[dict]) -> list[dict]:
    """같은 도매처 사이트 안에 이름/가격이 완전히 똑같은 상품이 서로 다른
    goods_no로 중복 등록돼 있는 경우가 있다(사이트 자체의 중복 리스팅 - 크롤러의
    goods_no 기준 중복 제거로는 못 잡음, goods_no 자체가 다르므로). 그대로 두면
    가격비교/장바구니 담기 선택지에 똑같은 상품이 여러 개인 것처럼 떠서 사용자가
    아무거나 골라도 되는 건지 헷갈린다. 정규화된 이름 기준으로 하나만 남긴다
    (가격이 있는 쪽을 우선)."""
    best: dict[str, dict] = {}
    order: list[str] = []
    for item in items:
        key = product_match._normalize(item.get("name") or "")
        if key not in best:
            best[key] = item
            order.append(key)
        elif not best[key].get("price") and item.get("price"):
            best[key] = item
    return [best[k] for k in order]


def search_cached_products(vendor_id: str, keyword: str, limit: int = 30) -> list[dict]:
    """저장된 상품 중 검색어와 이름이 비슷한 것들을 유사도 순으로 뽑아온다.
    정확히 포함되는 상품이 하나라도 있으면 그것만 쓰고, 하나도 없을 때만 오타/
    띄어쓰기 한두 글자 차이(예: '꿀밤맛쫀드기' vs '꿀밤맛 쫀디기')를 허용하는
    느슨한 매칭으로 대체한다. 짧은 검색어(예: '브이콘')가 오타 허용 때문에
    무관한 상품(예: '브이톡')과 섞이는 걸 막기 위한 구조다.

    도매처 하나당 캐시 상품이 수천 개라 매번 전부 가져와 Python으로 bigram
    스코어를 계산하면 검색 한 번에 초 단위가 걸린다(가격비교/텔레그램 발주
    분류 양쪽의 공통 경로라 부하 테스트에서 병목으로 확인됨). "정확히 포함"
    판정만 SQL의 LIKE로 먼저 값싸게 걸러내고, 그걸로 하나도 못 찾았을 때만
    기존처럼 전체를 가져와 오타 허용 매칭을 한다 - 정규화된 키워드가 이름에
    리터럴로 포함되면 bigram containment는 항상 1.0이라 기존 "정확" 판정과
    사실상 동일 집합이다."""
    if not keyword:
        return []

    normalized = product_match._normalize(keyword)
    escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        SELECT name, price, unit_qty, product_url, goods_no FROM product_cache
        WHERE vendor_id = ? AND LOWER(regexp_replace(name, '\\s+', '', 'g')) LIKE ? ESCAPE '\\'
        LIMIT ?
    """, (vendor_id, f"%{escaped}%", limit))
    exact_rows = cur.fetchall()

    if exact_rows:
        conn.close()
        return _dedupe_by_name([
            {"name": r[0] or "", "price": r[1], "unit_qty": r[2], "product_url": r[3], "goods_no": r[4]}
            for r in exact_rows
        ])

    cur.execute(
        "SELECT name, price, unit_qty, product_url, goods_no FROM product_cache WHERE vendor_id = ?",
        (vendor_id,),
    )
    rows = cur.fetchall()
    conn.close()

    fuzzy = []
    for r in rows:
        name = r[0] or ""
        score = product_match.keyword_containment_score(keyword, name)
        if score < MIN_SEARCH_SCORE:
            continue
        item = {"name": name, "price": r[1], "unit_qty": r[2], "product_url": r[3], "goods_no": r[4]}
        fuzzy.append((score, item))

    fuzzy.sort(key=lambda x: -x[0])
    return _dedupe_by_name([item for _, item in fuzzy])[:limit]
