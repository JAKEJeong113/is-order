# polcent.py
"""폴센트(가격 추적 앱) 알림을 받아 핫딜 안내에 바로 노출한다.

관리자 앱(admin_app)의 알림 접근 서비스가 폰에 뜬 폴센트 알림의 제목/본문을
그대로 서버로 보낸다(/api/admin/polcent/ingest). 알림에는 쿠팡 링크가 없고
상품명(옵션 포함)과 "현재가"만 있어서, 쿠팡 파트너스 상품검색으로 같은
상품을 찾되 알림의 현재가와 가격이 맞는 후보만 채택한다(같은 상품번호 아래
수량 옵션이 섞여 나오는 문제를 가격으로 확정). 이름이 비슷해도 가격이 안
맞거나 제로/라이트 같은 변형 제품이면 노출하지 않고 '매칭 실패'로 기록만
남긴다 - 관리자 화면(/admin/polcent)에서 사유를 볼 수 있다.

시각은 이 프로젝트의 다른 테이블과 같이 서버 시계(UTC)의 naive ISO 문자열.
"""
import re
import threading
from datetime import datetime, timedelta

import db_conn
import product_match
import product_ranking

SEARCH_BUCKET = "search_polcent"    # 폴센트 알림 전용 예약 호출분(product_ranking.SEARCH_BUCKET_LIMITS)
EXPOSE_HOURS = 24
PRICE_MATCH_TOLERANCE = 0.01        # 알림 현재가와 후보 가격 허용 오차(±1%)
MIN_NAME_SCORE = 0.5                # 검색어 bigram이 후보명에 포함되는 비율 하한
VERIFY_COOLDOWN_SECONDS = 180
ENDED_RISE_RATIO = 1.03             # 알림가보다 3% 넘게 오르면 핫딜 종료로 본다
SAME_OPTION_MAX_DIFF = 0.25         # 같은 옵션으로 볼 수 있는 가격 차이 상한

_PRICE_RE = re.compile(r"현재가\s*([\d,]+)\s*원")
_AVG_RE = re.compile(r"평균가\s*([\d,]+)\s*원")
_TRAILING_OPTION_RE = re.compile(r",\s*\d+(?:\.\d+)?\s*(?:ml|g|kg|l)\s*[×xX*]\s*\d+\s*개\s*$", re.IGNORECASE)
_SIZE_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:ml|g|kg|l)(?![a-z])|\d+\s*개(?:입)?|[×xX*]", re.IGNORECASE)
RATE_LIMIT_REASON_PREFIX = "쿠팡 검색 한도 초과"
RETRY_WINDOW_MINUTES = 30           # 한도 초과로 실패한 알림을 다시 시도하는 기간
RETRY_MAX = 15
NOTIFY_DEDUPE_HOURS = 12            # 같은 상품 텔레그램 안내는 이 시간 안에 한 번만
IGNORED_KEEP_DAYS = 3               # 해석 못 한 알림(광고 등) 기록 보관 기간
_ACTIVE = "status = 'exposed' AND deleted = 0 AND ended = 0"

_verify_locks: dict[int, threading.Lock] = {}
_verify_locks_guard = threading.Lock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _age_seconds(iso: str | None) -> float | None:
    if not iso:
        return None
    return max(0.0, (datetime.now() - datetime.fromisoformat(iso)).total_seconds())


def _to_kst(iso: str | None) -> str | None:
    if not iso:
        return None
    return (datetime.fromisoformat(iso) + timedelta(hours=9)).strftime("%m.%d %H:%M")


def init_tables() -> None:
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS polcent_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        received_at TEXT NOT NULL,
        source TEXT,
        raw_title TEXT,
        raw_text TEXT,
        kind TEXT,
        parsed_name TEXT,
        parsed_price INTEGER,
        avg_price INTEGER,
        pack_qty INTEGER,
        status TEXT NOT NULL,
        reason TEXT,
        product_id TEXT,
        matched_name TEXT,
        image_url TEXT,
        partners_link TEXT,
        current_price INTEGER,
        expires_at TEXT,
        last_verified_at TEXT,
        ended INTEGER NOT NULL DEFAULT 0,
        deleted INTEGER NOT NULL DEFAULT 0,
        click_count INTEGER NOT NULL DEFAULT 0
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS polcent_price_points (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alert_id INTEGER NOT NULL,
        price INTEGER NOT NULL,
        recorded_at TEXT NOT NULL
    )
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_polcent_points_alert ON polcent_price_points (alert_id)")
    cur.execute("ALTER TABLE polcent_alerts ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0")
    cur.execute("ALTER TABLE polcent_alerts ADD COLUMN IF NOT EXISTS notified_at TEXT")
    conn.commit()
    conn.close()


