import base64
import json
import re
from pathlib import Path
import httpx
from PIL import Image
import numpy as np

BASE_URL = "https://api.a-bly.com/api/v2/screens/SUB_CATEGORY_DEPARTMENT/"
REVIEW_API_URL = "https://api.a-bly.com/api/v2/goods/{sno}/review_summary/"
LEGAL_NOTICE_API_URL = "https://api.a-bly.com/api/v2/goods/{sno}/legal_notice/"
DETAIL_API_URL = "https://api.a-bly.com/api/v3/goods/{sno}/detail/"
OPTIONS_API_URL = "https://api.a-bly.com/api/v2/goods/{sno}/options/"
BASIC_API_URL = "https://api.a-bly.com/api/v3/goods/{sno}/basic/"

OUTPUT_DIR = Path("output")
IMAGES_DIR = OUTPUT_DIR / "images"

MIN_PURCHASE_COUNT = 2000
MIN_REVIEW_COUNT = 100
MIN_POSITIVE_PERCENT = 95
MAX_PRODUCTS = 10

CATEGORIES = {
    "자켓": 293,
    "코트": 294,
    "패딩": 295,
    "점퍼": 296,
    "가디건": 297,
}

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "ko,en-US;q=0.9,en;q=0.8,ja;q=0.7",
    "cache-control": "no-cache",
    "dnt": "1",
    "origin": "https://m.a-bly.com",
    "pragma": "no-cache",
    "referer": "https://m.a-bly.com/",
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Mobile/15E148 Safari/604.1",
    "x-anonymous-token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhbm9ueW1vdXNfaWQiOiI4MDc4MDMxODkiLCJpYXQiOjE3NjgwNjg2MDV9.VLJgodKMn0Mkounf6APU887rLZQAgYWvWy1hRVB3aFE",
    "x-app-version": "0.1.0",
    "x-device-id": "99e795d7-a1b1-44da-b2b5-263f1743b0a2",
    "x-device-type": "MobileWeb",
    "x-web-type": "Web",
}


def create_initial_token(category_sno: int) -> str:
    payload = {
        "l": "DepartmentCategoryRealtimeRankGenerator",
        "p": {"department_type": "CATEGORY", "category_sno": category_sno},
        "d": "CATEGORY",
        "previous_screen_name": "OVERVIEW",
        "category_sno": category_sno,
    }
    return base64.b64encode(json.dumps(payload, ensure_ascii=False).encode()).decode()


def build_product_url(sno: int) -> str:
    return f"https://m.a-bly.com/goods/{sno}"


def extract_products_from_response(data: dict) -> list[dict]:
    products = []
    for component in data.get("components", []):
        item_list = component.get("entity", {}).get("item_list", [])
        for item in item_list:
            if item.get("type") != "GOODS_CARD":
                continue
            item_entity = item.get("item_entity", {})
            item_data = item_entity.get("item", {})
            products.append(
                {
                    "sno": item_data.get("sno"),
                    "name": item_data.get("name"),
                    "sell_count": item_data.get("sell_count", 0),
                    "price": item_data.get("price"),
                    "market_name": item_data.get("market_name"),
                }
            )
    return products


def fetch_review_info(client: httpx.Client, sno: int) -> dict | None:
    try:
        response = client.get(REVIEW_API_URL.format(sno=sno))
        response.raise_for_status()
        data = response.json()
        review = data.get("review", {})
        return {
            "count": review.get("count", 0),
            "positive_percent": review.get("positive_percent", 0),
        }
    except Exception as e:
        print(f"  [!] 리뷰 조회 실패 (sno={sno}): {e}")
        return None


def fetch_color_info(client: httpx.Client, sno: int) -> str | None:
    """공시정보 API에서 색상 정보 가져오기"""
    try:
        response = client.get(LEGAL_NOTICE_API_URL.format(sno=sno))
        response.raise_for_status()
        data = response.json()
        return data.get("color_md")
    except Exception as e:
        print(f"  [!] 색상 정보 조회 실패 (sno={sno}): {e}")
        return None


