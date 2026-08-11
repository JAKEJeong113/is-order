# 미리캔버스 요소 자동 생성 워크플로

매일 실행되는 예약 에이전트가 아래 순서를 그대로 따른다.

## 0. 사전 준비 (사람이 한 번만)
1. `searchad.naver.com` 로그인 → 도구 > API 사용 관리 → API 서비스 신청
2. 발급된 `엑세스라이선스(API Key)`, `비밀키(Secret Key)`, `고객ID(Customer ID)`를
   프로젝트 루트 `.env`에 추가:
   ```
   NAVER_AD_API_KEY=...
   NAVER_AD_SECRET_KEY=...
   NAVER_AD_CUSTOMER_ID=...
   ```
3. `miricanvas_elements/keywords_seed.json`에 원하는 주제의 후보 키워드를 추가/수정
   (카테고리별로 자유롭게 편집 가능)

## 1. 오늘의 키워드 3개 선정
```bash
python miricanvas_elements/get_keywords.py
```
- 시드 풀 전체를 네이버 검색광고 연관키워드 API로 조회해 월간 검색량(PC+모바일)으로 정렬
- `used_keywords.json`에 이미 나온 키워드는 자동 제외
- 실행하면 오늘 뽑힌 키워드가 `used_keywords.json`에 기록되고, 콘솔에 JSON으로 출력됨
  예: `[{"keyword": "크리스마스", "volume": 40200}, ...]`

## 2. 키워드별로 요소 이미지 생성 (선정된 키워드 3개 각각 반복)
각 키워드에 대해:

1. **이미지 생성** — `generate_image` 툴 사용. 프롬프트 가이드:
   - 미리캔버스 "요소"(스티커/아이콘/일러스트)에 어울리는 flat vector illustration
     또는 sticker 스타일
   - 배경은 순백색 또는 단색 배경으로 지정 (배경 제거를 쉽게 하기 위함)
   - 예: `"{키워드}를 표현한 플랫 벡터 일러스트, 심플한 스티커 스타일, 순백색 배경, 테두리 없음"`
2. **배경 제거** — 생성된 이미지에 `remove_background` 툴 적용
3. **경계선 크롭** — 배경 제거된 이미지를 로컬에 저장한 뒤:
   ```bash
   python miricanvas_elements/crop_transparent.py <배경제거된_이미지> \
       miricanvas_elements/output/<오늘날짜>/<키워드>.png --padding 8
   ```

## 3. 결과 정리
- 오늘 생성된 3개 파일은 `miricanvas_elements/output/YYYY-MM-DD/` 에 저장됨
- 업로드는 자동화하지 않음 — 사용자가 직접 디자인허브(designhub.miricanvas.com)에서
  수동으로 업로드/심사 신청

## 참고
- `keywords_seed.json`의 후보가 바닥나면(전부 used) 새 키워드를 추가해야 함
- 네이버 검색광고 API는 `hintKeywords` 최대 5개까지만 한 번에 조회 가능 —
  `get_keywords.py`가 내부적으로 5개씩 나눠서 호출함