def parse_alert(title: str, text: str) -> dict:
    """알림 제목/본문에서 상품명·현재가·평균가·종류를 뽑는다. 종류는 가격 하락
    (drop) / 재입고(restock) / 해석 불가(unknown)."""
    title = (title or "").strip()
    text = (text or "").strip()
    name = _TRAILING_OPTION_RE.sub("", title).strip()
    price_m = _PRICE_RE.search(text) or _PRICE_RE.search(title)
    avg_m = _AVG_RE.search(text)
    price = int(price_m.group(1).replace(",", "")) if price_m else None
    avg = int(avg_m.group(1).replace(",", "")) if avg_m else None

    if "재입고" in text or "재입고" in title:
        kind = "restock"
    elif price and (avg or "→" in text):
        kind = "drop"
    else:
        kind = "unknown"

    return {
        "kind": kind,
        "name": name,
        "price": price,
        "avg_price": avg,
        "pack_qty": product_ranking._extract_coupang_pack_qty(name),
        "keyword": re.sub(r"\s+", " ", name.replace(",", " ")).strip(),
    }


def _core_keyword(keyword: str) -> str:
    """용량/수량 토큰을 뺀 상품명(예: '스프라이트 캔 350ml 24개' -> '스프라이트 캔').
    쿠팡 상품명에는 용량/수량이 안 붙어 있는 경우가 많아 이름 비교에는 이쪽을 쓴다."""
    return re.sub(r"\s+", " ", _SIZE_TOKEN_RE.sub(" ", keyword)).strip()


def _find_match(parsed: dict) -> tuple[dict | None, str]:
    """알림 정보에 맞는 쿠팡 후보 하나와(없으면 None) 사유 문자열을 돌려준다.
    전체 이름으로 검색해서 못 찾으면 용량/수량을 뺀 이름으로 한 번 더 찾는다."""
    price = parsed["price"]
    core = _core_keyword(parsed["keyword"]) or parsed["keyword"]
    queries = [parsed["keyword"]] + ([core] if core != parsed["keyword"] else [])

    seen_ids = set()
    near = []
    any_result = False
    for query in queries:
        cands = product_ranking._fetch_coupang_products(query, limit=10, bucket=SEARCH_BUCKET)
        if not cands:
            continue
        any_result = True
        good = []
        for c in cands:
            c_price = c.get("price")
            if not c_price:
                continue
            c_name = c.get("product_name") or ""
            diff = abs(c_price - price) / price
            key = (c.get("product_id"), c_price)
            if key not in seen_ids:
                seen_ids.add(key)
                near.append((diff, c))
            if diff > PRICE_MATCH_TOLERANCE:
                continue
            if product_ranking._variant_mismatch([parsed["name"]], c_name):
                continue
            c_qty = product_ranking._extract_coupang_pack_qty(c_name)
            if c_qty and parsed["pack_qty"] and c_qty != parsed["pack_qty"]:
                continue
            score = product_match.keyword_containment_score(core, c_name)
            if score < MIN_NAME_SCORE:
                continue
            good.append((diff, -score, c))
        if good:
            good.sort(key=lambda t: (t[0], t[1]))
            return good[0][2], "가격·이름 일치"

    if not any_result:
        return None, "쿠팡 검색 결과 없음(로켓 상품만 대상)"
    near.sort(key=lambda t: t[0])
    hint = ", ".join(
        f"{c.get('price'):,}원 '{(c.get('product_name') or '')[:24]}'" for _, c in near[:3]
    )
    return None, f"알림가 {price:,}원과 맞는 후보 없음 (가까운 후보: {hint or '없음'})"


def _insert_alert(**f) -> int:
    cols = list(f.keys())
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute(
        f"INSERT INTO polcent_alerts ({', '.join(cols)}) VALUES ({', '.join(['?'] * len(cols))}) RETURNING id",
        tuple(f[c] for c in cols),
    )
    alert_id = cur.fetchone()[0]
    conn.commit()
    conn.close()
    return alert_id


def _add_price_point(alert_id: int, price: int) -> None:
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO polcent_price_points (alert_id, price, recorded_at) VALUES (?, ?, ?)",
        (alert_id, price, _now()),
    )
    conn.commit()
    conn.close()


