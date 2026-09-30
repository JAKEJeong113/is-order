# 입출고 관리 (stock_site)

거래명세서 사진을 올리면 Claude Vision으로 품목을 읽고, 그 도매처의
크롤링된 상품 목록(`vendor_product_index` - 본체 `catalog_auto_import.py`가
채움) 안에서만 상품명을 대조해 바코드로 매칭한다. 정확히 하나로만 좁혀지는
품목은 재고에 바로 반영하고, 애매한 품목은 "확인 필요"로 남겨서 화면에서
직접 골라야 반영된다.

본체(is-order)와 완전히 분리된 별도 앱이다(추후 필요하면 합칠 수 있음).
DB는 같은 Postgres를 공유하지만, 이 앱은 `stock_levels`/`inbound_documents`/
`inbound_line_items`/`vendor_business_registry` 테이블만 직접 쓰고,
`vendor_product_index`는 읽기만 한다.

## 로컬 실행

```bash
pip install -r requirements.txt
cp .env.example .env
# DATABASE_URL: 본체와 동일한 값
# ANTHROPIC_API_KEY: console.anthropic.com에서 발급
# ADMIN_PASSWORD: 이 앱 전용 비밀번호(본체와 달라도 됨)
uvicorn main:app --reload --port 8002
```

## Render 배포 (본체와 별도 서비스로)

1. Render 대시보드 → New → Web Service
2. 같은 GitHub 저장소(is-order) 선택, Root Directory를 `stock_site`로 지정
3. Build Command: `pip install -r requirements.txt`
4. Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. 환경변수: `DATABASE_URL`(본체와 동일), `ANTHROPIC_API_KEY`, `ADMIN_PASSWORD`

## 새 도매처를 인식시키려면

첫 명세서를 올리면 사업자등록번호로 도매처를 못 찾아 "도매처 미확인"으로
뜬다. 문서함에서 도매처를 한 번 선택해주면 그 사업자번호를 기억해서
다음부터는 자동으로 인식한다.

## 매칭이 잘 안 될 때

- `vendor_product_index`가 비어있으면(그 도매처를 아직 한 번도
  `catalog_auto_import.py`로 크롤링한 적 없으면) 전부 "확인 필요"로만
  뜬다 - 본체에서 `python catalog_auto_import.py <vendor_id>`를 한 번
  돌려주면 채워진다(매주 월요일 새벽 자동으로도 돈다).
- 상품명이 조금이라도 다르면(예: 도매몰이 상품명을 중간에 바꾼 경우)
  정확히 일치하는 게 없어 "확인 필요"로 빠진다 - 검수 화면에서 검색해서
  고르면 되고, 이후 크롤링이 그 이름으로 다시 갱신되면 다음부터는 자동
  매칭된다.
