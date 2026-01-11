import base64
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "https://api.a-bly.com/api/v2/screens/SUB_CATEGORY_DEPARTMENT/"
REVIEW_API_URL = "https://api.a-bly.com/api/v2/goods/{sno}/review_summary/"
LEGAL_NOTICE_API_URL = "https://api.a-bly.com/api/v2/goods/{sno}/legal_notice/"
DETAIL_API_URL = "https://api.a-bly.com/api/v3/goods/{sno}/detail/"
OPTIONS_API_URL = "https://api.a-bly.com/api/v2/goods/{sno}/options/"
BASIC_API_URL = "https://api.a-bly.com/api/v3/goods/{sno}/basic/"


# ably 카테고리 API(overviewCategories) 기준
# - 여기서 "아우터/상의/팬츠/스커트/원피스"는 depth 1(상위)이고,
# - 실제 수집은 그 하위 subCategoryList(depth 2) 기준으로 순회한다.
#
# 주의: subCategoryList의 item.sno(예: 926x)는 화면용 id이고,
# 실제 next_token에 들어가는 category_sno는 logging.analytics.CATEGORY_SNO 값(예: 293)이다.
CATEGORIES: dict[str, dict[str, Any]] = {
    "아우터": {
        "sno": 7,
        "subcategories": {
            "가디건": 16,
            "자켓": 293,
            "집업/점퍼": 294,
            "바람막이": 497,
            "코트": 296,
            "플리스": 577,
            "야상": 496,
            "패딩": 297,
        },
    },
    "상의": {
        "sno": 8,
        "subcategories": {
            "후드": 500,
            "맨투맨": 300,
            "니트": 299,
            "셔츠": 499,
            "긴소매티셔츠": 498,
            "블라우스": 298,
            "조끼": 357,
            "반소매티셔츠": 18,
            "민소매": 21,
        },
    },
    "팬츠": {
        "sno": 174,
        "subcategories": {
            "롱팬츠": 176,
            "슬랙스": 178,
            "데님": 501,
            "숏팬츠": 177,
        },
    },
    "스커트": {
        "sno": 203,
        "subcategories": {
            "미디/롱스커트": 205,
            "미니 스커트": 204,
        },
    },
    # API 상 이름은 "원피스/세트" 이지만, 사용 편의상 "원피스" 키로 둠
    "원피스": {
        "sno": 10,
        "api_name": "원피스/세트",
        "subcategories": {
            "롱원피스": 207,
            "투피스": 208,
            "점프수트": 533,
            "미니원피스": 206,
        },
    },
}


def build_ably_headers() -> dict[str, str]:
    """
    Ably API 호출 헤더.
    - 기본값은 main_api.py와 동일하게 둠
    - 운영에서는 env로 덮어쓰는 걸 권장 (토큰/디바이스 값 만료 가능)
    """
    headers = {
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
        "user-agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 "
            "Mobile/15E148 Safari/604.1"
        ),
        "x-anonymous-token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhbm9ueW1vdXNfaWQiOiI4MDc4MDMxODkiLCJpYXQiOjE3NjgwNjg2MDV9.VLJgodKMn0Mkounf6APU887rLZQAgYWvWy1hRVB3aFE",
        "x-app-version": "0.1.0",
        "x-device-id": "99e795d7-a1b1-44da-b2b5-263f1743b0a2",
        "x-device-type": "MobileWeb",
        "x-web-type": "Web",
    }

    overrides = {
        "x-anonymous-token": os.getenv("ABLY_ANON_TOKEN"),
        "x-app-version": os.getenv("ABLY_APP_VERSION"),
        "x-device-id": os.getenv("ABLY_DEVICE_ID"),
        "user-agent": os.getenv("ABLY_USER_AGENT"),
    }
    for k, v in overrides.items():
        if v:
            headers[k] = v
    return headers


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