def _notify_unlinked(alert_id: int | None, parsed: dict, reason: str) -> None:
    """폴센트가 알림을 줬는데 내 쿠팡 파트너스 링크를 만들지 못한 상품을 대표님
    텔레그램으로 알린다. 폴센트가 같은 상품을 계속 다시 알려도 스팸이 되지 않게
    같은 상품명은 NOTIFY_DEDUPE_HOURS 안에 한 번만 보낸다."""
    name = parsed.get("name") or ""
    if alert_id:
        since = (datetime.now() - timedelta(hours=NOTIFY_DEDUPE_HOURS)).isoformat(timespec="seconds")
        conn = db_conn.get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM polcent_alerts WHERE parsed_name = ? AND notified_at >= ? LIMIT 1", (name, since),
        )
        already = cur.fetchone() is not None
        conn.close()
        if already:
            return

    kind_text = "🚚 재입고" if parsed.get("kind") == "restock" else "📉 가격 하락"
    price_text = f"{parsed['price']:,}원" if parsed.get("price") else "가격 미상"
    if parsed.get("avg_price"):
        price_text += f" (평균가 {parsed['avg_price']:,}원)"
    lines = [
        "🔔 폴센트 알림 - 내 쿠팡 파트너스 링크를 만들지 못했어요",
        "",
        f"{kind_text} · {name}",
        f"알림가: {price_text}",
        f"사유: {reason}",
        "",
        "쿠팡에서 직접 확인해 보세요. 관리자 앱 > 폴센트 감지에서 '다시 처리'도 할 수 있어요.",
    ]
    import telegram_bot  # 순환 import를 피하려고 필요할 때만 불러온다

    # 실제로 보내졌을 때만 "안내함"으로 기록한다 - 전송이 실패했는데 기록해버리면
    # 같은 상품의 다음 알림까지 12시간 동안 막힌다.
    sent = bool(telegram_bot.ADMIN_CHAT_ID) and telegram_bot.send_message(telegram_bot.ADMIN_CHAT_ID, "\n".join(lines))
    if sent and alert_id:
        conn = db_conn.get_conn()
        cur = conn.cursor()
        cur.execute("UPDATE polcent_alerts SET notified_at = ? WHERE id = ?", (_now(), alert_id))
        conn.commit()
        conn.close()


def _maybe_notify(result: dict, source: str, dry_run: bool) -> None:
    """링크를 못 만든 최종 결과(매칭 실패/검색 오류)만 안내한다. 쿠팡 검색 한도 초과는
    자동 재시도가 처리하므로 보내지 않고, 대표님이 직접 누른 다시 처리/테스트도 보내지 않는다."""
    if dry_run or source not in ("notification", "retry"):
        return
    status, reason = result.get("status"), result.get("reason") or ""
    if status == "unmatched" or (status == "error" and not reason.startswith(RATE_LIMIT_REASON_PREFIX)):
        try:
            _notify_unlinked(result.get("alert_id"), result["parsed"], reason)
        except Exception as e:
            print("[POLCENT] 텔레그램 안내 실패:", e)


