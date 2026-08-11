"""
네이버 검색광고 API(연관키워드 조회)로 시드 키워드 풀의 월간 검색량을 조회해
가장 인기 있는(=검색량 많은) 키워드 중 아직 안 쓴 것 상위 N개를 뽑는다.

사용법:
    python get_keywords.py            # 오늘의 키워드 3개를 골라 출력 + used_keywords.json에 기록
    python get_keywords.py --dry-run  # 기록하지 않고 순위만 출력 (테스트용)
    python get_keywords.py -n 5       # 개수 조절

필요 환경변수 (.env, 프로젝트 루트):
    NAVER_AD_API_KEY
    NAVER_AD_SECRET_KEY
    NAVER_AD_CUSTOMER_ID
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR.parent / ".env")

BASE_URL = "https://api.naver.com"
URI = "/keywordstool"
METHOD = "GET"

API_KEY = os.getenv("NAVER_AD_API_KEY")
SECRET_KEY = os.getenv("NAVER_AD_SECRET_KEY")
CUSTOMER_ID = os.getenv("NAVER_AD_CUSTOMER_ID")

SEED_FILE = BASE_DIR / "keywords_seed.json"
USED_FILE = BASE_DIR / "used_keywords.json"


def _signature(timestamp: str, method: str, uri: str, secret_key: str) -> str:
    message = f"{timestamp}.{method}.{uri}"
    digest = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _headers() -> dict:
    if not (API_KEY and SECRET_KEY and CUSTOMER_ID):
        raise RuntimeError(
            "NAVER_AD_API_KEY / NAVER_AD_SECRET_KEY / NAVER_AD_CUSTOMER_ID 가 .env에 설정되어 있지 않습니다."
        )
    timestamp = str(round(time.time() * 1000))
    return {
        "Content-Type": "application/json; charset=UTF-8",
        "X-Timestamp": timestamp,
        "X-API-KEY": API_KEY,
        "X-Customer": str(CUSTOMER_ID),
        "X-Signature": _signature(timestamp, METHOD, URI, SECRET_KEY),
    }


def _to_int(value) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).strip()
    if s.startswith("<"):
        return 5  # "< 10" 같은 표기는 최소치로 취급
    digits = "".join(ch for ch in s if ch.isdigit())
    return int(digits) if digits else 0


def fetch_volumes(hint_keywords: list[str]) -> list[dict]:
    """hint_keywords(최대 5개)를 넣어 연관키워드+검색량을 받아온다."""
    params = {
        "hintKeywords": ",".join(hint_keywords),
        "showDetail": "1",
    }
    resp = requests.get(BASE_URL + URI, params=params, headers=_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json().get("keywordList", [])


def load_seed_pool() -> list[str]:
    data = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    keywords = []
    for category in data.values():
        keywords.extend(category)
    # 중복 제거, 순서 유지
    seen = set()
    unique = []
    for kw in keywords:
        if kw not in seen:
            seen.add(kw)
            unique.append(kw)
    return unique


def load_used() -> dict:
    if USED_FILE.exists():
        return json.loads(USED_FILE.read_text(encoding="utf-8"))
    return {}


def save_used(used: dict, new_keywords: list[str], today: str):
    used[today] = new_keywords
    USED_FILE.write_text(json.dumps(used, ensure_ascii=False, indent=2), encoding="utf-8")


def pick_top_keywords(n: int) -> list[dict]:
    seed_pool = load_seed_pool()
    used = load_used()
    already_used = {kw for day_list in used.values() for kw in day_list}

    ranked = []
    # API가 hintKeywords 최대 5개 제한이라 5개씩 묶어서 호출
    for i in range(0, len(seed_pool), 5):
        batch = seed_pool[i : i + 5]
        try:
            results = fetch_volumes(batch)
        except Exception as exc:
            print(f"[경고] {batch} 조회 실패: {exc}")
            continue
        for item in results:
            keyword = item.get("relKeyword")
            if not keyword or keyword in already_used:
                continue
            volume = _to_int(item.get("monthlyPcQcCnt", 0)) + _to_int(item.get("monthlyMobileQcCnt", 0))
            ranked.append({"keyword": keyword, "volume": volume})

    # relKeyword로 연관어까지 딸려오므로 중복 제거(먼저 나온 것 우선)
    seen = set()
    deduped = []
    for item in ranked:
        if item["keyword"] in seen:
            continue
        seen.add(item["keyword"])
        deduped.append(item)

    deduped.sort(key=lambda x: x["volume"], reverse=True)
    return deduped[:n]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", type=int, default=3, help="뽑을 키워드 개수 (기본 3)")
    parser.add_argument("--dry-run", action="store_true", help="used_keywords.json에 기록하지 않음")
    args = parser.parse_args()

    top = pick_top_keywords(args.n)
    if not top:
        print(json.dumps({"error": "선정할 키워드가 없습니다. 시드 풀을 확인하세요."}, ensure_ascii=False))
        return

    if not args.dry_run:
        today = time.strftime("%Y-%m-%d")
        used = load_used()
        save_used(used, [item["keyword"] for item in top], today)

    print(json.dumps(top, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