def build_category_targets(
    *, all_categories: bool, category: str | None, subcategory: str | None
) -> list[tuple[str, int]]:
    if all_categories:
        targets: list[tuple[str, int]] = []
        for category_name, cat in CATEGORIES.items():
            subs: dict[str, int] = cat.get("subcategories") or {}  # type: ignore[assignment]
            if subs:
                targets.extend(
                    (f"{category_name}/{name}", int(sno)) for name, sno in subs.items()
                )
            else:
                targets.append((category_name, int(cat["sno"])))
        return targets

    if not category:
        category = "아우터"

    if category not in CATEGORIES:
        raise KeyError(f"Unknown category: {category}")
    cat = CATEGORIES[category]
    subs: dict[str, int] = cat.get("subcategories") or {}  # type: ignore[assignment]

    if subcategory:
        if subcategory not in subs:
            raise KeyError(f"Unknown subcategory for {category}: {subcategory}")
        return [(f"{category}/{subcategory}", int(subs[subcategory]))]

    if subs:
        return [(f"{category}/{name}", int(sno)) for name, sno in subs.items()]
    return [(category, int(cat["sno"]))]


def extract_products_from_response(data: dict[str, Any]) -> list[dict[str, Any]]:
    products: list[dict[str, Any]] = []
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


def fetch_review_info(client: httpx.Client, sno: int) -> dict[str, Any] | None:
    try:
        response = client.get(REVIEW_API_URL.format(sno=sno))
        response.raise_for_status()
        data = response.json()
        review = data.get("review", {})
        return {
            "count": review.get("count", 0),
            "positive_percent": review.get("positive_percent", 0),
        }
    except Exception:
        return None


def fetch_legal_notice_meta(client: httpx.Client, sno: int) -> dict[str, Any]:
    try:
        response = client.get(LEGAL_NOTICE_API_URL.format(sno=sno))
        response.raise_for_status()
        data = response.json()
        return {
            "color_md": data.get("color_md"),
            "fabric": data.get("fabric"),
            "country": data.get("country"),
        }
    except Exception:
        return {}


def fetch_basic_meta(client: httpx.Client, sno: int) -> dict[str, Any]:
    try:
        response = client.get(BASIC_API_URL.format(sno=sno))
        response.raise_for_status()
        data = response.json()
        goods = data.get("goods", {})
        price_info = goods.get("price_info", {}) or {}
        cover_images = goods.get("cover_images", []) or []
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
    except Exception:
        return {}


def fetch_option_colors(client: httpx.Client, sno: int) -> list[str]:
    try:
        response = client.get(OPTIONS_API_URL.format(sno=sno), params={"depth": "1"})
        response.raise_for_status()
        data = response.json()

        option_name = data.get("name")
        if option_name not in ("컬러", "색상", "Color", "COLOR"):
            return []

        colors: list[str] = []
        for opt in data.get("option_components", []):
            name = opt.get("name")
            if isinstance(name, str) and name.strip():
                colors.append(name.strip())

        seen = set()
        unique: list[str] = []
        for c in colors:
            if c not in seen:
                seen.add(c)
                unique.append(c)
        return unique
    except Exception:
        return []


def clean_image_url(url: str) -> str | None:
    url = url.replace("&quot;", "").replace("\\&quot;", "")
    url = url.replace("&amp;", "&")
    url = url.strip('"').strip("'").strip()
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return None


def fetch_detail_images(client: httpx.Client, sno: int) -> list[str]:
    try:
        response = client.get(DETAIL_API_URL.format(sno=sno), params={"channel": "0"})
        response.raise_for_status()
        data = response.json()

        images: list[str] = []
        goods = data.get("goods", {})
        for part in goods.get("detail_html_parts", []):
            if part.get("html_part_type") != "DESCRIPTION":
                continue
            for content in part.get("contents", []):
                patterns = [
                    r'<img[^>]+src="([^"]+)"',
                    r"<img[^>]+src='([^']+)'",
                    r'<img[^>]+src=\\"([^\\]+)\\"',
                    r"<img[^>]+src=\\&quot;([^&]+)\\&quot;",
                ]
                for pattern in patterns:
                    img_urls = re.findall(pattern, content)
                    images.extend(img_urls)

        unique_images: list[str] = []
        seen = set()
        for url in images:
            cleaned = clean_image_url(url)
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                unique_images.append(cleaned)
        return unique_images
    except Exception:
        return []


