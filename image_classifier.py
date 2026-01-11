"""
이미지 분류 에이전트 - Gemini Vision을 이용한 상품 이미지 분류
"""

import os
import json
import base64
import asyncio
import re
import shutil
from pathlib import Path
from dotenv import load_dotenv
from google import genai
from google.genai import types

# .env 파일 로드
load_dotenv()

# Gemini 클라이언트 설정
api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
if not api_key:
    raise ValueError("GOOGLE_API_KEY 또는 GEMINI_API_KEY 환경변수를 설정하세요")

client = genai.Client(api_key=api_key)
MODEL_ID = "gemini-2.5-flash"
MODEL_ID = os.environ.get("GEMINI_MODEL", MODEL_ID)

# 동시 요청 수 제한
MAX_CONCURRENT_REQUESTS = 10

# 이미지 분류 카테고리
IMAGE_CATEGORIES = {
    "worn_front": "착용샷 - 정면 (모델이 제품을 입고 정면을 보는 사진)",
    "worn_side": "착용샷 - 측면 (모델이 제품을 입고 옆모습)",
    "worn_back": "착용샷 - 후면 (모델이 제품을 입고 뒷모습)",
    "product_front": "제품샷 - 앞면 (제품만 보이는 앞면 사진, 행거/마네킹/평면)",
    "product_back": "제품샷 - 뒷면 (제품만 보이는 뒷면 사진)",
    "detail_neckline": "디테일 - 넥라인 (목 부분 클로즈업)",
    "detail_sleeve": "디테일 - 소매 (소매 부분 클로즈업)",
    "detail_hem": "디테일 - 밑단 (밑단 부분 클로즈업)",
    "detail_material": "디테일 - 소재 (원단/재질 클로즈업)",
    "detail_button": "디테일 - 단추/지퍼 (버튼, 지퍼 등 클로즈업)",
    "color_swatch": "컬러 스와치 (색상 비교 이미지)",
    "size_chart": "사이즈 차트/측정 정보",
    "product_info": "상품 정보 이미지 (사이즈/소재/혼용률/핏/상품체크 표 등)",
    "marketing": "마케팅/텍스트 이미지 (광고 문구, 설명 텍스트)",
    "other": "기타 (위 카테고리에 해당하지 않음)",
}