def process_alert(title: str, text: str, source: str = "notification", dry_run: bool = False) -> dict:
    """알림 하나를 해석하고 쿠팡에서 같은 상품을 찾아 바로 노출한다. dry_run이면
    DB에 아무것도 남기지 않고 해석/매칭 결과만 돌려준다(관리자 화면 테스트용)."""
    parsed = parse_alert(title, text)
    now = _now()
    base = {
        "received_at": now, "source": source, "raw_title": title, "raw_text": text,
        "kind": parsed["kind"], "parsed_name": parsed["name"], "parsed_price": parsed["price"],
        "avg_price": parsed["avg_price"], "pack_qty": parsed["pack_qty"],
    }

    def finish(status: str, reason: str, matched: dict | None = None, db: dict | None = None) -> dict:
        result = {"status": status, "reason": reason, "parsed": parsed}
        if matched:
            result["matched"] = matched
        if not dry_run:
            result["alert_id"] = _insert_alert(**base, status=status, reason=reason, **(db or {}))
        return result

    if parsed["kind"] == "unknown" or not parsed["price"] or not parsed["name"]:
        return finish("ignored", "알림 형식을 해석하지 못함(현재가/상품명 없음)")

    try:
        match, reason = _find_match(parsed)
    except product_ranking.CoupangRateLimitError as e:
        return finish("error", f"{RATE_LIMIT_REASON_PREFIX}: {e}")
    except Exception as e:
        result = finish("error", f"쿠팡 검색 실패: {e}")
        _maybe_notify(result, source, dry_run)
        return result

    if not match:
        result = finish("unmatched", reason)
        _maybe_notify(result, source, dry_run)
        return result

    product_id = str(match["product_id"]) if match.get("product_id") is not None else None
    matched = {"product_id": product_id, "matched_name": match.get("product_name"), "matched_price": match["price"]}
    expires_at = (datetime.now() + timedelta(hours=EXPOSE_HOURS)).isoformat(timespec="seconds")

    # 같은 상품이 이미 노출 중이면(폴센트 재알림) 새 카드를 만들지 않고 기간/가격만 갱신한다.
    existing_id = None
    if product_id and not dry_run:
        conn = db_conn.get_conn()
        cur = conn.cursor()
        cur.execute(
            f"SELECT id FROM polcent_alerts WHERE product_id = ? AND {_ACTIVE} ORDER BY id DESC LIMIT 1",
            (product_id,),
        )
        row = cur.fetchone()
        conn.close()
        if row:
            existing_id = row[0]

    if existing_id:
        conn = db_conn.get_conn()
        cur = conn.cursor()
        cur.execute(
            "UPDATE polcent_alerts SET current_price = ?, expires_at = ?, last_verified_at = ?, kind = ? WHERE id = ?",
            (match["price"], expires_at, now, parsed["kind"], existing_id),
        )
        conn.commit()
        conn.close()
        _add_price_point(existing_id, match["price"])
        return finish("duplicate", f"이미 노출 중인 상품(#{existing_id}) - 노출 기간만 연장", matched=matched)

    db_fields = {
        "product_id": product_id, "matched_name": match.get("product_name"),
        "image_url": match.get("image_url"), "partners_link": match.get("reference_url"),
        "current_price": match["price"], "expires_at": expires_at, "last_verified_at": now,
    }
    result = finish("exposed", f"노출됨({EXPOSE_HOURS}시간)", matched=matched, db=db_fields)
    if not dry_run:
        _add_price_point(result["alert_id"], match["price"])
    return result


