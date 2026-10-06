# catalog_crawler.py
"""활성화된 도매처의 전체상품을 크롤링해서 catalog_cache에 저장한다."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import cafe24_bot
import catalog_cache
import godomall_bot
import vendors
import yamimall_bot

_crawl_lock = threading.Lock()
_is_crawling = False
BASE_DIR = Path(__file__).resolve().parent

# 도매처 하나가 응답 없이 멈추면(사이트 이슈 등) 전체 크롤링이 무한정 멈추는 걸
# 막기 위한 상한선. 이 시간을 넘기면 그 도매처만 실패 처리하고 다음으로 넘어간다.
VENDOR_CRAWL_TIMEOUT_SECONDS = 20 * 60


def is_crawl_running() -> bool:
    return _is_crawling


def _crawl_vendor_products(vendor_id: str, meta: dict, login_id: str, login_pwd: str) -> list[dict]:
    base_url = meta["base_url"]

    if vendor_id == "yamimall":
        return yamimall_bot.crawl_full_catalog(login_id, login_pwd)
    if vendor_id in ("ccdome", "3bong", "hdinter"):
        return godomall_bot.crawl_full_catalog(base_url, login_id, login_pwd, meta["catalog_category_code"])
    if vendor_id == "moomarket":
        return cafe24_bot.crawl_full_catalog(base_url, login_id, login_pwd, meta["catalog_category_code"])
    if vendor_id == "douyou":
        return yamimall_bot.crawl_full_catalog(
            login_id, login_pwd, base_url=base_url, category_codes=meta["catalog_category_code"],
        )
    if vendor_id == "mud5":
        # 도윤상사도 야미몰과 같은 플랫폼이지만 base_url이 http://라 ":443"을
        # 붙이면 접속 자체가 실패한다(vendors.py의 mud5 설정 주석 참고) -
        # use_port_suffix=False로 그 접미사를 뺀다.
        return yamimall_bot.crawl_full_catalog(
            login_id, login_pwd, base_url=base_url, category_codes=meta["catalog_category_code"],
            use_port_suffix=meta.get("list_page_use_port_suffix", True),
        )
    raise ValueError("이 도매처는 아직 전체상품 수집을 지원하지 않습니다")


def _kill_process_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)  # start_new_session=True라 pid가 곧 그룹 id
        except ProcessLookupError:
            pass
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass


def _run_crawl_subprocess(vendor_id: str, timeout_seconds: int) -> list[dict]:
    """크롤링을 별도 프로세스(catalog_crawl_worker)에서 돌리고, 시간이 넘으면
    프로세스 그룹째(Chromium 포함) 죽인다. 예전엔 스레드로 돌려서 타임아웃이
    나도 크롤링/브라우저가 서버 안에 계속 남아 메모리가 쌓였다(2026-10-07 새벽
    메모리 100% -> 서버 재시작). 프로세스가 끝나면 메모리도 모두 반환된다.
    시간 초과는 subprocess.TimeoutExpired로 올린다."""
    fd, out_path = tempfile.mkstemp(prefix=f"crawl_{vendor_id}_", suffix=".json")
    os.close(fd)
    err_path = out_path + ".err"
    try:
        with open(err_path, "w", encoding="utf-8") as err_file:
            proc = subprocess.Popen(
                [sys.executable, "-m", "catalog_crawl_worker", vendor_id, out_path],
                cwd=str(BASE_DIR), stderr=err_file, start_new_session=(os.name != "nt"),
            )
            try:
                proc.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc)
                raise
        if proc.returncode != 0:
            tail = Path(err_path).read_text(encoding="utf-8", errors="replace")[-1500:].strip()
            raise RuntimeError(tail or f"크롤링 프로세스가 비정상 종료됨(코드 {proc.returncode})")
        with open(out_path, encoding="utf-8") as f:
            return json.load(f)
    finally:
        for path in (out_path, err_path):
            try:
                os.unlink(path)
            except OSError:
                pass


def crawl_vendor(vendor_id: str) -> dict:
    creds = vendors.get_vendor_credentials(vendor_id)
    if not creds:
        return {"vendor_id": vendor_id, "ok": False, "error": "계정 정보 없음"}

    try:
        products = _run_crawl_subprocess(vendor_id, VENDOR_CRAWL_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        error = f"{VENDOR_CRAWL_TIMEOUT_SECONDS // 60}분 넘게 응답이 없어 건너뜀(프로세스 종료)"
        print(f"[CATALOG_CRAWLER] {vendor_id} 크롤링 실패:", error)
        catalog_cache.record_refresh_error(vendor_id, error)
        return {"vendor_id": vendor_id, "ok": False, "error": error}
    except Exception as e:
        print(f"[CATALOG_CRAWLER] {vendor_id} 크롤링 실패:", e)
        catalog_cache.record_refresh_error(vendor_id, str(e))
        return {"vendor_id": vendor_id, "ok": False, "error": str(e)}

    # 예외 없이 "성공"으로 끝났는데 상품이 0개면 진짜로 그 도매처가 텅 빈 게
    # 아니라 로그인 세션이 미묘하게 깨졌거나 사이트 구조가 바뀌는 등 크롤링
    # 자체가 조용히 실패한 경우일 가능성이 훨씬 크다(실측: 과자생각이 이 경로로
    # 캐시가 통째로 0건이 됨). 이 상태를 그대로 replace_vendor_catalog에 넘기면
    # 멀쩡했던 기존 캐시까지 빈 걸로 덮어써버리므로, 0건이면 기존 캐시를 그대로
    # 두고 실패로 기록해서 다음 크롤링 때 회복을 노린다.
    if not products:
        error = "크롤링 결과 0건 - 기존 캐시를 유지합니다(사이트 구조 변경/로그인 세션 문제 가능성)"
        print(f"[CATALOG_CRAWLER] {vendor_id} 크롤링 실패:", error)
        catalog_cache.record_refresh_error(vendor_id, error)
        return {"vendor_id": vendor_id, "ok": False, "error": error}

    catalog_cache.replace_vendor_catalog(vendor_id, products)
    return {"vendor_id": vendor_id, "ok": True, "product_count": len(products)}


def crawl_all_enabled() -> list[dict]:
    global _is_crawling
    if not _crawl_lock.acquire(blocking=False):
        print("[CATALOG_CRAWLER] 이미 크롤링이 진행 중이라 이번 실행은 건너뜁니다.")
        return []

    try:
        _is_crawling = True
        enabled_ids = vendors.get_enabled_vendor_ids()
        return [crawl_vendor(vid) for vid in enabled_ids]
    finally:
        _is_crawling = False
        _crawl_lock.release()