def fetch_legal_notice_meta(client: httpx.Client, sno: int) -> dict:
    """공시정보 API에서 메타(소재/제조국 등) 가져오기"""
    try:
        response = client.get(LEGAL_NOTICE_API_URL.format(sno=sno))
        response.raise_for_status()
        data = response.json()
        return {
            "color_md": data.get("color_md"),
            "fabric": data.get("fabric"),
            "country": data.get("country"),
        }
    except Exception as e:
        print(f"  [!] 공시정보 조회 실패 (sno={sno}): {e}")
        return {}


def fetch_basic_meta(client: httpx.Client, sno: int) -> dict:
    """기본정보 API에서 가격/썸네일(cover_images) 가져오기"""
    try:
        response = client.get(BASIC_API_URL.format(sno=sno))
        response.raise_for_status()
        data = response.json()
        goods = data.get("goods", {})
        price_info = goods.get("price_info", {}) or {}
        cover_images = goods.get("cover_images", []) or []
        # cover_images는 URL 리스트
        cover_images = [
            u for u in cover_images if isinstance(u, str) and u.startswith("http")
        ]
        return {
            "price_info": {
                "consumer": price_info.get("consumer"),
                "thumbnail_price": price_info.get("thumbnail_price"),
                "discount_rate": price_info.get("discount_rate"),
            },
            "cover_images": cover_images,
        }
    except Exception as e:
        print(f"  [!] 기본정보 조회 실패 (sno={sno}): {e}")
        return {}


def fetch_option_colors(client: httpx.Client, sno: int) -> list[str]:
    """옵션 정보 API에서 '컬러' 옵션 값 가져오기"""
    try:
        response = client.get(OPTIONS_API_URL.format(sno=sno), params={"depth": "1"})
        response.raise_for_status()
        data = response.json()

        # 응답 예시는 { "name": "컬러", "option_components": [...] } 형태
        option_name = data.get("name")
        # 케이스: "컬러" / "색상" / "Color" 등
        if option_name not in ("컬러", "색상", "Color", "COLOR"):
            return []

        colors: list[str] = []
        for opt in data.get("option_components", []):
            name = opt.get("name")
            if isinstance(name, str) and name.strip():
                colors.append(name.strip())

        # 중복 제거(순서 유지)
        seen = set()
        unique: list[str] = []
        for c in colors:
            if c not in seen:
                seen.add(c)
                unique.append(c)
        return unique
    except Exception as e:
        print(f"  [!] 옵션 색상 조회 실패 (sno={sno}): {e}")
        return []


def clean_image_url(url: str) -> str | None:
    """이미지 URL 정리 (HTML 이스케이프 처리)"""
    # HTML 엔티티 디코딩
    url = url.replace("&quot;", "").replace("\\&quot;", "")
    url = url.replace("&amp;", "&")
    url = url.strip('"').strip("'").strip()

    # 유효한 URL인지 확인
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return None


def fetch_detail_images(client: httpx.Client, sno: int) -> list[str]:
    """상세페이지 API에서 이미지 URL 추출"""
    try:
        response = client.get(DETAIL_API_URL.format(sno=sno), params={"channel": "0"})
        response.raise_for_status()
        data = response.json()

        images = []
        goods = data.get("goods", {})

        # detail_html_parts에서 이미지 URL 추출
        for part in goods.get("detail_html_parts", []):
            if part.get("html_part_type") == "DESCRIPTION":
                for content in part.get("contents", []):
                    # img src 추출 (다양한 인용부호 패턴 처리)
                    patterns = [
                        r'<img[^>]+src="([^"]+)"',
                        r"<img[^>]+src='([^']+)'",
                        r'<img[^>]+src=\\"([^\\]+)\\"',
                        r"<img[^>]+src=\\&quot;([^&]+)\\&quot;",
                    ]
                    for pattern in patterns:
                        img_urls = re.findall(pattern, content)
                        images.extend(img_urls)

        # 중복 제거 및 URL 정리
        unique_images = []
        seen = set()
        for url in images:
            cleaned = clean_image_url(url)
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                unique_images.append(cleaned)

        return unique_images
    except Exception as e:
        print(f"  [!] 상세 이미지 조회 실패 (sno={sno}): {e}")
        return []


