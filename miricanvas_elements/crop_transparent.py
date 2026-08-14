"""
배경이 제거된(투명 배경) PNG를 실제 그림의 경계선(bounding box)에 맞춰 자동으로 크롭한다.
크롭 후 짧은 변이 min_size보다 작으면 비율을 유지한 채 확대해 미리캔버스
업로드 최소 해상도(700px) 기준을 맞춘다.

사용법:
    python crop_transparent.py input.png output.png
    python crop_transparent.py input.png output.png --padding 8
    python crop_transparent.py input.png output.png --min-size 700
"""
import argparse

from PIL import Image


def crop_to_content(src_path: str, dst_path: str, padding: int = 0, min_size: int = 700) -> tuple[int, int]:
    img = Image.open(src_path).convert("RGBA")
    alpha = img.getchannel("A")
    bbox = alpha.getbbox()
    if bbox is None:
        raise ValueError(f"{src_path}: 완전히 투명한 이미지라 경계선을 찾을 수 없습니다.")

    if padding:
        left, top, right, bottom = bbox
        left = max(0, left - padding)
        top = max(0, top - padding)
        right = min(img.width, right + padding)
        bottom = min(img.height, bottom + padding)
        bbox = (left, top, right, bottom)

    cropped = img.crop(bbox)

    if min_size and min(cropped.size) < min_size:
        scale = min_size / min(cropped.size)
        new_size = (round(cropped.width * scale), round(cropped.height * scale))
        cropped = cropped.resize(new_size, Image.LANCZOS)

    cropped.save(dst_path)
    return cropped.size


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("--padding", type=int, default=0, help="경계선 바깥 여백(px), 기본 0")
    parser.add_argument("--min-size", type=int, default=700, help="짧은 변의 최소 픽셀, 기본 700 (미리캔버스 업로드 기준)")
    args = parser.parse_args()

    size = crop_to_content(args.input, args.output, args.padding, args.min_size)
    print(f"저장 완료: {args.output} ({size[0]}x{size[1]})")


if __name__ == "__main__":
    main()
