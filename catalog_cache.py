# catalog_cache.py
"""도매처 전체상품을 미리 크롤링해서 저장해두는 로컬 캐시.
가격비교 검색은 이 캐시만 조회하므로 즉시 응답한다."""
import os
import sqlite3
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR))
DB_PATH = DATA_DIR / "inventory.db"


def get_conn():
    return sqlite3.connect(DB_PATH)


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


def search_cached_products(vendor_id: str, keyword_tokens: list[str], limit: int = 30) -> list[dict]:
    """저장된 상품 중 키워드 토큰이 이름에 포함된 것만 뽑아온다 (최종 유사도 랭킹은 호출측에서)."""
    if not keyword_tokens:
        return []

    conn = get_conn()
    cur = conn.cursor()

    where = " OR ".join(["name LIKE ?"] * len(keyword_tokens))
    params = [f"%{tok}%" for tok in keyword_tokens]
    cur.execute(
        f"SELECT name, price, unit_qty, product_url, goods_no FROM product_cache "
        f"WHERE vendor_id = ? AND ({where}) LIMIT ?",
        (vendor_id, *params, limit * 5),
    )
    rows = cur.fetchall()
    conn.close()

    return [
        {"name": r[0], "price": r[1], "unit_qty": r[2], "product_url": r[3], "goods_no": r[4]}
        for r in rows
    ]
