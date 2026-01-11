from pathlib import Path
from PIL import Image
import numpy as np


def find_split_points(
    image: Image.Image, threshold: float = 0.98, min_gap: int = 20
) -> list[int]:
    """이미지에서 분할 지점 찾기 (균일한 색상의 가로줄 감지)"""
    img_array = np.array(image.convert("RGB"))
    height, width, _ = img_array.shape

    # 각 행의 픽셀 표준편차 계산 (낮으면 균일한 색상)
    row_std = np.std(img_array, axis=(1, 2))

    # 표준편차가 낮은 행 찾기 (균일한 색상 = 구분선)
    max_std = np.max(row_std)
    uniform_rows = row_std < (max_std * (1 - threshold))

    # 연속된 균일 행 그룹 찾기
    split_points = []
    in_uniform = False
    start = 0

    for i, is_uniform in enumerate(uniform_rows):
        if is_uniform and not in_uniform:
            start = i
            in_uniform = True
        elif not is_uniform and in_uniform:
            mid = (start + i) // 2
            # 이미지 상단/하단 근처는 제외
            if mid > min_gap and mid < height - min_gap:
                # 이전 분할점과 너무 가까우면 제외
                if not split_points or mid - split_points[-1] > min_gap:
                    split_points.append(mid)
            in_uniform = False

    return split_points


def split_image(
    image_path: Path, output_dir: Path | None = None, min_height: int = 100
) -> list[Path]:
    """이미지를 분할하고 저장"""
    image = Image.open(image_path)
    width, height = image.size

    # 세로로 긴 이미지가 아니면 분할 불필요
    if height < width * 1.5:
        return [image_path]

    split_points = find_split_points(image)

    if not split_points:
        return [image_path]

    # 출력 디렉토리 설정
    if output_dir is None:
        output_dir = image_path.parent / f"{image_path.stem}_split"
    output_dir.mkdir(parents=True, exist_ok=True)

    # 분할 지점으로 이미지 자르기
    saved_paths = []
    points = [0] + split_points + [height]

    suffix = image_path.suffix.lower()

    for i in range(len(points) - 1):
        top = points[i]
        bottom = points[i + 1]

        # 너무 작은 조각은 건너뛰기
        if bottom - top < min_height:
            continue

        cropped = image.crop((0, top, width, bottom))

        # JPEG는 RGBA 지원 안함 → RGB로 변환
        if suffix in [".jpg", ".jpeg"] and cropped.mode == "RGBA":
            background = Image.new("RGB", cropped.size, (255, 255, 255))
            background.paste(cropped, mask=cropped.split()[3])
            cropped = background

        output_path = output_dir / f"{image_path.stem}_{i + 1:02d}{image_path.suffix}"
        cropped.save(output_path)
        saved_paths.append(output_path)

    return saved_paths


def process_directory(
    input_dir: Path, output_dir: Path | None = None
) -> dict[str, list[Path]]:
    """디렉토리 내 모든 이미지 처리"""
    results = {}
    image_extensions = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

    for image_path in input_dir.iterdir():
        if image_path.suffix.lower() not in image_extensions:
            continue

        print(f"처리중: {image_path.name}")
        split_output = output_dir / image_path.stem if output_dir else None
        split_paths = split_image(image_path, split_output)

        if len(split_paths) > 1:
            print(f"  → {len(split_paths)}개로 분할됨")
        else:
            print(f"  → 분할 불필요")

        results[str(image_path)] = split_paths

    return results


def main():
    import sys

    if len(sys.argv) < 2:
        print("Usage: python image_splitter.py <image_path_or_directory>")
        return

    path = Path(sys.argv[1])

    if path.is_file():
        result = split_image(path)
        print(f"결과: {len(result)}개 이미지")
        for p in result:
            print(f"  - {p}")
    elif path.is_dir():
        results = process_directory(path)
        total_split = sum(len(v) for v in results.values())
        print(f"\n총 {len(results)}개 이미지 → {total_split}개로 분할")


if __name__ == "__main__":
    main()