def download_image(client: httpx.Client, url: str, save_path: Path) -> bool:
    try:
        response = client.get(url, follow_redirects=True)
        response.raise_for_status()
        save_path.write_bytes(response.content)
        return True
    except Exception:
        return False


def download_cover_images(
    *, client: httpx.Client, images_dir: Path, sno: int, cover_images: list[str]
) -> list[str]:
    if not cover_images:
        return []

    product_dir = images_dir / str(sno)
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


def download_detail_images(
    *, client: httpx.Client, images_dir: Path, sno: int
) -> list[str]:
    product_dir = images_dir / str(sno)
    product_dir.mkdir(parents=True, exist_ok=True)

    images = fetch_detail_images(client, sno)
    downloaded: list[str] = []
    for idx, url in enumerate(images):
        ext = "jpg"
        low = url.lower()
        if ".png" in low:
            ext = "png"
        elif ".gif" in low:
            ext = "gif"
        elif ".webp" in low:
            ext = "webp"
        filename = f"{idx + 1:03d}.{ext}"
        save_path = product_dir / filename
        if download_image(client, url, save_path):
            downloaded.append(str(save_path))
    return downloaded


def write_product_metadata(*, images_dir: Path, product: dict[str, Any]) -> None:
    sno = product["sno"]
    product_dir = images_dir / str(sno)
    product_dir.mkdir(parents=True, exist_ok=True)

    meta = {
        "sno": sno,
        "name": product.get("name"),
        "category": product.get("category"),
        "market_name": product.get("market_name"),
        "url": product.get("url"),
        "option_colors": product.get("option_colors") or [],
        "legal_notice_colors": product.get("colors"),
        "price_info": product.get("price_info"),
        "fabric": product.get("fabric"),
        "country": product.get("country"),
        "cover_images": product.get("cover_images") or [],
        "sell_count": product.get("sell_count"),
        "review_count": product.get("review_count"),
        "positive_percent": product.get("positive_percent"),
    }

    meta_path = product_dir / "meta.json"
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


@dataclass(frozen=True)
class CrawlConfig:
    min_purchase_count: int = 2000
    min_review_count: int = 100
    min_positive_percent: int = 95
    max_products_per_target: int = 10
    timeout_seconds: float = 60.0


def fetch_products_by_category(
    *,
    client: httpx.Client,
    category_sno: int,
    category_label: str,
    config: CrawlConfig,
    exclude_snos: set[int] | None = None,
) -> list[dict[str, Any]]:
    found_products: list[dict[str, Any]] = []
    checked_snos: set[int] = set()
    next_token = create_initial_token(category_sno)

    while len(found_products) < config.max_products_per_target:
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
            sno = product.get("sno")
            if not isinstance(sno, int):
                continue
            if exclude_snos and sno in exclude_snos:
                continue
            if sno in checked_snos:
                continue
            checked_snos.add(sno)

            if int(product.get("sell_count") or 0) < config.min_purchase_count:
                continue

            review = fetch_review_info(client, sno)
            if not review:
                continue

            if int(review.get("count") or 0) < config.min_review_count:
                continue

            if int(review.get("positive_percent") or 0) < config.min_positive_percent:
                continue

            product["url"] = build_product_url(sno)
            product["review_count"] = int(review.get("count") or 0)
            product["positive_percent"] = int(review.get("positive_percent") or 0)
            product["category"] = category_label
            found_products.append(product)
            if len(found_products) >= config.max_products_per_target:
                break

        next_token = data.get("next_token")
        if not next_token:
            break

    return found_products