def parse_expected_colors(meta: dict | None) -> list[str]:
    if not meta:
        return []
    # 옵션 API 기반 색상(우선)
    opt = meta.get("option_colors")
    if isinstance(opt, list) and opt:
        out: list[str] = []
        seen: set[str] = set()
        for c in opt:
            if isinstance(c, str) and c.strip() and c.strip() not in seen:
                seen.add(c.strip())
                out.append(c.strip())
        return out

    # fallback: 공시정보 색상(콤마 문자열)
    raw = meta.get("legal_notice_colors") or meta.get("colors")
    if not raw or not isinstance(raw, str):
        return []
    parts = [p.strip() for p in re.split(r"[,/|]", raw) if p.strip()]
    # 중복 제거 (순서 유지)
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def build_prompt(meta: dict | None) -> str:
    """상품 메타데이터를 포함해 '어떤 옷'을 분류해야 하는지 가이드를 강화"""
    title = (meta or {}).get("name")
    category = (meta or {}).get("category")
    market = (meta or {}).get("market_name")
    expected_colors = parse_expected_colors(meta)

    meta_lines = []
    if title:
        meta_lines.append(f"- 상품명: {title}")
    if category:
        meta_lines.append(f"- 카테고리: {category}")
    if market:
        meta_lines.append(f"- 판매처: {market}")
    if expected_colors:
        meta_lines.append(f"- 예상 색상 옵션: {', '.join(expected_colors)}")

    meta_block = "\n".join(meta_lines) if meta_lines else "- (메타데이터 없음)"

    return f"""당신은 의류 상품 이미지 분류 전문가입니다.

아래 '타겟 상품'의 이미지를 분류합니다. 모델이 함께 착용한 다른 옷/가방/악세서리/배경의 색상은 무시하고,
반드시 '타겟 상품(주된 의류 1개)' 기준으로만 판단하세요.

타겟 상품 정보:
{meta_block}

아주 중요한 분류 규칙:
- 같은 상품이 **여러 색상으로 동시에 나열/비교**되어 보이면(세로로 여러 벌 배치, 컬러명 텍스트가 여러 개, “아이보리/베이지/차콜/블랙”처럼 여러 컬러 라벨) 이는 **color_swatch** 입니다.
  - 이 경우 제품이 여러 개 보이더라도 “제품샷”이 아니라 “컬러 라인업”이므로 color_swatch로 분류합니다.
  - color_swatch인 경우 color는 반드시 null 입니다.
    - 표/테이블 형태의 “SIZE”, “PRODUCT CHECK”, “혼용률”, “제조국”, “소재”, “핏”, “두께감/신축성/비침/안감” 등의 정보가 담긴 이미지는 **product_info** 로 분류합니다.
      - product_info인 경우 color는 보통 null 입니다(색상을 특정할 수 없으면 반드시 null).

이 이미지를 분석하고 아래 JSON 스키마로만 응답하세요:

1. category: 이미지 카테고리 (아래 중 하나 선택)
   - worn_front: 착용샷 정면 (모델이 제품을 입고 정면)
   - worn_side: 착용샷 측면
   - worn_back: 착용샷 후면
   - product_front: 제품샷 앞면 (행거/마네킹/평면에 제품만)
   - product_back: 제품샷 뒷면
   - detail_neckline: 넥라인 디테일
   - detail_sleeve: 소매 디테일
   - detail_hem: 밑단 디테일
   - detail_material: 소재/원단 디테일
   - detail_button: 단추/지퍼 디테일
   - color_swatch: 컬러 스와치
   - size_chart: 사이즈 차트
   - product_info: 상품 정보(사이즈/소재/혼용률/핏/상품체크 등 표/텍스트)
   - marketing: 마케팅/텍스트 이미지
   - other: 기타

2. color: 타겟 상품의 색상 (한글 단일 값 1개)
   - color_swatch(=여러 색상 라인업/비교) 이미지라면 null
   - product_info(표/정보) 이미지라면 보통 null
   - 색상을 확실히 모르면 null
   - '예상 색상 옵션'이 있으면 그 중 하나로만 출력 (그 외의 색상은 절대 출력하지 말 것)

3. confidence: 분류 확신도 (0.0 ~ 1.0)

4. has_multiple_items: 이미지에 여러 제품/색상이 함께 있는지 (true/false)
   - 여러 컬러가 함께 나열된 라인업(color_swatch)은 true가 일반적

5. quality_score: 이미지 품질 점수 (0.0 ~ 1.0)
   - 선명도, 조명, 제품 가시성 기준

6. extracted: **정규화된 텍스트 추출은 '소재/혼용률'만 수행**
   - composition: 혼용률/소재 구성 (예: "폴리 97%, 스판 3%")
   - material: 소재/원단 설명 (예: "울10% 아크릴60% 폴리30%")
   - 그 외(사이즈/핏/상품체크/제조국 등)는 추출하지 말고 null 로 두세요.

JSON만 반환하세요. 다른 텍스트 없이."""


def load_product_metadata(product_dir: Path) -> dict | None:
    """main_api.py가 저장한 meta.json 로드"""
    meta_path = product_dir / "meta.json"
    if not meta_path.exists():
        return None
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def update_product_metadata_with_extracted_composition(
    product_dir: Path, result: dict
) -> None:
    """
    분류 결과에서 추출된 소재/혼용률 정보를 meta.json에 병합 저장.
    기존 공시정보(fabric 등)와 충돌하지 않도록 별도 필드 사용.
    """
    meta_path = product_dir / "meta.json"
    if not meta_path.exists():
        return

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            return
    except Exception:
        return

    selected = result.get("selected")
    if not isinstance(selected, dict):
        return

    info_images = selected.get("info_images")
    if not isinstance(info_images, dict):
        return

    comp_item = info_images.get("composition")
    if not isinstance(comp_item, dict):
        return

    extracted = comp_item.get("extracted")
    if not isinstance(extracted, dict):
        return

    composition = extracted.get("composition")
    material = extracted.get("material")
    if not composition and not material:
        return

    # 충돌 방지: 이미지 기반 추출 결과는 별도 필드에 저장
    meta["extracted_composition"] = composition
    meta["extracted_material"] = material
    meta["extracted_composition_source"] = {
        "file_name": comp_item.get("file_name"),
        "file_path": comp_item.get("file_path"),
        "confidence": comp_item.get("confidence"),
    }

    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def coerce_color(raw_color, expected_colors: list[str]) -> str | None:
    """LLM이 리스트/콤마 문자열 등으로 반환해도 단일 색상으로 정규화"""
    if raw_color is None:
        return None

    # list -> 첫 값
    if isinstance(raw_color, list):
        raw_color = raw_color[0] if raw_color else None
        if raw_color is None:
            return None

    # "아이보리, 베이지" 처럼 온 경우 첫 토큰
    if isinstance(raw_color, str):
        c = raw_color.strip()
        if not c:
            return None
        # 콤마로 나열된 경우 첫 번째만
        if "," in c:
            c = c.split(",")[0].strip()
        # expected_colors가 있으면 그 중 하나만 허용
        if expected_colors:
            for opt in expected_colors:
                if opt and opt in c:
                    return opt
            return None
        return c

    return None


