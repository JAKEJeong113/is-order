# catalog_margin.py
"""도매몰에서 크롤링한 원가로 추천판매가를 계산하는 순수 로직만 모아둔 모듈
(DB/크롤링과 분리해서 계산식만 따로 테스트하기 쉽게 하기 위함).

계산 방식은 사용자가 명시적으로 확인한 "총이익률" 방식이다:
    판매가 = 원가 ÷ (1 - 마진율)
    예) 원가 1,000원, 마진 40% -> 1,000 ÷ 0.6 = 1,667원
        원가 1,000원, 마진 50% -> 1,000 ÷ 0.5 = 2,000원

마진율은 고정값이 아니라 40~50% 범위 안에서, round_unit(기본 100원) 단위로
반올림했을 때 원래 계산값과 가장 차이가 적은(=가장 "깔끔하게 떨어지는")
마진을 골라 쓴다. 오차가 동률이면 40~50% 중간값(45%)에 더 가까운 쪽을
우선한다(항상 극단값으로 쏠리지 않게 하기 위함).
"""

DEFAULT_ROUND_UNIT = 100
DEFAULT_MARGIN_RANGE = (40, 50)


def compute_recommended_price(
    unit_cost: float,
    round_unit: int = DEFAULT_ROUND_UNIT,
    margin_min: int = DEFAULT_MARGIN_RANGE[0],
    margin_max: int = DEFAULT_MARGIN_RANGE[1],
) -> tuple[int, int]:
    """(추천판매가, 적용된 마진율%) 튜플을 반환한다."""
    if unit_cost <= 0:
        raise ValueError("unit_cost는 0보다 커야 합니다")
    if margin_min >= margin_max:
        raise ValueError("margin_min은 margin_max보다 작아야 합니다")

    mid = (margin_min + margin_max) / 2
    best = None  # (정렬키, 마진율, 반올림된 가격)

    for margin_pct in range(margin_min, margin_max + 1):
        raw_price = unit_cost / (1 - margin_pct / 100)
        rounded_price = round(raw_price / round_unit) * round_unit
        if rounded_price <= 0:
            continue
        delta = abs(raw_price - rounded_price)
        sort_key = (delta, abs(margin_pct - mid))
        if best is None or sort_key < best[0]:
            best = (sort_key, margin_pct, rounded_price)

    if best is None:
        raise ValueError(f"unit_cost={unit_cost}에 대한 추천판매가를 계산할 수 없습니다")

    _, margin_pct, rounded_price = best
    return rounded_price, margin_pct
