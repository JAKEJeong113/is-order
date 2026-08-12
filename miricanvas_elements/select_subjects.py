"""
"일상생활에서 볼 수 있는 모든 것" 풀(subjects_pool.json)에서 매일 랜덤으로
주제를 뽑는다. 외부 API 없이 로컬에서만 동작.

사용법:
    python select_subjects.py            # 오늘의 주제 20개를 뽑아 출력 + 기록
    python select_subjects.py --dry-run  # 기록하지 않고 뽑기만 (테스트용)
    python select_subjects.py -n 10      # 개수 조절

풀을 다 쓰면(한 바퀴 돌면) 자동으로 초기화하고 새 바퀴(cycle)를 시작해서
무기한 반복 가능. subjects_pool.json은 카테고리별로 자유롭게 추가/수정 가능.
"""
import argparse
import json
import random
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
POOL_FILE = BASE_DIR / "subjects_pool.json"
STATE_FILE = BASE_DIR / "used_subjects.json"


def load_pool() -> list[str]:
    data = json.loads(POOL_FILE.read_text(encoding="utf-8"))
    items = []
    for category_items in data.values():
        items.extend(category_items)
    # 중복 제거, 순서 유지
    seen = set()
    unique = []
    for item in items:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"cycle": 1, "used_this_cycle": [], "history": {}}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def pick_today(n: int, state: dict, pool: list[str]) -> list[str]:
    used_this_cycle = set(state["used_this_cycle"])
    available = [item for item in pool if item not in used_this_cycle]

    if len(available) < n:
        # 풀을 한 바퀴 다 돌았으면 새 사이클로 초기화
        state["cycle"] += 1
        state["used_this_cycle"] = []
        used_this_cycle = set()
        available = list(pool)

    picked = random.sample(available, min(n, len(available)))
    state["used_this_cycle"].extend(picked)
    return picked


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", type=int, default=20, help="뽑을 주제 개수 (기본 20)")
    parser.add_argument("--dry-run", action="store_true", help="used_subjects.json에 기록하지 않음")
    args = parser.parse_args()

    pool = load_pool()
    state = load_state()
    picked = pick_today(args.n, state, pool)

    if not args.dry_run:
        today = time.strftime("%Y-%m-%d")
        state["history"][today] = picked
        save_state(state)

    print(json.dumps(picked, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