def encode_image_to_base64(image_path: Path) -> str:
    """이미지를 base64로 인코딩"""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def get_mime_type(image_path: Path) -> str:
    """파일 확장자로 MIME 타입 결정"""
    suffix = image_path.suffix.lower()
    mime_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }
    return mime_types.get(suffix, "image/jpeg")


async def classify_image_async(
    image_path: Path,
    semaphore: asyncio.Semaphore,
    progress: dict,
    prompt: str,
    expected_colors: list[str],
) -> dict:
    """단일 이미지 분류 (비동기)"""
    async with semaphore:
        try:
            image_data = encode_image_to_base64(image_path)
            mime_type = get_mime_type(image_path)

            # 동기 API를 비동기로 실행
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: client.models.generate_content(
                    model=MODEL_ID,
                    contents=[
                        types.Content(
                            role="user",
                            parts=[
                                types.Part.from_bytes(
                                    data=base64.b64decode(image_data),
                                    mime_type=mime_type,
                                ),
                                types.Part.from_text(text=prompt),
                            ],
                        )
                    ],
                    config=types.GenerateContentConfig(
                        temperature=0.1,
                        response_mime_type="application/json",
                    ),
                ),
            )

            parsed = json.loads(response.text)
            # 일부 케이스에서 JSON 배열로 응답하는 경우 방어
            if isinstance(parsed, dict):
                result = parsed
            elif isinstance(parsed, list) and parsed and isinstance(parsed[0], dict):
                result = parsed[0]
            else:
                raise ValueError(f"Unexpected JSON shape: {type(parsed).__name__}")
            # 색상 정규화 (단일 값 강제)
            if result.get("category") == "color_swatch":
                result["color"] = None
            else:
                result["color"] = coerce_color(result.get("color"), expected_colors)

            # extracted 필드 방어 (없으면 null)
            if (
                "extracted" in result
                and not isinstance(result["extracted"], dict)
                and result["extracted"] is not None
            ):
                result["extracted"] = None
            result["file_path"] = str(image_path)
            result["file_name"] = image_path.name

            # 진행 상황 업데이트
            progress["done"] += 1
            color_str = result.get("color", "N/A")
            if isinstance(color_str, list):
                color_str = ", ".join(color_str)
            print(
                f"  [{progress['done']}/{progress['total']}] {image_path.name} → {result.get('category', 'error')} ({color_str})"
            )

            return result

        except Exception as e:
            progress["done"] += 1
            print(
                f"  [{progress['done']}/{progress['total']}] {image_path.name} → [!] 실패: {e}"
            )
            return {
                "file_path": str(image_path),
                "file_name": image_path.name,
                "category": "error",
                "color": None,
                "confidence": 0,
                "has_multiple_items": False,
                "quality_score": 0,
                "error": str(e),
            }


async def classify_product_images_async(product_dir: Path) -> dict:
    """상품 폴더 내 모든 이미지 분류 (병렬)"""
    meta = load_product_metadata(product_dir)
    expected_colors = parse_expected_colors(meta)
    prompt = build_prompt(meta)

    image_extensions = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    images = sorted(
        [f for f in product_dir.iterdir() if f.suffix.lower() in image_extensions]
    )

    print(
        f"\n📷 {product_dir.name}: {len(images)}개 이미지 분류 중... (동시 {MAX_CONCURRENT_REQUESTS}개)"
    )

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
    progress = {"done": 0, "total": len(images)}

    # 모든 이미지를 동시에 처리
    tasks = [
        classify_image_async(image_path, semaphore, progress, prompt, expected_colors)
        for image_path in images
    ]
    classifications = await asyncio.gather(*tasks)

    # 파일명 순서로 정렬
    classifications = sorted(classifications, key=lambda x: x["file_name"])

    return {
        "product_sno": product_dir.name,
        "total_images": len(images),
        "meta": meta,
        "classifications": classifications,
    }