def download_image(client: httpx.Client, url: str, save_path: Path) -> bool:
    """이미지 다운로드"""
    try:
        response = client.get(url, follow_redirects=True)
        response.raise_for_status()
        save_path.write_bytes(response.content)
        return True
    except Exception as e:
        print(f"    [!] 이미지 다운로드 실패: {e}")
        return False


def download_cover_images(
    client: httpx.Client, sno: int, cover_images: list[str]
) -> list[str]:
    """basic API의 cover_images(썸네일/대표이미지) 다운로드"""
    if not cover_images:
        return []

    product_dir = IMAGES_DIR / str(sno)
    product_dir.mkdir(parents=True, exist_ok=True)

    downloaded: list[str] = []
    for idx, url in enumerate(cover_images, 1):
        ext = "jpg"
        low = url.lower()
        if ".png" in low:
            ext = "png"
        elif ".webp" in low:
            ext = "webp"
        elif ".gif" in low:
            ext = "gif"

        save_path = product_dir / f"cover_{idx:02d}.{ext}"
        if download_image(client, url, save_path):
            downloaded.append(str(save_path))

    return downloaded


def find_split_points(
    image: Image.Image, threshold: float = 0.98, min_gap: int = 50
) -> list[int]:
    """이미지에서 분할 지점 찾기 (균일한 색상의 가로줄 감지)"""
    img_array = np.array(image.convert("RGB"))
    height, width, _ = img_array.shape

    # 각 행의 픽셀 표준편차 계산 (낮으면 균일한 색상)
    row_std = np.std(img_array, axis=(1, 2))

    # 표준편차가 낮은 행 찾기 (균일한 색상 = 구분선)
    max_std = np.max(row_std)
    if max_std == 0:
        return []
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
            if mid > min_gap and mid < height - min_gap:
                if not split_points or mid - split_points[-1] > min_gap:
                    split_points.append(mid)
            in_uniform = False

    return split_points


def split_image(image_path: Path, min_height: int = 100) -> list[Path]:
    """이미지를 분할하고 저장 (원본 파일 대체)"""
    try:
        image = Image.open(image_path)
    except Exception:
        return [image_path]

    width, height = image.size

    # 세로로 긴 이미지가 아니면 분할 불필요
    if height < width * 1.5:
        return [image_path]

    split_points = find_split_points(image)

    if not split_points:
        return [image_path]

    # 분할 지점으로 이미지 자르기
    saved_paths = []
    points = [0] + split_points + [height]
    stem = image_path.stem
    suffix = image_path.suffix.lower()
    parent = image_path.parent

    for i in range(len(points) - 1):
        top = points[i]
        bottom = points[i + 1]

        if bottom - top < min_height:
            continue

        cropped = image.crop((0, top, width, bottom))

        # JPEG는 RGBA 지원 안함 → RGB로 변환
        if suffix in [".jpg", ".jpeg"] and cropped.mode == "RGBA":
            # 흰색 배경에 합성
            background = Image.new("RGB", cropped.size, (255, 255, 255))
            background.paste(cropped, mask=cropped.split()[3])
            cropped = background

        output_path = parent / f"{stem}_{i + 1:02d}{suffix}"
        cropped.save(output_path)
        saved_paths.append(output_path)

    # 분할 성공시 원본 삭제
    if len(saved_paths) > 1:
        image_path.unlink()

    return saved_paths


