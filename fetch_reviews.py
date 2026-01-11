import argparse
import json
import sys
from pathlib import Path

import httpx

from main_api import HEADERS


REVIEWS_WEBVIEW_API_URL = "https://api.a-bly.com/webview/goods/{sno}/reviews/"
DEFAULT_OUTPUT_DIR = Path("output") / "reviews"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ably 상품 리뷰 가져오기")
    p.add_argument("sno", type=int, help="상품 id(goods_sno)")
    p.add_argument(
        "--out",
        type=str,
        default=None,
        help="저장할 JSON 경로(기본: output/reviews/<sno>_reviews.json)",
    )
    p.add_argument(
        "--pages",
        type=int,
        default=1,
        help="가져올 페이지 수(기본 1). API가 페이지네이션을 지원하면 자동으로 시도합니다.",
    )
    p.add_argument(
        "--max-reviews",
        type=int,
        default=0,
        help="최대 리뷰 개수 제한(0이면 제한 없음)",
    )
    p.add_argument(
        "--no-autopaginate",
        action="store_true",
        help="자동 페이지네이션(다음 페이지 탐색) 시도를 하지 않고 1회 호출만 수행",
    )
    p.add_argument(
        "--stdout",
        action="store_true",
        help="파일 저장 대신 stdout으로 JSON 출력",
    )
    p.add_argument(
        "--pretty",
        action="store_true",
        help="pretty JSON으로 출력/저장",
    )
    return p.parse_args(argv)


def _get_reviews_list(payload: dict) -> list[dict]:
    reviews = payload.get("reviews")
    if isinstance(reviews, list):
        return [r for r in reviews if isinstance(r, dict)]
    return []


def _make_client() -> httpx.Client:
    # main_api.py와 동일한 HEADERS 재사용(anonymous token 포함)
    return httpx.Client(headers=HEADERS, timeout=30.0)


def _fetch_reviews_payload(
    client: httpx.Client, sno: int, params: dict | None = None
) -> dict:
    resp = client.get(REVIEWS_WEBVIEW_API_URL.format(sno=sno), params=params or {})
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("Unexpected response shape (expected JSON object)")
    return data


def _detect_pagination_param(
    client: httpx.Client, sno: int, first_payload: dict
) -> tuple[str, str | int] | None:
    """
    Ably 리뷰 API가 어떤 파라미터로 다음 페이지를 넘기는지 환경에 따라 다를 수 있어서
    몇 가지 후보를 1회씩 시도해보고, "새로운 리뷰"가 나오면 그 파라미터를 채택합니다.
    """
    first = _get_reviews_list(first_payload)
    if not first:
        return None

    first_snos = [r.get("sno") for r in first]
    first_snos_set = {s for s in first_snos if isinstance(s, int)}

    last_sno = None
    if isinstance(first[-1].get("sno"), int):
        last_sno = int(first[-1]["sno"])

    page_size = len(first)

    candidates: list[tuple[str, str | int]] = []
    # page 기반
    candidates.append(("page", 2))
    # offset 기반
    candidates.append(("offset", page_size))
    candidates.append(("start", page_size))
    candidates.append(("from", page_size))
    # cursor / last_sno 기반 (가장 흔한 패턴들)
    if last_sno is not None:
        candidates.extend(
            [
                ("cursor", last_sno),
                ("last_sno", last_sno),
                ("lastReviewSno", last_sno),
                ("last_review_sno", last_sno),
                ("review_sno", last_sno),
            ]
        )

    for key, value in candidates:
        try:
            payload = _fetch_reviews_payload(client, sno, params={key: value})
            reviews = _get_reviews_list(payload)
            if not reviews:
                continue
            snos = {r.get("sno") for r in reviews if isinstance(r.get("sno"), int)}
            # 다음 페이지면 최소 1개는 first page와 달라야 함
            if snos and (snos - first_snos_set):
                return (key, value)
        except Exception:
            # 후보 파라미터가 틀려도 조용히 다음 후보로 넘어감
            continue

    return None


def fetch_reviews(
    sno: int,
    pages: int = 1,
    max_reviews: int = 0,
    no_autopaginate: bool = False,
) -> dict:
    if pages < 1:
        raise ValueError("--pages must be >= 1")
    if max_reviews < 0:
        raise ValueError("--max-reviews must be >= 0")

    with _make_client() as client:
        first_payload = _fetch_reviews_payload(client, sno)
        first_reviews = _get_reviews_list(first_payload)

        merged: list[dict] = []
        seen: set[int] = set()

        def add_reviews(items: list[dict]) -> None:
            nonlocal merged, seen
            for r in items:
                rsno = r.get("sno")
                if isinstance(rsno, int) and rsno in seen:
                    continue
                if isinstance(rsno, int):
                    seen.add(rsno)
                merged.append(r)

        add_reviews(first_reviews)

        if max_reviews and len(merged) >= max_reviews:
            merged = merged[:max_reviews]
            return {**first_payload, "reviews": merged}

        if pages == 1 or no_autopaginate:
            return {**first_payload, "reviews": merged}

        pagination = _detect_pagination_param(client, sno, first_payload)
        if pagination is None:
            # 환경/응답에 따라 페이지네이션을 못 찾을 수 있어요. 그 경우 1페이지만 반환.
            return {**first_payload, "reviews": merged}

        key, value = pagination
        current_page = 1
        current_offset = len(first_reviews)
        last_sno = None
        if first_reviews and isinstance(first_reviews[-1].get("sno"), int):
            last_sno = int(first_reviews[-1]["sno"])

        while current_page < pages:
            params: dict[str, str | int] = {}
            if key == "page":
                params[key] = current_page + 1
            elif key in ("offset", "start", "from"):
                params[key] = current_offset
            else:
                # cursor/last_sno 계열
                if last_sno is None:
                    break
                params[key] = last_sno

            payload = _fetch_reviews_payload(client, sno, params=params)
            reviews = _get_reviews_list(payload)
            if not reviews:
                break

            before = len(merged)
            add_reviews(reviews)
            if len(merged) == before:
                # 더 이상 새 리뷰가 안오면 중단
                break

            current_page += 1
            current_offset = len(merged)
            if isinstance(reviews[-1].get("sno"), int):
                last_sno = int(reviews[-1]["sno"])

            if max_reviews and len(merged) >= max_reviews:
                merged = merged[:max_reviews]
                break

        return {**first_payload, "reviews": merged}


def main(argv: list[str]) -> int:
    args = _parse_args(argv)

    out_path: Path | None
    if args.stdout:
        out_path = None
    else:
        if args.out:
            out_path = Path(args.out)
        else:
            DEFAULT_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            out_path = DEFAULT_OUTPUT_DIR / f"{args.sno}_reviews.json"

    try:
        payload = fetch_reviews(
            sno=int(args.sno),
            pages=int(args.pages),
            max_reviews=int(args.max_reviews),
            no_autopaginate=bool(args.no_autopaginate),
        )
    except httpx.HTTPStatusError as e:
        print(
            f"[!] HTTP 오류: {e.response.status_code} {e.request.url}", file=sys.stderr
        )
        try:
            print(e.response.text, file=sys.stderr)
        except Exception:
            pass
        return 2
    except Exception as e:
        print(f"[!] 실패: {e}", file=sys.stderr)
        return 1

    dump_kwargs = {"ensure_ascii": False}
    if args.pretty:
        dump_kwargs |= {"indent": 2}

    if out_path is None:
        print(json.dumps(payload, **dump_kwargs))
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, **dump_kwargs), encoding="utf-8")
    print(f"[ok] saved: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
