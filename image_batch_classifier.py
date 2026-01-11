"""
PoC: 상품 폴더의 모든 이미지를 한 번에 Gemini로 보내서
1) 이미지별 분류
2) 대표 색상/디테일/정보이미지 선택(select)
까지 전부 모델에게 맡기는 실험용 스크립트

Usage:
  poetry run python image_batch_classifier.py output/images/31106295
  poetry run python image_batch_classifier.py output/images/31106295 --max-images 60

Env:
  GOOGLE_API_KEY 또는 GEMINI_API_KEY
  (옵션) GEMINI_MODEL
"""

import argparse
import io
import json
import os
import re
import shutil
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

try:
    from PIL import Image
except Exception:  # pragma: no cover
    Image = None  # type: ignore


load_dotenv()

API_KEY = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    raise ValueError("GOOGLE_API_KEY 또는 GEMINI_API_KEY 환경변수를 설정하세요")

MODEL_ID = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
client = genai.Client(api_key=API_KEY)


OUTPUT_CLASSIFICATIONS_DIR = Path("output/classifications_batch")
OUTPUT_SELECTED_DIR = Path("output/selected_batch")


def safe_filename_part(s: str) -> str:
    s = (s or "").strip()
    s = s.replace(" ", "_").replace("/", "_").replace(",", "_")
    s = re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "unknown"


def get_mime_type(path: Path) -> str:
    suf = path.suffix.lower()
    if suf in [".jpg", ".jpeg"]:
        return "image/jpeg"
    if suf == ".png":
        return "image/png"
    if suf == ".webp":
        return "image/webp"
    if suf == ".gif":
        return "image/gif"
    return "image/jpeg"


def load_meta(product_dir: Path) -> dict:
    meta_path = product_dir / "meta.json"
    if not meta_path.exists():
        return {}
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def list_images(product_dir: Path) -> list[Path]:
    exts = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
    return sorted(
        [p for p in product_dir.iterdir() if p.is_file() and p.suffix.lower() in exts]
    )


def maybe_downscale_to_jpeg_bytes(
    path: Path, max_side: int = 1024, quality: int = 85
) -> tuple[bytes, str]:
    """
    전송 크기/토큰 절감을 위해 (가능하면) JPEG로 리사이즈 후 바이트 반환.
    Pillow가 없거나 실패하면 원본 바이트를 반환.
    """
    raw = path.read_bytes()
    if Image is None:
        return raw, get_mime_type(path)

    try:
        with Image.open(io.BytesIO(raw)) as img:
            img = img.convert("RGB")
            w, h = img.size
            scale = min(1.0, max_side / max(w, h))
            if scale < 1.0:
                img = img.resize((int(w * scale), int(h * scale)))
            out = io.BytesIO()
            img.save(out, format="JPEG", quality=quality, optimize=True)
            return out.getvalue(), "image/jpeg"
    except Exception:
        return raw, get_mime_type(path)


