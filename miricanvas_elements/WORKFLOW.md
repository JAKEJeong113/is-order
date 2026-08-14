# 미리캔버스 요소 자동 생성 워크플로

사용자가 "오늘 미리캔버스 요소 만들어줘" 같은 요청을 하면, 이 대화형 세션에서
아래 순서를 그대로 따른다. 외부 API 키는 필요 없다.

> 완전 무인(예약 실행) 자동화는 시도했지만 포기했다 — `generate_image` 등
> 이미지 생성 MCP 도구가 새 세션에서 처음 쓰일 때 신뢰(승인) 절차를 거치는데,
> 이 절차가 대화형 세션에서만 통과되고 `claude -p` 헤드리스 실행에서는
> `--allowedTools`로 미리 허용해도, `use_unlim` 값을 명시해도 계속 막혔다.
> 그래서 매일 아침 사용자가 이 세션에서 직접 요청하면 실행하는 방식으로 운영한다.

## 1. 오늘의 주제 20개 선정
```bash
python miricanvas_elements/select_subjects.py
```
- `subjects_pool.json`(생활용품/사무용품/동물/식물/가전제품/주방용품/욕실용품/
  음식/패션잡화/교통수단/취미레저/계절이벤트 12개 카테고리, 총 240개 항목)에서
  랜덤으로 20개를 뽑음
- 같은 사이클(cycle) 안에서는 중복 없이 뽑히고, 풀을 한 바퀴 다 돌면 자동으로
  새 사이클을 시작해 무기한 반복
- 실행하면 오늘 뽑힌 주제가 `used_subjects.json`에 기록되고, 콘솔에 JSON
  배열로 출력됨. 예: `["텀블러", "고양이", "선인장", ...]`
- 풀에 주제를 더 추가하고 싶으면 `subjects_pool.json`을 직접 편집

## 2. 주제별로 요소 이미지 생성 (선정된 20개 각각 반복)
각 주제에 대해:

1. **이미지 생성** — `generate_image` 툴 사용. 반드시 아래 그대로:
   - `model: "recraft_v4_1"`, `model_type: "utility"` (래스터 PNG로 나옴 —
     `"vector"`는 SVG로 나와서 배경 제거/크롭이 안 되니 쓰지 말 것)
   - `background_color: "#FFFFFF"` (배경 제거를 쉽게 하기 위함)
   - **`use_unlim: false`를 항상 명시적으로 넣을 것.** 생략하면 무제한 생성권을
     쓸지 물어보는 확인 절차(`unlim_choice`)가 걸리는데, 이 자동화는 사람이
     없는 상태로 도는 거라 응답할 사람이 없어 그대로 멈춰버림. 매번 명시적으로
     `false`를 넣어야 막히지 않고 크레딧으로 바로 진행됨.
   - prompt 예: `"{주제}를 표현한 플랫 일러스트, 심플한 스티커 스타일, 순백색 배경, 테두리 없음"`
2. **배경 제거** — 생성 결과의 job_id를 `media_id`로 써서 `remove_background`
   툴 적용 (`media_type: "image"`)
3. **로컬 다운로드 + 경계선 크롭** — `remove_background` 결과의 `result_url`을
   Bash로 다운로드한 뒤:
   ```bash
   python miricanvas_elements/crop_transparent.py <다운로드한_이미지> \
       miricanvas_elements/output/<오늘날짜>/<주제>.png --padding 8
   ```

## 3. 결과 정리
- 오늘 생성된 20개 파일은 `miricanvas_elements/output/YYYY-MM-DD/` 에 저장됨
- 업로드는 자동화하지 않음 — 사용자가 직접 디자인허브(designhub.miricanvas.com)에서
  수동으로 업로드/심사 신청

## 참고
- `used_subjects.json`은 자동 생성/갱신되는 상태 파일이라 손대지 않아도 됨
- 하루 20개 생성은 이미지 생성 + 배경 제거를 각 20회씩 호출하므로 크레딧을
  꽤 소모함 — 주기적으로 크레딧 잔량 확인 권장