def list_hotdeals(limit: int = 50) -> list[dict]:
    """핫딜 안내 화면용 - 노출 중인(만료/종료/삭제 안 된) 폴센트 감지 상품."""
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT id, parsed_name, image_url, partners_link, current_price, received_at, pack_qty,
               last_verified_at, kind, avg_price, parsed_price
        FROM polcent_alerts
        WHERE {_ACTIVE} AND expires_at > ?
        ORDER BY received_at DESC LIMIT ?
        """,
        (_now(), limit),
    )
    rows = cur.fetchall()
    conn.close()
    items = []
    for r in rows:
        price, pack_qty = r[4], r[6]
        items.append({
            "item_key": f"polcent_{r[0]}", "item_name": r[1], "image_url": r[2], "partners_link": r[3],
            "price": price, "detected_at": r[5], "product_type": "polcent",
            "pack_qty": pack_qty, "unit_cost": round(price / pack_qty) if pack_qty and pack_qty > 1 else None,
            "price_checked_at": r[7], "checked_age_seconds": _age_seconds(r[7]),
            "kind": r[8], "avg_price": r[9], "alert_price": r[10],
        })
    return items


def _alert_id_from_key(item_key: str) -> int | None:
    m = re.fullmatch(r"polcent_(\d+)", item_key or "")
    return int(m.group(1)) if m else None


def get_price_points(item_key: str) -> list[dict]:
    alert_id = _alert_id_from_key(item_key)
    if alert_id is None:
        return []
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT price, recorded_at FROM polcent_price_points WHERE alert_id = ? ORDER BY recorded_at ASC",
        (alert_id,),
    )
    rows = cur.fetchall()
    conn.close()
    return [{"price": r[0], "recorded_at": r[1]} for r in rows]


def record_click(item_key: str) -> bool:
    alert_id = _alert_id_from_key(item_key)
    if alert_id is None:
        return False
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE polcent_alerts SET click_count = click_count + 1 WHERE id = ?", (alert_id,))
    conn.commit()
    conn.close()
    return True


def verify_item(item_key: str) -> dict:
    """클릭 시점 재확인 - 같은 상품번호 후보 중 저장된 가격에 가장 가까운(같은
    옵션으로 볼 수 있는) 가격으로 갱신한다. 알림가보다 3% 넘게 오르면 핫딜 종료."""
    alert_id = _alert_id_from_key(item_key)
    if alert_id is None:
        return {"ok": False, "error": "not_found"}
    with _verify_locks_guard:
        lock = _verify_locks.setdefault(alert_id, threading.Lock())

    rate_limited = False
    with lock:
        row = _load(alert_id)
        if not row:
            return {"ok": False, "error": "not_found"}
        age = _age_seconds(row["last_verified_at"])
        if row["status"] == "exposed" and not row["ended"] and (age is None or age >= VERIFY_COOLDOWN_SECONDS):
            try:
                cands = product_ranking._fetch_coupang_products(
                    parse_alert(row["raw_title"], row["raw_text"])["keyword"], limit=10, bucket=SEARCH_BUCKET,
                )
            except product_ranking.CoupangRateLimitError:
                cands = None
                rate_limited = True
            except Exception:
                cands = None
            if cands is not None:
                same = [c for c in cands if c.get("price") and str(c.get("product_id")) == row["product_id"]]
                if same:
                    ref = row["current_price"]
                    best = min(same, key=lambda c: abs(c["price"] - ref))
                    if abs(best["price"] - ref) / ref <= SAME_OPTION_MAX_DIFF:
                        _apply_verified(alert_id, best["price"], row["parsed_price"])
            row = _load(alert_id)

    live = bool(row and row["status"] == "exposed" and not row["ended"] and not row["deleted"]
                and row["expires_at"] > _now())
    pack_qty = row["pack_qty"] if row else None
    price = row["current_price"] if row else None
    return {
        "ok": True, "is_hotdeal": live, "price": price,
        "checked_at": row["last_verified_at"] if row else None,
        "checked_age_seconds": _age_seconds(row["last_verified_at"]) if row else None,
        "rate_limited": rate_limited, "pack_qty": pack_qty,
        "unit_cost": round(price / pack_qty) if price and pack_qty and pack_qty > 1 else None,
    }


def _apply_verified(alert_id: int, new_price: int, alert_price: int) -> None:
    ended = 1 if new_price > alert_price * ENDED_RISE_RATIO else 0
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE polcent_alerts SET current_price = ?, last_verified_at = ?, ended = ? WHERE id = ?",
        (new_price, _now(), ended, alert_id),
    )
    conn.commit()
    conn.close()
    _add_price_point(alert_id, new_price)


_COLUMNS = (
    "id, raw_title, raw_text, status, product_id, current_price, parsed_price, pack_qty, "
    "last_verified_at, expires_at, ended, deleted"
)


def _load(alert_id: int) -> dict | None:
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute(f"SELECT {_COLUMNS} FROM polcent_alerts WHERE id = ?", (alert_id,))
    r = cur.fetchone()
    conn.close()
    if not r:
        return None
    return dict(zip([c.strip() for c in _COLUMNS.split(",")], r))


def list_alerts(limit: int = 60, include_ignored: bool = False) -> dict:
    """관리자 화면용 최근 알림 목록 + 마지막 수신 시각. 폴센트의 광고/추천 알림처럼
    해석 못 해 무시된 건 기본으로 숨긴다(include_ignored로 볼 수 있음)."""
    conn = db_conn.get_conn()
    cur = conn.cursor()
    where = "" if include_ignored else "WHERE status <> 'ignored'"
    cur.execute(
        f"""
        SELECT id, received_at, source, raw_title, raw_text, kind, parsed_name, parsed_price, avg_price,
               status, reason, matched_name, current_price, expires_at, ended, deleted, click_count, partners_link
        FROM polcent_alerts {where} ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    )
    rows = cur.fetchall()
    cur.execute("SELECT MAX(received_at) FROM polcent_alerts WHERE source = 'notification'")
    last_notification = cur.fetchone()[0]
    conn.close()
    now = _now()
    items = []
    for r in rows:
        live = r[9] == "exposed" and not r[14] and not r[15] and r[13] and r[13] > now
        items.append({
            "id": r[0], "received_kst": _to_kst(r[1]), "source": r[2], "title": r[3], "text": r[4],
            "kind": r[5], "name": r[6], "price": r[7], "avg_price": r[8], "status": r[9], "reason": r[10],
            "matched_name": r[11], "current_price": r[12], "expires_kst": _to_kst(r[13]),
            "live": bool(live), "ended": bool(r[14]), "deleted": bool(r[15]), "clicks": r[16],
            "link": r[17],
        })
    return {
        "items": items,
        "last_notification_age_seconds": _age_seconds(last_notification),
    }


def expire_alert(alert_id: int) -> bool:
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE polcent_alerts SET deleted = 1 WHERE id = ?", (alert_id,))
    ok = cur.rowcount > 0
    conn.commit()
    conn.close()
    return ok