def normalize_color(color) -> list[str]:
    """색상 값을 리스트로 정규화"""
    if color is None:
        return []
    if isinstance(color, list):
        return [c for c in color if isinstance(c, str)]
    if isinstance(color, str):
        return [color]
    return []


def select_best_images(
    classifications: list[dict], colors: list[str] | None = None
) -> dict:
    """분류된 이미지 중 최적 이미지 선택"""

    # 카테고리별 그룹화
    by_category = {}
    for item in classifications:
        cat = item.get("category", "other")
        if cat not in by_category:
            by_category[cat] = []
        by_category[cat].append(item)

    # 색상별 그룹화 (색상이 리스트일 수 있음)
    by_color = {}
    for item in classifications:
        colors_list = normalize_color(item.get("color"))
        # 첫 번째 색상만 사용 (주요 색상)
        if colors_list:
            color = colors_list[0]
            if color not in by_color:
                by_color[color] = []
            by_color[color].append(item)

    # 최적 이미지 선택
    selected = {
        "worn_shots_by_color": {},  # 색상별 착용샷 (정면 우선)
        "product_shots_by_color": {},  # 색상별 제품 앞면
        "representative_details": {},  # 대표 SKU 디테일
        "info_images": {},  # 상품 정보 이미지(사이즈/소재/핏/상품체크)
    }

    # 1. 색상별 착용샷 선택 (정면 우선, confidence 우선)
    worn_categories = ["worn_front", "worn_side", "worn_back"]
    for color, items in by_color.items():
        worn_items = [i for i in items if i.get("category") in worn_categories]
        if worn_items:
            # 정면 우선, confidence 높은 것, 그 다음 quality
            worn_items.sort(
                key=lambda x: (
                    x.get("category") != "worn_front",
                    -x.get("confidence", 0),
                    -x.get("quality_score", 0),
                )
            )
            selected["worn_shots_by_color"][color] = worn_items[0]

    # 2. 색상별 제품 앞면 선택 (confidence 우선)
    for color, items in by_color.items():
        product_items = [i for i in items if i.get("category") == "product_front"]
        if product_items:
            product_items.sort(
                key=lambda x: (-x.get("confidence", 0), -x.get("quality_score", 0))
            )
            selected["product_shots_by_color"][color] = product_items[0]

    # 3. 대표 SKU 디테일 (대표 SKU/대표 색상에서만 선택)
    if by_color:
        detail_categories = [
            "product_front",
            "product_back",
            "detail_neckline",
            "detail_sleeve",
            "detail_hem",
        ]

        # 대표 색상 선정:
        # - 필수 디테일(앞/뒤/넥라인/소매/밑단) "완전체" 우선
        # - 없으면 충족 카테고리 수 최다
        # - 그 다음 각 카테고리 best confidence 합으로 타이브레이크
        def color_score(c: str) -> tuple[int, int, float]:
            items = by_color.get(c, [])
            per_cat_best_conf: dict[str, float] = {}
            for cat in detail_categories:
                best = None
                best_conf = -1.0
                for it in items:
                    if it.get("category") != cat:
                        continue
                    conf = float(it.get("confidence", 0) or 0)
                    if conf > best_conf:
                        best_conf = conf
                        best = it
                if best is not None:
                    per_cat_best_conf[cat] = best_conf

            coverage = len(per_cat_best_conf)  # 충족한 카테고리 수
            is_complete = 1 if coverage == len(detail_categories) else 0
            confidence_sum = (
                sum(per_cat_best_conf.values()) if per_cat_best_conf else 0.0
            )
            return (is_complete, coverage, confidence_sum)

        representative_color = max(by_color.keys(), key=color_score)

        for cat in detail_categories:
            cat_items = [
                i
                for i in by_color.get(representative_color, [])
                if i.get("category") == cat
            ]
            if cat_items:
                cat_items.sort(
                    key=lambda x: (-x.get("confidence", 0), -x.get("quality_score", 0))
                )
                selected["representative_details"][cat] = cat_items[0]

        selected["representative_color"] = representative_color

    # 4. 상품 정보 이미지 선택 (사이즈/소재/핏/상품체크)
    # - product_info 카테고리 우선, 그 다음 size_chart
    info_candidates = [
        i
        for i in classifications
        if i.get("category") in ("product_info", "size_chart")
    ]

    # extracted는 소재/혼용률만 의미있게 사용 (그 외는 추출하지 않음)
    def get_extracted(item: dict) -> dict:
        ex = item.get("extracted")
        return ex if isinstance(ex, dict) else {}

    def best_item(items: list[dict]) -> dict | None:
        if not items:
            return None
        items = sorted(
            items, key=lambda x: (-x.get("confidence", 0), -x.get("quality_score", 0))
        )
        return items[0]

    # 사이즈/상품정보: 정규화 추출 없이, size_chart 우선으로 1장 선택
    selected_size = best_item(
        [i for i in info_candidates if i.get("category") == "size_chart"]
    ) or best_item([i for i in info_candidates if i.get("category") == "product_info"])
    if selected_size:
        selected["info_images"]["size"] = selected_size

    # 혼용률/소재
    comp_items = [
        i
        for i in info_candidates
        if get_extracted(i).get("composition") or get_extracted(i).get("material")
    ]
    selected_comp = best_item(comp_items)
    if selected_comp:
        selected["info_images"]["composition"] = selected_comp

    # 상품정보 이미지(표/텍스트) 1장 추가 선택(사이즈 이미지와 다를 수 있음)
    selected_info = best_item(
        [i for i in info_candidates if i.get("category") == "product_info"]
    )
    if selected_info and (
        not selected_size
        or selected_info.get("file_path") != selected_size.get("file_path")
    ):
        selected["info_images"]["product_info"] = selected_info

    return selected


