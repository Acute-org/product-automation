import base64
import json
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import httpx

app = FastAPI()

ABLY_BASE_URL = "https://api.a-bly.com"

HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "ko,en-US;q=0.9,en;q=0.8,ja;q=0.7",
    "cache-control": "no-cache",
    "origin": "https://m.a-bly.com",
    "referer": "https://m.a-bly.com/",
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


@app.get("/api/products")
async def get_products(
    category_sno: int = Query(...),
    next_token: str = Query(None),
):
    if not next_token:
        next_token = create_initial_token(category_sno)

    params = {
        "next_token": next_token,
        "category_list[]": str(category_sno),
        "sorting_type": "POPULAR",
    }

    async with httpx.AsyncClient(headers=HEADERS, timeout=30) as client:
        response = await client.get(f"{ABLY_BASE_URL}/api/v2/screens/SUB_CATEGORY_DEPARTMENT/", params=params)
        return response.json()


@app.get("/api/review/{sno}")
async def get_review(sno: int):
    async with httpx.AsyncClient(headers=HEADERS, timeout=30) as client:
        response = await client.get(f"{ABLY_BASE_URL}/api/v2/goods/{sno}/review_summary/")
        return response.json()


@app.get("/", response_class=HTMLResponse)
async def index():
    with open("index.html", "r", encoding="utf-8") as f:
        return f.read()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
