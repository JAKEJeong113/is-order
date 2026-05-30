from mapping import load_coupang_catalog_xlsx, select_representative_item
from main import create_partners_link_from_search_keyword

catalog = load_coupang_catalog_xlsx("coupang_catalog_sample_10.xlsx")

# 샘플 주문 아이템 흉내
order_items = [
    {
        "바코드번호": "8801104123280",
        "메뉴명": "메로나",
        "추천발주량": 28,
        "추천발주량_포장반영": 28,
    },
    {
        "바코드번호": "8801062417414",
        "메뉴명": "옥동자",
        "추천발주량": 42,
        "추천발주량_포장반영": 42,
    },
]

rep = select_representative_item(order_items, catalog)

print("대표상품 =", rep)

if rep:
    print("대표 search_keyword =", rep.search_keyword)
    link = create_partners_link_from_search_keyword(rep.search_keyword)
    print("대표 파트너스 링크 =", link)
else:
    print("대표상품을 찾지 못했습니다.")