async def process_product_async(
    product_dir: Path, output_dir: Path | None = None
) -> dict:
    """상품 이미지 처리 및 최적 이미지 선택 (비동기)"""

    # 1. 모든 이미지 분류 (병렬)
    result = await classify_product_images_async(product_dir)

    # 2. 최적 이미지 선택
    selected = select_best_images(result["classifications"])
    result["selected"] = selected

    # 2-1. 추출된 소재/혼용률을 meta.json에 병합 저장 (fallback 용)
    update_product_metadata_with_extracted_composition(product_dir, result)

    # 3. 결과 저장
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_file = output_dir / f"{product_dir.name}_classification.json"
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"  💾 저장: {output_file}")

    return result


SELECTED_DIR = Path("output/selected")


def safe_filename_part(s: str) -> str:
    # 한글/영문/숫자/언더스코어/하이픈만 남기고 나머지 치환
    s = s.strip()
    s = s.replace(" ", "_")
    s = s.replace("/", "_")
    s = s.replace(",", "_")
    s = re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "unknown"


def copy_selected_images(result: dict) -> tuple[Path, list[str]]:
    """선택된 이미지를 별도 폴더에 복사"""
    product_sno = result["product_sno"]
    product_dir = SELECTED_DIR / product_sno
    product_dir.mkdir(parents=True, exist_ok=True)

    selected = result.get("selected", {})
    copied_files = []

    # 1. 착용샷 복사 (색상별)
    for color, item in selected.get("worn_shots_by_color", {}).items():
        src = Path(item["file_path"])
        safe_color = safe_filename_part(color)
        dst = product_dir / f"worn_{safe_color}{src.suffix}"
        shutil.copy2(src, dst)
        copied_files.append(f"worn_{safe_color}{src.suffix}")

    # 2. 제품 앞면 복사 (색상별)
    for color, item in selected.get("product_shots_by_color", {}).items():
        src = Path(item["file_path"])
        safe_color = safe_filename_part(color)
        dst = product_dir / f"product_{safe_color}{src.suffix}"
        shutil.copy2(src, dst)
        copied_files.append(f"product_{safe_color}{src.suffix}")

    # 3. 대표 SKU 디테일 복사
    detail_name_map = {
        "product_front": "detail_front",
        "product_back": "detail_back",
        "detail_neckline": "detail_neckline",
        "detail_sleeve": "detail_sleeve",
        "detail_hem": "detail_hem",
    }

    for cat, item in selected.get("representative_details", {}).items():
        src = Path(item["file_path"])
        name = detail_name_map.get(cat, cat)
        dst = product_dir / f"{name}{src.suffix}"
        # 중복 방지 (product_front가 이미 복사됐을 수 있음)
        if not dst.exists():
            shutil.copy2(src, dst)
            copied_files.append(f"{name}{src.suffix}")

    # 4. 상품 정보 이미지 복사
    info_name_map = {
        "size": "info_size",
        "composition": "info_composition",
        "product_info": "info_product_info",
    }
    for key, item in selected.get("info_images", {}).items():
        if not isinstance(item, dict) or "file_path" not in item:
            continue
        src = Path(item["file_path"])
        name = info_name_map.get(key, f"info_{key}")
        dst = product_dir / f"{name}{src.suffix}"
        if not dst.exists():
            shutil.copy2(src, dst)
            copied_files.append(f"{name}{src.suffix}")

    return product_dir, copied_files


