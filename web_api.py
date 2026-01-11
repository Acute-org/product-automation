import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from ably_crawler import CATEGORIES, CrawlConfig, crawl_ably_products
from db import (
    connect,
    create_job as db_create_job,
    get_job as db_get_job,
    get_job_products as db_get_job_products,
    get_products_count as db_get_products_count,
    get_known_snos as db_get_known_snos,
    init_db,
    list_jobs as db_list_jobs,
    list_products as db_list_products,
    update_job_status as db_update_job_status,
    upsert_products_for_job as db_upsert_products_for_job,
)

load_dotenv()


class CrawlRequest(BaseModel):
    all: bool = Field(default=False, description="모든 상위/하위 카테고리를 수집")
    category: str | None = Field(default=None, description="상위 카테고리 (예: 아우터)")
    subcategory: str | None = Field(
        default=None, description="하위 카테고리 (예: 자켓). category와 함께 사용"
    )

    min_purchase_count: int = 2000
    min_review_count: int = 100
    min_positive_percent: int = 95
    max_products_per_target: int = 10

    # 이미지 저장은 하지 않음: 링크만 저장/반환
    include_cover_image_urls: bool = True
    include_detail_image_urls: bool = True

    dedupe_against_history: bool = True


JobStatus = Literal["queued", "running", "succeeded", "failed"]


APP_NAME = os.getenv("APP_NAME", "crawler-manual-api")
BASE_OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "output")).resolve()
JOBS_DIR = BASE_OUTPUT_DIR / "jobs"
DB_PATH = os.getenv("DB_PATH", str((BASE_OUTPUT_DIR / "data.db").resolve()))

MAX_WORKERS = int(os.getenv("MAX_WORKERS", "2"))
EXECUTOR = ThreadPoolExecutor(max_workers=MAX_WORKERS)

app = FastAPI(title=APP_NAME)

# 사내툴: 프론트가 다른 origin(다른 포트/도메인, file:// 등)에서 호출할 수 있어 CORS 허용
# 필요하면 CORS_ALLOW_ORIGINS="https://example.com,https://foo.bar" 형태로 제한 가능
_cors_origins_raw = os.getenv("CORS_ALLOW_ORIGINS", "*").strip()
_cors_allow_origins = (
    ["*"]
    if _cors_origins_raw in ("*", "")
    else [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(DB_PATH)
    try:
        init_db(conn)
    finally:
        conn.close()


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/categories")
def get_categories() -> dict[str, Any]:
    return {"categories": CATEGORIES}


@app.get("/", response_class=HTMLResponse)
def index_page() -> HTMLResponse:
    html = (Path(__file__).parent / "templates" / "index.html").read_text(
        encoding="utf-8"
    )
    return HTMLResponse(content=html)


@app.get("/history", response_class=HTMLResponse)
def history_page() -> HTMLResponse:
    html = (Path(__file__).parent / "templates" / "history.html").read_text(
        encoding="utf-8"
    )
    return HTMLResponse(content=html)


def _run_job(job_id: str, req: CrawlRequest) -> None:
    conn = connect(DB_PATH)
    db_update_job_status(conn, job_id=job_id, status="running")

    try:
        if req.subcategory and not req.category:
            raise ValueError("subcategory는 category와 함께 사용해야 합니다.")

        config = CrawlConfig(
            min_purchase_count=req.min_purchase_count,
            min_review_count=req.min_review_count,
            min_positive_percent=req.min_positive_percent,
            max_products_per_target=req.max_products_per_target,
        )

        exclude_snos: set[int] | None = None
        if req.dedupe_against_history:
            exclude_snos = db_get_known_snos(conn)

        result = crawl_ably_products(
            all_categories=req.all,
            category=req.category,
            subcategory=req.subcategory,
            config=config,
            output_dir=BASE_OUTPUT_DIR,
            include_cover_image_urls=req.include_cover_image_urls,
            include_detail_image_urls=req.include_detail_image_urls,
            exclude_snos=exclude_snos,
            write_meta_file=False,
            write_products_json=False,
        )
        products = result.get("products") if isinstance(result, dict) else None
        if isinstance(products, list):
            db_upsert_products_for_job(conn, job_id=job_id, products=products)
        db_update_job_status(conn, job_id=job_id, status="succeeded")
    except Exception as e:
        db_update_job_status(conn, job_id=job_id, status="failed", error=str(e))
    finally:
        conn.close()


@app.post("/v1/jobs")
def create_job(req: CrawlRequest) -> dict[str, Any]:
    job_id = uuid4().hex
    conn = connect(DB_PATH)
    try:
        db_create_job(conn, job_id=job_id, request=req.model_dump())
    finally:
        conn.close()

    EXECUTOR.submit(_run_job, job_id, req)
    return {
        "job_id": job_id,
        "status_url": f"/v1/jobs/{job_id}",
        "result_url": f"/v1/jobs/{job_id}/result",
        "products_url": f"/v1/jobs/{job_id}/products",
    }


@app.get("/v1/jobs")
def list_jobs(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    conn = connect(DB_PATH)
    try:
        rows = db_list_jobs(conn, limit=limit, offset=offset)
    finally:
        conn.close()
    return {
        "jobs": [
            {
                "job_id": r.job_id,
                "status": r.status,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
                "request": r.request_json,
                "error": r.error,
            }
            for r in rows
        ]
    }


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    conn = connect(DB_PATH)
    try:
        job = db_get_job(conn, job_id=job_id)
    finally:
        conn.close()
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return {
        "job_id": job.job_id,
        "status": job.status,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "request": job.request_json,
        "error": job.error,
        "products_url": f"/v1/jobs/{job_id}/products",
        "result_url": f"/v1/jobs/{job_id}/result",
    }


@app.get("/v1/jobs/{job_id}/result")
def get_job_result(job_id: str) -> JSONResponse:
    conn = connect(DB_PATH)
    try:
        job = db_get_job(conn, job_id=job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        if job.status == "failed":
            raise HTTPException(status_code=500, detail=job.error or "job failed")
        if job.status != "succeeded":
            raise HTTPException(status_code=409, detail="job not completed")
        products = db_get_job_products(conn, job_id=job_id)
    finally:
        conn.close()

    return JSONResponse(
        content={
            "job_id": job_id,
            "count": len(products),
            "products": products,
        }
    )


@app.get("/v1/jobs/{job_id}/products")
def get_job_products(job_id: str) -> JSONResponse:
    conn = connect(DB_PATH)
    try:
        job = db_get_job(conn, job_id=job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job not found")
        if job.status == "failed":
            raise HTTPException(status_code=500, detail=job.error or "job failed")
        if job.status != "succeeded":
            raise HTTPException(status_code=409, detail="job not completed")
        products = db_get_job_products(conn, job_id=job_id)
    finally:
        conn.close()
    return JSONResponse(
        content={"job_id": job_id, "count": len(products), "products": products}
    )


@app.get("/v1/products")
def list_products(limit: int = 50, offset: int = 0) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    conn = connect(DB_PATH)
    try:
        total = db_get_products_count(conn)
        products = db_list_products(conn, limit=limit, offset=offset)
    finally:
        conn.close()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "count": len(products),
        "products": products,
    }