def enrich_product_details(
    *,
    client: httpx.Client,
    product: dict[str, Any],
    images_dir: Path,
    include_cover_image_urls: bool,
    include_detail_image_urls: bool,
) -> None:
    sno = product["sno"]

    legal = fetch_legal_notice_meta(client, sno)
    product["colors"] = legal.get("color_md")
    product["fabric"] = legal.get("fabric")
    product["country"] = legal.get("country")

    option_colors = fetch_option_colors(client, sno)
    product["option_colors"] = option_colors

    basic = fetch_basic_meta(client, sno)
    product["price_info"] = basic.get("price_info")
    product["cover_images"] = basic.get("cover_images") or []

    # 이미지 저장은 하지 않음. 링크만 필요하면 cover_images는 이미 들어있고,
    # 상세 이미지는 HTML에서 URL을 파싱해 넣는다.
    if include_detail_image_urls:
        product["detail_images"] = fetch_detail_images(client, sno)
    else:
        product["detail_images"] = []

    # 서버(웹) 용도에서는 파일 저장 없이 DB에만 저장하는 걸 권장하므로,
    # meta.json 파일 저장은 crawl_ably_products 옵션(write_meta_file)에서 제어한다.


def save_results(*, output_dir: Path, products: list[dict[str, Any]]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / "products.json"

    for product in products:
        # 웹 서버에서는 로컬 이미지 저장을 하지 않으므로 경로 변환 불필요.
        # (CLI에서 저장한 결과를 그대로 덤프하는 용도로만 유지)
        pass

    output_file.write_text(
        json.dumps(products, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return output_file


def crawl_ably_products(
    *,
    all_categories: bool,
    category: str | None,
    subcategory: str | None,
    config: CrawlConfig,
    output_dir: Path,
    include_cover_image_urls: bool = True,
    include_detail_image_urls: bool = True,
    exclude_snos: set[int] | None = None,
    write_meta_file: bool = False,
    write_products_json: bool = False,
) -> dict[str, Any]:
    """
    main_api.py와 유사한 상품 수집 + (옵션) 이미지 다운로드.
    - 이미지 splitting 기능은 포함하지 않음.
    """
    images_dir = output_dir / "images"
    targets = build_category_targets(
        all_categories=all_categories, category=category, subcategory=subcategory
    )

    headers = build_ably_headers()
    with httpx.Client(headers=headers, timeout=config.timeout_seconds) as client:
        found_products: list[dict[str, Any]] = []
        for label, category_sno in targets:
            found_products.extend(
                fetch_products_by_category(
                    client=client,
                    category_sno=category_sno,
                    category_label=label,
                    config=config,
                    exclude_snos=exclude_snos,
                )
            )

        # 중복 제거(상품 sno 기준, 순서 유지)
        unique: list[dict[str, Any]] = []
        seen: set[int] = set()
        for p in found_products:
            sno = p.get("sno")
            if not isinstance(sno, int):
                continue
            if sno in seen:
                continue
            seen.add(sno)
            unique.append(p)
        found_products = unique

        for product in found_products:
            sno = product.get("sno")
            if not isinstance(sno, int):
                continue
            enrich_product_details(
                client=client,
                product=product,
                images_dir=images_dir,
                include_cover_image_urls=include_cover_image_urls,
                include_detail_image_urls=include_detail_image_urls,
            )
            if write_meta_file and (
                include_cover_image_urls or include_detail_image_urls
            ):
                write_product_metadata(images_dir=images_dir, product=product)

    products_path = (
        save_results(output_dir=output_dir, products=found_products)
        if write_products_json
        else None
    )
    return {
        "count": len(found_products),
        "output_dir": str(output_dir),
        "products_json": str(products_path) if products_path else None,
        "products": found_products,
    }