def print_summary(result: dict):
    """분류 결과 요약 출력 및 선택 이미지 복사"""
    print(f"\n{'=' * 50}")
    print(f"📊 분류 결과 요약: 상품 {result['product_sno']}")
    print(f"{'=' * 50}")

    # 카테고리별 카운트
    category_counts = {}
    for item in result["classifications"]:
        cat = item.get("category", "unknown")
        category_counts[cat] = category_counts.get(cat, 0) + 1

    print("\n📁 카테고리별 이미지 수:")
    for cat, count in sorted(category_counts.items(), key=lambda x: -x[1]):
        cat_name = IMAGE_CATEGORIES.get(cat, cat)
        print(f"  - {cat_name}: {count}개")

    # 색상별 카운트
    color_counts = {}
    for item in result["classifications"]:
        colors_list = normalize_color(item.get("color"))
        for color in colors_list:
            color_counts[color] = color_counts.get(color, 0) + 1

    if color_counts:
        print("\n🎨 색상별 이미지 수:")
        for color, count in sorted(color_counts.items(), key=lambda x: -x[1]):
            print(f"  - {color}: {count}개")

    # 선택된 이미지
    selected = result.get("selected", {})
    if selected:
        print("\n✅ 선택된 이미지:")

        if selected.get("representative_color"):
            print(f"  대표 색상: {selected['representative_color']}")

        if selected.get("worn_shots_by_color"):
            print("\n  착용샷 (색상별):")
            for color, item in selected["worn_shots_by_color"].items():
                print(f"    - {color}: {item['file_name']}")

        if selected.get("product_shots_by_color"):
            print("\n  제품 앞면 (색상별):")
            for color, item in selected["product_shots_by_color"].items():
                print(f"    - {color}: {item['file_name']}")

        if selected.get("representative_details"):
            print("\n  대표 SKU 디테일:")
            for cat, item in selected["representative_details"].items():
                cat_name = IMAGE_CATEGORIES.get(cat, cat)
                print(f"    - {cat_name}: {item['file_name']}")

    # 선택된 이미지 복사
    if result.get("selected"):
        product_dir, copied_files = copy_selected_images(result)
        print(f"\n📁 선택 이미지 복사: {product_dir}")
        for f in copied_files:
            print(f"    - {f}")


async def process_all_products_async(images_dir: Path, output_dir: Path) -> list[dict]:
    """모든 상품 이미지 처리 (비동기)"""
    results = []

    # 숫자로 된 디렉토리만 (상품 sno)
    product_dirs = [d for d in images_dir.iterdir() if d.is_dir() and d.name.isdigit()]

    print(f"\n🛍️ 총 {len(product_dirs)}개 상품 처리 시작")

    for i, product_dir in enumerate(sorted(product_dirs), 1):
        print(f"\n[{i}/{len(product_dirs)}] 상품 {product_dir.name}")
        result = await process_product_async(product_dir, output_dir)
        results.append(result)
        print_summary(result)

    # 전체 결과 저장
    summary_file = output_dir / "all_products_summary.json"
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n💾 전체 요약 저장: {summary_file}")

    return results


async def main_async():
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  단일 상품: python image_classifier.py output/images/54822073")
        print("  전체 상품: python image_classifier.py --all")
        return

    output_dir = Path("output/classifications")

    if sys.argv[1] == "--all":
        images_dir = Path("output/images")
        if not images_dir.exists():
            print(f"Error: Directory not found: {images_dir}")
            return
        await process_all_products_async(images_dir, output_dir)
    else:
        product_dir = Path(sys.argv[1])
        if not product_dir.exists():
            print(f"Error: Directory not found: {product_dir}")
            return
        result = await process_product_async(product_dir, output_dir)
        print_summary(result)


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
