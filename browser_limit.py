# browser_limit.py
"""헤드리스 브라우저(Playwright) 동시 실행 개수를 전역으로 제한한다.

가맹점 수가 늘면서 여러 곳에서 동시에 도매처 로그인/카트담기/카탈로그
크롤링을 시도하면, 매 요청마다 새 Chromium 프로세스가 뜨는 지금 구조상
서버 메모리가 순식간에 소진될 수 있다. 각 브라우저 세션을 여는
`with sync_playwright() as p:` 블록 앞에 이 세마포어를 같이 걸어서
(`with browser_semaphore, sync_playwright() as p:`), 동시에 열리는
브라우저 개수를 MAX_CONCURRENT_BROWSERS로 제한한다 - 초과분은 자리가
날 때까지 자동으로 대기한다."""
import os
import threading

MAX_CONCURRENT_BROWSERS = int(os.getenv("MAX_CONCURRENT_BROWSERS", "4"))
browser_semaphore = threading.Semaphore(MAX_CONCURRENT_BROWSERS)