def build_batch_prompt(meta: dict, image_names: list[str]) -> str:
    title = meta.get("name")
    category = meta.get("category")
    market = meta.get("market_name")
    option_colors = meta.get("option_colors") or []
    price_info = meta.get("price_info") or {}
    fabric = meta.get("fabric")
    country = meta.get("country")

    lines = []
    if title:
        lines.append(f"- 상품명: {title}")
    if category:
        lines.append(f"- 카테고리: {category}")
    if market:
        lines.append(f"- 판매처: {market}")
    if option_colors:
        lines.append(f"- 옵션 색상: {', '.join(option_colors)}")
    if price_info:
        lines.append(f"- 가격: {json.dumps(price_info, ensure_ascii=False)}")
    if fabric:
        lines.append(f"- 공시 소재/혼용률: {fabric}")
    if country:
        lines.append(f"- 제조국: {country}")

    meta_block = "\n".join(lines) if lines else "- (메타데이터 없음)"

    # 이미지 이름 목록을 주어, 반드시 파일명 단위로 결과를 반환하도록 강제
    names_block = "\n".join([f"- {n}" for n in image_names])

    return f"""
너는 의류 상품 이미지 분류 + 최종 선택(select) 전문가다.
아래는 "동일 상품"의 이미지 묶음이다. 이미지 간 맥락을 활용해 더 일관되게 판단하라.

타겟 상품 정보:
{meta_block}

입력 이미지 파일명 목록(반드시 이 파일명으로만 결과를 매핑):
{names_block}

분류 카테고리 정의:
- worn_front / worn_side / worn_back
- product_front / product_back
- detail_neckline / detail_sleeve / detail_hem / detail_material / detail_button
- color_swatch (여러 색상 라인업/비교 이미지. 세로로 여러 색상 제품 나열도 포함)
- size_chart (사이즈 표/치수 중심)
- product_info (상품 체크표/혼용률/소재/핏/세탁/제조국 등 정보성 이미지. size_chart보다 넓은 개념)
- marketing / other

규칙:
- 여러 컬러 제품이 한 장에 나열/비교되어 보이면 무조건 color_swatch, 그리고 color=null.
- color는 타겟 상품의 색상 1개(옵션 색상 중 하나)만. 확실치 않으면 null.

너의 출력은 JSON 하나만. 스키마는 아래와 같다(키 이름 정확히):
{{
  "per_image": {{
    "<file_name>": {{
      "category": "...",
      "color": "..." | null,
      "confidence": 0.0-1.0,
      "has_multiple_items": true|false,
      "quality_score": 0.0-1.0
    }},
    ...
  }},
  "selected": {{
    "representative_color": "<color>" | null,
    "worn_by_color": {{ "<color>": "<file_name>" }},
    "product_front_by_color": {{ "<color>": "<file_name>" }},
    "representative_details": {{
      "product_front": "<file_name>"|null,
      "product_back": "<file_name>"|null,
      "detail_neckline": "<file_name>"|null,
      "detail_sleeve": "<file_name>"|null,
      "detail_hem": "<file_name>"|null
    }},
    "info_images": {{
      "size": "<file_name>"|null,
      "product_info": "<file_name>"|null,
      "composition": "<file_name>"|null
    }},
    "extracted_composition": {{
      "composition": "..."|null,
      "material": "..."|null
    }}
  }}
}}

선택 로직 가이드:
- representative_color는 (product_front/product_back/detail_neckline/detail_sleeve/detail_hem) 5개가 가장 잘 갖춰진 색상 우선.
- worn_by_color: 색상별로 1장씩(정면 우선).
- product_front_by_color: 색상별로 1장씩.
- info_images.size: size_chart 또는 product_info 중에서 사이즈 표가 가장 명확한 1장.
- info_images.composition: 혼용률이 적힌 이미지 1장(없으면 null). 혼용률 텍스트는 extracted_composition에 같이 적어라.

중요: 모든 file_name 값은 위 목록 중 하나여야 한다. 없으면 null.
"""


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def copy_selected_files(product_dir: Path, out_dir: Path, selected: dict) -> list[str]:
    ensure_dir(out_dir)
    copied: list[str] = []

    def cp(src_name: str | None, dst_name: str) -> None:
        if not src_name:
            return
        src = product_dir / src_name
        if not src.exists():
            return
        dst = out_dir / dst_name
        if dst.exists():
            return
        shutil.copy2(src, dst)
        copied.append(dst.name)

    # worn_by_color
    for color, fname in (selected.get("worn_by_color") or {}).items():
        if not fname:
            continue
        cp(fname, f"worn_{safe_filename_part(color)}{Path(fname).suffix}")

    # product_front_by_color
    for color, fname in (selected.get("product_front_by_color") or {}).items():
        if not fname:
            continue
        cp(fname, f"product_{safe_filename_part(color)}{Path(fname).suffix}")

    # representative_details
    details = selected.get("representative_details") or {}
    detail_map = {
        "product_front": "detail_front",
        "product_back": "detail_back",
        "detail_neckline": "detail_neckline",
        "detail_sleeve": "detail_sleeve",
        "detail_hem": "detail_hem",
    }
    for k, out_stem in detail_map.items():
        fname = details.get(k)
        if fname:
            cp(fname, f"{out_stem}{Path(fname).suffix}")

    # info_images
    info = selected.get("info_images") or {}
    info_map = {
        "size": "info_size",
        "product_info": "info_product_info",
        "composition": "info_composition",
    }
    for k, out_stem in info_map.items():
        fname = info.get(k)
        if fname:
            cp(fname, f"{out_stem}{Path(fname).suffix}")

    return copied


def update_meta_with_extracted(product_dir: Path, selected: dict) -> None:
    meta_path = product_dir / "meta.json"
    if not meta_path.exists():
        return
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if not isinstance(meta, dict):
            return
    except Exception:
        return

    ex = (
        (selected.get("extracted_composition") or {})
        if isinstance(selected, dict)
        else {}
    )
    if isinstance(ex, dict):
        meta["batch_extracted_composition"] = ex.get("composition")
        meta["batch_extracted_material"] = ex.get("material")
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("product_dir", type=str)
    parser.add_argument("--max-images", type=int, default=0, help="0이면 전부 전송")
    parser.add_argument("--max-side", type=int, default=1024)
    args = parser.parse_args()

    product_dir = Path(args.product_dir)
    if not product_dir.exists() or not product_dir.is_dir():
        raise SystemExit(f"Not a directory: {product_dir}")

    meta = load_meta(product_dir)
    images = list_images(product_dir)
    if args.max_images and args.max_images > 0:
        images = images[: args.max_images]

    if not images:
        raise SystemExit("No images found")

    image_names = [p.name for p in images]
    prompt = build_batch_prompt(meta, image_names)

    parts: list[types.Part] = []
    # 이미지들을 먼저 넣고 마지막에 텍스트 프롬프트
    for p in images:
        data, mime = maybe_downscale_to_jpeg_bytes(p, max_side=args.max_side)
        parts.append(types.Part.from_bytes(data=data, mime_type=mime))
    parts.append(types.Part.from_text(text=prompt))

    resp = client.models.generate_content(
        model=MODEL_ID,
        contents=[types.Content(role="user", parts=parts)],
        config=types.GenerateContentConfig(
            temperature=0.1,
            response_mime_type="application/json",
        ),
    )

    out = json.loads(resp.text)
    if not isinstance(out, dict):
        raise ValueError("Model output is not a JSON object")

    # 저장
    sno = meta.get("sno") or product_dir.name
    ensure_dir(OUTPUT_CLASSIFICATIONS_DIR)
    out_path = OUTPUT_CLASSIFICATIONS_DIR / f"{sno}_batch.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"💾 saved: {out_path}")

    # 선택 파일 복사 + meta 병합
    selected = out.get("selected") if isinstance(out, dict) else None
    if isinstance(selected, dict):
        out_sel_dir = OUTPUT_SELECTED_DIR / str(sno)
        copied = copy_selected_files(product_dir, out_sel_dir, selected)
        print(f"📁 copied: {out_sel_dir} ({len(copied)} files)")
        update_meta_with_extracted(product_dir, selected)
    else:
        print("⚠️ no selected in model output")


if __name__ == "__main__":
    main()
