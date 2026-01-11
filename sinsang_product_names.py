import argparse
import csv
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable
from urllib.parse import parse_qs, urlparse

from dotenv import load_dotenv

if TYPE_CHECKING:
    import httpx


DETAIL_API_URL = "https://abara.sinsang.market/api/v1/goods/{gid}/detail"

DEFAULT_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "ko,en-US;q=0.9,en;q=0.8",
    "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
    "referer": "https://sinsangmarket.kr/",
    "origin": "https://sinsangmarket.kr",
}


URL_RE = re.compile(r"https?://\S+")
GOODS_PATH_RE = re.compile(r"/goods/(?P<gid>\d+)(?:/|$)")


@dataclass(frozen=True)
class RowResult:
    row_index: int
    raw: str
    url: str | None
    gid: int | None
    name: str | None
    error: str | None


def _extract_first_url(raw_line: str) -> str | None:
    m = URL_RE.search(raw_line.strip())
    if not m:
        return None
    # Excel에서 복사 시 탭/스페이스 뒤에 붙는 쓰레기 문자를 정리
    return m.group(0).strip().strip('"').strip("'").rstrip("),")


def extract_gid_from_url(url: str) -> int | None:
    """
    지원하는 URL 형태:
    - https://sinsangmarket.kr/sinsangLens?modalGid=99334929
    - https://sinsangmarket.kr/search?...&modalGid=98920960
    - https://sinsangmarket.kr/goods/98920960/0
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return None

    qs = parse_qs(parsed.query)
    modal = qs.get("modalGid", [])
    if modal:
        try:
            return int(str(modal[0]))
        except Exception:
            return None

    m = GOODS_PATH_RE.search(parsed.path or "")
    if m:
        try:
            return int(m.group("gid"))
        except Exception:
            return None

    return None


def fetch_goods_name(client: "httpx.Client", gid: int, *, max_retries: int = 4) -> str:
    url = DETAIL_API_URL.format(gid=gid)
    last_err: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            resp = client.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            content = data.get("content") if isinstance(data, dict) else None
            name = content.get("name") if isinstance(content, dict) else None
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Response JSON missing content.name")
            return name.strip()
        except Exception as e:
            last_err = e
            # 간단한 재시도: 429/5xx/네트워크 이슈를 완전히 구분하진 않되, 너무 공격적이지 않게 backoff
            if attempt >= max_retries:
                break
            time.sleep(0.4 * (2**attempt))

    raise RuntimeError(f"Failed to fetch gid={gid}: {last_err}")


async def async_fetch_goods_name(
    client: "httpx.AsyncClient", gid: int, *, max_retries: int = 4
) -> str:
    url = DETAIL_API_URL.format(gid=gid)
    last_err: Exception | None = None

    for attempt in range(max_retries + 1):
        try:
            resp = await client.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            content = data.get("content") if isinstance(data, dict) else None
            name = content.get("name") if isinstance(content, dict) else None
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Response JSON missing content.name")
            return name.strip()
        except Exception as e:
            last_err = e
            if attempt >= max_retries:
                break
            # same backoff strategy as sync
            import asyncio

            await asyncio.sleep(0.4 * (2**attempt))

    raise RuntimeError(f"Failed to fetch gid={gid}: {last_err}")


def iter_input_lines(path: str | None, *, interactive: bool) -> Iterable[str]:
    if path:
        with open(path, "r", encoding="utf-8") as f:
            yield from f
        return

    # stdin은 문제가 잦아서 기본 비활성화: 필요한 경우 --interactive를 사용
    if not interactive:
        raise SystemExit(
            "입력 파일이 필요합니다. 예: poetry run python sinsang_product_names.py sinsang-urls.txt"
        )

    # interactive (tui)
    print(
        "URL들을 한 줄에 하나씩 입력/붙여넣기 하세요. (빈 줄 입력 시 종료)\n"
        "- 예: https://sinsangmarket.kr/sinsangLens?modalGid=99334929\n"
        "- 예: https://sinsangmarket.kr/goods/98920960/0\n"
    )
    while True:
        try:
            line = input("> ")
        except EOFError:
            break
        if not line.strip():
            break
        yield line + "\n"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="신상마켓 상품 URL 목록에서 gid를 추출해 상품명을 TSV/CSV로 저장합니다."
    )
    p.add_argument(
        "in_path_pos",
        nargs="?",
        default=None,
        help="입력 파일 경로(예: sinsang-urls.txt). 미지정 시 stdin/인터랙티브 입력",
    )
    p.add_argument(
        "--in",
        dest="in_path",
        type=str,
        default=None,
        help="입력 파일 경로(미지정 시 stdin/인터랙티브 입력). positional 인자와 동일",
    )
    p.add_argument(
        "--out",
        dest="out_path",
        type=str,
        default="sinsang_product_names.tsv",
        help="출력 파일 경로 (기본 sinsang_product_names.tsv)",
    )
    p.add_argument(
        "--format",
        choices=["tsv", "csv"],
        default="tsv",
        help="출력 포맷 (기본 tsv)",
    )
    p.add_argument(
        "--interactive",
        action="store_true",
        help="파일 없이 터미널에서 URL을 직접 입력(빈 줄 입력 시 종료).",
    )
    p.add_argument(
        "--names-only",
        action="store_true",
        help="엑셀 붙여넣기용: 상품명만 1열로 출력(헤더 없음). 실패 시 빈 줄.",
    )
    p.add_argument(
        "--access-token",
        dest="access_token",
        type=str,
        default=None,
        help="신상마켓 API access-token 헤더 값 (미지정 시 env SINSANGMARKET_ACCESS_TOKEN 사용)",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=20,
        help="동시 요청 수 (기본 20). 400개 이상 요청 시 속도 개선용.",
    )
    p.add_argument(
        "--max-retries",
        type=int,
        default=4,
        help="요청 실패 시 재시도 횟수 (기본 4).",
    )
    return p.parse_args(argv)


def main() -> None:
    load_dotenv()
    args = _parse_args(sys.argv[1:])

    in_path = args.in_path or args.in_path_pos
    raw_lines = list(iter_input_lines(in_path, interactive=bool(args.interactive)))
    results: list[RowResult] = []

    import httpx

    access_token = args.access_token or os.getenv("SINSANGMARKET_ACCESS_TOKEN")
    if not access_token:
        raise SystemExit(
            "access-token이 필요합니다. env SINSANGMARKET_ACCESS_TOKEN을 설정하거나 "
            "--access-token 옵션을 사용하세요."
        )

    headers = dict(DEFAULT_HEADERS)
    headers["access-token"] = access_token

    # 1) Parse/validate lines first (cheap) and create async jobs only for rows with gid
    parsed: list[RowResult] = []
    jobs: list[tuple[int, int, str, int]] = []  # (row_index, list_index, raw, gid)

    for row_index, raw in enumerate(raw_lines, start=1):
        raw = raw.rstrip("\n")
        stripped = raw.strip()

        if not stripped:
            parsed.append(
                RowResult(
                    row_index=row_index,
                    raw=raw,
                    url=None,
                    gid=None,
                    name=None,
                    error="empty_line",
                )
            )
            continue

        # Excel/Sheets에서 빈 값이 #N/A로 들어온 경우: 스킵하되 row는 남김
        if stripped == "#N/A":
            parsed.append(
                RowResult(
                    row_index=row_index,
                    raw=raw,
                    url="none",
                    gid=None,
                    name=None,
                    error="na_url",
                )
            )
            continue

        url = _extract_first_url(raw)
        if not url:
            parsed.append(
                RowResult(
                    row_index=row_index,
                    raw=raw,
                    url=None,
                    gid=None,
                    name=None,
                    error="no_url_found",
                )
            )
            continue

        gid = extract_gid_from_url(url)
        if not gid:
            parsed.append(
                RowResult(
                    row_index=row_index,
                    raw=raw,
                    url=url,
                    gid=None,
                    name=None,
                    error="cannot_extract_gid",
                )
            )
            continue

        parsed.append(
            RowResult(
                row_index=row_index,
                raw=raw,
                url=url,
                gid=gid,
                name=None,
                error=None,
            )
        )
        jobs.append((row_index, len(parsed) - 1, raw, gid))

    # 2) Run API calls concurrently (async) and fill parsed results in-place
    import asyncio

    async def _run_jobs() -> None:
        sem = asyncio.Semaphore(max(1, int(args.concurrency)))

        async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:

            async def _one(job: tuple[int, int, str, int]) -> None:
                row_index, list_index, raw0, gid0 = job
                async with sem:
                    try:
                        name = await async_fetch_goods_name(
                            client, gid0, max_retries=int(args.max_retries)
                        )
                        parsed[list_index] = RowResult(
                            row_index=row_index,
                            raw=raw0,
                            url=parsed[list_index].url,
                            gid=gid0,
                            name=name,
                            error=None,
                        )
                    except Exception as e:
                        parsed[list_index] = RowResult(
                            row_index=row_index,
                            raw=raw0,
                            url=parsed[list_index].url,
                            gid=gid0,
                            name=None,
                            error=str(e),
                        )

            await asyncio.gather(*(_one(j) for j in jobs))

    if jobs:
        asyncio.run(_run_jobs())

    results = parsed

    # write output
    delim = "\t" if args.format == "tsv" else ","

    with open(args.out_path, "w", encoding="utf-8", newline="") as f:
        if args.names_only:
            for r in results:
                f.write((r.name or "") + "\n")
            return

        writer = csv.writer(f, delimiter=delim)
        writer.writerow(["row", "gid", "name", "url", "error", "raw"])
        for r in results:
            writer.writerow(
                [
                    r.row_index,
                    r.gid if r.gid is not None else "",
                    r.name if r.name is not None else "",
                    r.url if r.url is not None else "",
                    r.error if r.error is not None else "",
                    r.raw,
                ]
            )


if __name__ == "__main__":
    main()