def reprocess_alert(alert_id: int) -> dict:
    """매칭 실패/오류 건을 다시 처리한다(쿠팡 한도 초과였거나 가격이 그 사이 바뀐 경우)."""
    row = _load(alert_id)
    if not row:
        return {"ok": False, "error": "not_found"}
    expire_alert(alert_id)
    result = process_alert(row["raw_title"], row["raw_text"], source="reprocess")
    return {"ok": True, **{k: v for k, v in result.items() if k != "parsed"}}


def retry_pending_errors() -> dict:
    """쿠팡 검색 한도 초과로 실패한 최근 알림을 다시 처리한다(1분마다 호출). 한도는
    분 단위로 풀리므로 보통 다음 시도에서 성공한다. 같은 알림을 RETRY_MAX번까지,
    RETRY_WINDOW_MINUTES 안에서만 시도하고, 다시 실패하면 기록을 늘리지 않고
    시도 횟수만 올린다. 오래된 '무시' 기록도 여기서 같이 정리한다."""
    cutoff = (datetime.now() - timedelta(minutes=RETRY_WINDOW_MINUTES)).isoformat(timespec="seconds")
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM polcent_alerts WHERE status = 'ignored' AND received_at < ?",
        ((datetime.now() - timedelta(days=IGNORED_KEEP_DAYS)).isoformat(timespec="seconds"),),
    )
    cur.execute(
        """
        SELECT id, raw_title, raw_text, retry_count FROM polcent_alerts
        WHERE status = 'error' AND deleted = 0 AND reason LIKE ? AND received_at >= ? AND retry_count < ?
        ORDER BY id ASC LIMIT 5
        """,
        (RATE_LIMIT_REASON_PREFIX + "%", cutoff, RETRY_MAX),
    )
    rows = cur.fetchall()
    conn.commit()
    conn.close()

    retried = succeeded = 0
    for alert_id, title, text, retry_count in rows:
        retried += 1
        result = process_alert(title, text, source="retry")
        new_id = result.get("alert_id")
        conn = db_conn.get_conn()
        cur = conn.cursor()
        if result["status"] == "error" and result["reason"].startswith(RATE_LIMIT_REASON_PREFIX):
            # 또 한도 초과 - 새로 생긴 오류 기록은 지우고 원래 건의 시도 횟수만 올린다.
            if new_id:
                cur.execute("DELETE FROM polcent_alerts WHERE id = ?", (new_id,))
            cur.execute("UPDATE polcent_alerts SET retry_count = retry_count + 1 WHERE id = ?", (alert_id,))
            if retry_count + 1 >= RETRY_MAX:
                try:
                    _notify_unlinked(
                        alert_id, parse_alert(title, text),
                        f"쿠팡 검색 한도 때문에 자동으로 {RETRY_MAX}번 다시 시도했지만 처리하지 못했어요",
                    )
                except Exception as e:
                    print("[POLCENT] 텔레그램 안내 실패:", e)
        else:
            # 노출/중복/매칭 실패 등 최종 결과가 나왔다 - 원래 오류 건은 재처리됨으로 닫는다.
            succeeded += 1
            cur.execute(
                "UPDATE polcent_alerts SET deleted = 1, reason = ? WHERE id = ?",
                (f"재시도로 처리됨(#{new_id})", alert_id),
            )
        conn.commit()
        conn.close()
    return {"retried": retried, "resolved": succeeded}


def refresh_live_items(limit: int = 6) -> dict:
    """노출 중인 항목의 가격을 주기적으로 다시 확인한다(10분마다 호출). 바코드 검색기
    앱은 쿠팡 API를 직접 못 불러서 클릭 시점 재확인이 없으므로, 서버가 미리 가격이
    오른 항목을 내려둔다. verify_item이 쿨다운/한도 처리를 이미 갖고 있다."""
    conn = db_conn.get_conn()
    cur = conn.cursor()
    cur.execute(
        f"""
        SELECT id FROM polcent_alerts
        WHERE {_ACTIVE} AND expires_at > ?
        ORDER BY last_verified_at ASC NULLS FIRST LIMIT ?
        """,
        (_now(), limit),
    )
    ids = [r[0] for r in cur.fetchall()]
    conn.close()
    checked = 0
    for alert_id in ids:
        result = verify_item(f"polcent_{alert_id}")
        if result.get("rate_limited"):
            break
        checked += 1
    return {"checked": checked}