def download_product_images(client: httpx.Client, product: dict) -> list[str]:
    """상품의 상세 이미지 다운로드 및 분할"""
    sno = product["sno"]
    product_dir = IMAGES_DIR / str(sno)
    product_dir.mkdir(parents=True, exist_ok=True)

    images = fetch_detail_images(client, sno)
    all_images = []

    for idx, url in enumerate(images):
        ext = "jpg"
        if ".png" in url.lower():
            ext = "png"
        elif ".gif" in url.lower():
            ext = "gif"
        elif ".webp" in url.lower():
            ext = "webp"

        filename = f"{idx + 1:03d}.{ext}"
        save_path = product_dir / filename

        if download_image(client, url, save_path):
            # 이미지 분할 시도
            split_paths = split_image(save_path)
            if len(split_paths) > 1:
                print(f"    ✂️ {filename} → {len(split_paths)}개로 분할")
            all_images.extend(str(p) for p in split_paths)

    return all_images


def write_product_metadata(product: dict) -> None:
    """분류 단계에서 사용할 상품 메타데이터를 이미지 폴더에 저장"""
    sno = product["sno"]
    product_dir = IMAGES_DIR / str(sno)
    product_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "sno": sno,
        "name": product.get("name"),
        "category": product.get("category"),
        "market_name": product.get("market_name"),
        "url": product.get("url"),
        # 옵션 기반 색상(정답 소스)
        "option_colors": product.get("option_colors") or [],
        # 참고: 공시정보 색상(콤마 문자열일 수 있음)
        "legal_notice_colors": product.get("colors"),
        # 가격 정보 (basic)
        "price_info": product.get("price_info"),
        # 소재/제조국 (legal_notice)
        "fabric": product.get("fabric"),
        "country": product.get("country"),
        # 썸네일 URL (basic)
        "cover_images": product.get("cover_images") or [],
        # 참고용
        "sell_count": product.get("sell_count"),
        "review_count": product.get("review_count"),
        "positive_percent": product.get("positive_percent"),
    }

    meta_path = product_dir / "meta.json"
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def fetch_products_by_category(
    category_sno: int, category_name: str = ""
) -> list[dict]:
    found_products: list[dict] = []
    checked_snos: set[int] = set()
    next_token = create_initial_token(category_sno)

    print(f"\n{'=' * 50}")
    print(f"카테고리: {category_name} (sno={category_sno})")
    print(f"{'=' * 50}")

    with httpx.Client(headers=HEADERS, timeout=30) as client:
        while len(found_products) < MAX_PRODUCTS:
            params = {
                "next_token": next_token,
                "category_list[]": str(category_sno),
                "sorting_type": "POPULAR",
            }

            response = client.get(BASE_URL, params=params)
            response.raise_for_status()
            data = response.json()

            products = extract_products_from_response(data)

            for product in products:
                sno = product["sno"]
                if sno in checked_snos:
                    continue
                checked_snos.add(sno)

                if product["sell_count"] < MIN_PURCHASE_COUNT:
                    continue

                print(
                    f"검사중: {product['name'][:40]}... ({product['sell_count']:,}개 구매)"
                )

                review = fetch_review_info(client, sno)
                if not review:
                    continue

                if review["count"] < MIN_REVIEW_COUNT:
                    print(
                        f"  ❌ 리뷰 수 부족: {review['count']}개 < {MIN_REVIEW_COUNT}개"
                    )
                    continue

                if review["positive_percent"] < MIN_POSITIVE_PERCENT:
                    print(
                        f"  ❌ 긍정률 부족: {review['positive_percent']}% < {MIN_POSITIVE_PERCENT}%"
                    )
                    continue

                product["url"] = build_product_url(sno)
                product["review_count"] = review["count"]
                product["positive_percent"] = review["positive_percent"]
                product["category"] = category_name
                found_products.append(product)

                print(
                    f"  ✅ [{len(found_products)}/{MAX_PRODUCTS}] 리뷰 {review['count']}개, 긍정률 {review['positive_percent']}%"
                )

                if len(found_products) >= MAX_PRODUCTS:
                    break

            if len(found_products) >= MAX_PRODUCTS:
                break

            next_token = data.get("next_token")
            if not next_token:
                print("No more pages")
                break

    return found_products


