# 무인매장 바코드 조회 (barcode_site)

is-order 본체에서 바코드 검색 기능 하나만 떼어낸 가벼운 독립 사이트.
로그인 없이 누구나 바코드/상품명으로 추천판매가를 조회할 수 있고,
결과 하단에 본체(i's ORDER) 안내 링크가 붙어있다 — 무인매장 발주 자동화
서비스로 유입을 유도하는 무료 도구 역할.

**DB는 본체와 완전히 공유한다.** 이 사이트는 `catalog_items` 테이블을
읽기만 하고, 테이블 생성/쓰기는 전혀 하지 않는다(본체가 유일한 소유자).

## 로컬 실행

```bash
pip install -r requirements.txt
cp .env.example .env   # DATABASE_URL을 본체와 동일한 값으로 채워넣기
uvicorn main:app --reload --port 8001
```

## Render 배포 (본체와 별도 서비스로)

1. Render 대시보드 → **New → Web Service**
2. 같은 GitHub 저장소(is-order) 선택
3. **Root Directory**를 `barcode_site`로 지정 (본체 코드와 섞이지 않게 이 폴더만 배포)
4. Build Command: `pip install -r requirements.txt`
5. Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
6. 환경변수:
   - `DATABASE_URL` — **본체(is-order) 서비스와 완전히 같은 값**으로 설정 (Render의 is-order 서비스 환경변수 탭에서 그대로 복사)
   - `MAIN_SITE_URL` — 기본값(`https://www.is-cream.co.kr`) 그대로 두면 됨
7. 배포 완료 후, 원하는 도메인(예: `barcode.is-cream.co.kr` 또는 완전히 별개 도메인)을 Render의 Custom Domain 설정에서 연결

## 왜 별도 배포인가

- 본체(main.py)는 Playwright·pandas 등 무거운 의존성을 포함한 발주 자동화 플랫폼 전체다.
- 이 사이트는 카탈로그 테이블 하나를 읽어서 보여주는 게 전부라, 완전히 분리해서
  더 가볍고 빠르게, 그리고 본체 배포/장애와 무관하게 독립적으로 운영한다.
- 이후 마케팅 목적(무료 도구로 비가맹점 잠재고객 유입)에도 본체 로그인 페이지를
  거치지 않는 별도 진입점이 유리하다.
