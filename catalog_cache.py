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


def search_cached_products(vendor_id: str, keyword: str, limit: int = 30) -> list[dict]:
    """저장된 상품 중 검색어와 이름이 비슷한 것들을 유사도 순으로 뽑아온다.
    정확히 포함되는 상품이 하나라도 있으면 그것만 쓰고, 하나도 없을 때만 오타/
    띄어쓰기 한두 글자 차이(예: '꿀밤맛쫀드기' vs '꿀밤맛 쫀디기')를 허용하는
    느슨한 매칭으로 대체한다. 짧은 검색어(예: '브이콘')가 오타 허용 때문에
    무관한 상품(예: '브이톡')과 섞이는 걸 막기 위한 구조다."""
    if not keyword:
        return []

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT name, price, unit_qty, product_url, goods_no FROM product_cache WHERE vendor_id = ?",
        (vendor_id,),
    )
    rows = cur.fetchall()
    conn.close()

    exact, fuzzy = [], []
    for r in rows:
        name = r[0] or ""
        score = product_match.keyword_containment_score(keyword, name)
        if score < MIN_SEARCH_SCORE:
            continue
        item = {"name": name, "price": r[1], "unit_qty": r[2], "product_url": r[3], "goods_no": r[4]}
        (exact if score >= EXACT_SEARCH_SCORE else fuzzy).append((score, item))

    chosen = exact or fuzzy
    chosen.sort(key=lambda x: -x[0])
    return [item for _, item in chosen[:limit]]