def enrich_product_details(products: list[dict]) -> None:
    """상품 상세 정보(색상, 이미지) 추가"""
    print(f"\n{'=' * 50}")
    print("상세 정보 수집 중...")
    print(f"{'=' * 50}")

    with httpx.Client(headers=HEADERS, timeout=60) as client:
        for i, product in enumerate(products, 1):
            sno = product["sno"]
            print(f"\n[{i}/{len(products)}] {product['name'][:40]}...")

            # 공시정보(색상/소재/제조국)
            legal = fetch_legal_notice_meta(client, sno)
            product["colors"] = legal.get("color_md")
            product["fabric"] = legal.get("fabric")
            product["country"] = legal.get("country")
            if product.get("colors"):
                print(f"  🎨 색상(공시): {product['colors']}")
            if product.get("fabric"):
                print(f"  🧵 소재(혼용률): {product['fabric']}")
            if product.get("country"):
                print(f"  🌍 제조국: {product['country']}")

            # 옵션(컬러) 정보: 분류의 정답 소스
            option_colors = fetch_option_colors(client, sno)
            product["option_colors"] = option_colors
            if option_colors:
                print(f"  🎨 옵션 색상: {', '.join(option_colors)}")

            # 기본정보(가격/썸네일)
            basic = fetch_basic_meta(client, sno)
            product["price_info"] = basic.get("price_info")
            product["cover_images"] = basic.get("cover_images") or []
            if product.get("price_info"):
                pi = product["price_info"] or {}
                cp = pi.get("consumer")
                tp = pi.get("thumbnail_price")
                dr = pi.get("discount_rate")
                msg = []
                if tp is not None:
                    msg.append(f"{tp:,}원")
                if dr is not None:
                    msg.append(f"{dr}%")
                if cp is not None:
                    msg.append(f"(정가 {cp:,}원)")
                if msg:
                    print(f"  💰 가격: {' '.join(msg)}")

            # 썸네일(cover_images) 다운로드
            cover_local = download_cover_images(
                client, sno, product.get("cover_images") or []
            )
            product["cover_images_local"] = cover_local
            if cover_local:
                print(f"  🖼️ 썸네일: {len(cover_local)}개 다운로드")

            # 분류 단계에서 사용할 메타데이터 저장
            write_product_metadata(product)

            # 상세 이미지 다운로드
            downloaded = download_product_images(client, product)
            product["images"] = downloaded
            print(f"  📷 이미지: {len(downloaded)}개 다운로드")


def save_results(products: list[dict]) -> None:
    """결과를 JSON 파일로 저장"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_file = OUTPUT_DIR / "products.json"

    # 이미지 경로를 상대 경로로 변환
    for product in products:
        if "images" in product:
            product["images"] = [
                str(Path(p).relative_to(OUTPUT_DIR)) for p in product["images"]
            ]

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)

    print(f"\n💾 결과 저장: {output_file}")


def main():
    category_name = "자켓"
    category_sno = CATEGORIES[category_name]

    # 1. 상품 검색
    found_products = fetch_products_by_category(category_sno, category_name)

    if not found_products:
        print("검색 결과가 없습니다.")
        return

    # 2. 상세 정보 수집 (색상 + 이미지 다운로드)
    enrich_product_details(found_products)

    # 3. 결과 저장
    save_results(found_products)

    # 4. 결과 출력
    print("\n" + "=" * 50)
    print(f"최종 결과: {len(found_products)}개 상품")
    print("=" * 50)

    for i, product in enumerate(found_products, 1):
        print(f"\n{i}. {product['name']}")
        print(
            f"   구매: {product['sell_count']:,}개 | 리뷰: {product['review_count']}개 | 긍정률: {product['positive_percent']}%"
        )
        print(f"   색상: {product.get('option_colors', 'N/A')}")
        print(f"   이미지: {len(product.get('images', []))}개")
        print(f"   {product['url']}")


if __name__ == "__main__":
    main()
