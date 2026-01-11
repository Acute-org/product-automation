# crawler-manual

Ably(에이블리) 카테고리에서 **인기 상품을 필터링해 수집**하고, 상세페이지 이미지를 다운로드한 뒤 **Gemini Vision으로 이미지 분류/선별**하는 스크립트 모음입니다.

## 주요 기능

- **상품 수집 (`main_api.py`)**

  - 카테고리별 인기 상품을 조회한 뒤 조건(구매수/리뷰수/긍정률)으로 필터링
  - 상품 메타데이터 저장(`output/images/<sno>/meta.json`)
  - 상세 이미지 + 대표(커버) 이미지 다운로드
  - 세로로 긴 이미지는 자동으로 **분할 저장**
    - 분할 조각은 `*_01`, `*_02`… 형태로 생성
    - **분할 전 원본은 `{stem}_original{ext}`로 백업 저장** (예: `001_original.jpg`)

- **이미지 분류 + 대표 이미지 선택 (`image_classifier.py`)**

  - 상품 폴더(`output/images/<sno>/`)의 이미지를 Gemini로 분류
  - 색상/착용샷/제품샷/디테일/정보이미지 등을 선별해 `output/images/<sno>/selected/`로 복사
  - 분류 결과 JSON 저장(`output/images/<sno>/classification.json`)

- **배치 분류(PoC) (`image_batch_classifier.py`)**

  - 한 상품 폴더의 이미지를 “한 번에” 모델에 보내 분류+선별까지 모델에게 맡기는 실험용
  - **추천**: (가능하면) 이 방식을 기본으로 사용하세요. 이미지마다 호출하는 방식보다 **API 호출 수가 적고(=비용/속도 유리)**, 한 묶음 맥락을 활용해 **색상/디테일 선택이 더 일관적**인 편입니다.
  - 결과 JSON 저장(`output/classifications_batch/`) + 선택 이미지 복사(`output/images/<sno>/selected/`)

- **이미지 분할 유틸 (`image_splitter.py`)**
  - 이미지 1장 또는 디렉토리 전체를 분할해 `<stem>_split/`에 저장(원본도 함께 복사)

## 요구 사항

- Python **3.13+**
- [Poetry](https://python-poetry.org/)
- Gemini API Key (아래 중 하나)
  - `GOOGLE_API_KEY` 또는 `GEMINI_API_KEY`
  - (옵션) `GEMINI_MODEL` (기본값: `gemini-2.5-flash`)

프로젝트는 `.env` 로드(`dotenv`)를 사용하므로 루트에 `.env`를 만들어도 됩니다.

## 설치

```bash
poetry install
```

## 사용법

## 웹 API 서버 (FastAPI)

`main_api.py`와 유사한 수집 기능을 **웹 API**로 제공합니다.
이미지 **splitting/다운로드는 포함하지 않습니다**. **이미지 URL만 저장**합니다.
또한 잡/상품 히스토리를 **SQLite(DB_PATH)** 로 영속하고, 기본값으로 **과거에 이미 수집한 상품(sno)은 새 잡에서 자동 제외**합니다.

### 로컬 실행

```bash
poetry install
poetry run uvicorn web_api:app --host 0.0.0.0 --port 8080 --reload
```

- `GET /healthz`
- `GET /v1/categories`
- `POST /v1/jobs` : 수집 잡 생성(비동기)
- `GET /v1/jobs` : 잡 목록(최신순)
- `GET /v1/jobs/{job_id}` : 상태 조회
- `GET /v1/jobs/{job_id}/result` : 결과 JSON(상품 리스트 포함)
- `GET /v1/jobs/{job_id}/products` : 결과 상품 리스트만

### Fly.io 배포(컨테이너)

이 repo에는 `Dockerfile`/`fly.toml`이 포함되어 있습니다.

```bash
fly launch
fly volumes create crawler_data --size 1 --region nrt
fly deploy
```

> Ably API 헤더/토큰이 만료될 수 있으니, 운영에서는 env로 덮어쓰는 걸 권장합니다:
>
> - `ABLY_ANON_TOKEN`
> - `ABLY_DEVICE_ID`
> - `ABLY_APP_VERSION`
> - `ABLY_USER_AGENT`

### 1) 상품/이미지 수집

```bash
poetry run python main_api.py
```

- **인터랙티브 선택**: 실행 시 상위/하위 카테고리를 선택할 수 있습니다.
- **기본값**: 옵션을 주지 않으면 `아우터`를 기본으로 수집합니다.
- **주요 옵션**
  - `--all`: 모든 상위/하위 카테고리 수집
  - `--category <상위>` / `--subcategory <하위>`: 특정 카테고리만 수집
  - `--no-prompt`: 프롬프트 없이 실행

수집 결과:

- `output/products.json`: 수집된 상품 리스트(이미지 경로 포함)
- `output/images/<sno>/`
  - `meta.json`: 분류에 사용되는 메타데이터(옵션 색상, 소재/제조국, 가격 등)
  - `001.jpg`, `002.jpg`…: 상세 이미지(세로로 긴 건 분할될 수 있음)
    - 분할되면: `001_01.jpg`, `001_02.jpg`… + `001_original.jpg`(원본 백업)
  - `cover_01.jpg`…: 대표/커버 이미지(basic API 기반)

> 필터 기준(구매/리뷰/긍정률/최대 상품 수)은 `main_api.py` 상단 상수(`MIN_*`, `MAX_PRODUCTS`)로 조정합니다.

### 2) 이미지 분류(단일 상품 / 전체)

> 가능하면 아래 3) `image_batch_classifier.py`(배치)를 쓰는 걸 권장합니다.  
> `image_classifier.py`는 “이미지별로 병렬 호출”하는 방식이라 호출 수가 많아질 수 있고, 상품 단위 맥락 활용이 상대적으로 약합니다.

먼저 환경변수를 준비합니다:

```bash
export GOOGLE_API_KEY="..."
# 또는 export GEMINI_API_KEY="..."
```

단일 상품 폴더 분류:

```bash
poetry run python image_classifier.py output/images/54822073
```

전체 상품( `output/images/` 아래 모든 숫자 폴더) 일괄 분류:

```bash
poetry run python image_classifier.py --all
```

출력:

- `output/images/<sno>/classification.json`
- `output/images/<sno>/selected/` (선택된 이미지가 파일명 규칙으로 복사됨)
  - 예: `worn_<색상>.jpg`, `product_<색상>.jpg`, `detail_front.jpg`, `info_size.jpg` 등

### 3) 배치 분류(PoC)

```bash
poetry run python image_batch_classifier.py output/images/31106295
poetry run python image_batch_classifier.py output/images/31106295 --max-images 60
poetry run python image_batch_classifier.py output/images/31106295 --max-side 768
```

옵션:

- `--max-images N`: 전송할 이미지 수 제한(0이면 전부 전송)
- `--max-side N`: 전송 전 리사이즈(긴 변 기준, 기본 1024)

출력:

- `output/classifications_batch/<sno>_batch.json`
- `output/images/<sno>/selected/` (모델이 고른 파일들을 복사)

### 4) 이미지 분할 유틸

```bash
poetry run python image_splitter.py path/to/image.jpg
poetry run python image_splitter.py path/to/directory/
```

### 5) 상품 리뷰 가져오기

`api-examples.md`에 추가한 Review API(`webview/goods/<sno>/reviews/`)를 호출해 리뷰를 JSON으로 저장합니다.

```bash
poetry run python fetch_reviews.py 52080305
poetry run python fetch_reviews.py 52080305 --pages 3
poetry run python fetch_reviews.py 52080305 --max-reviews 50 --pretty
poetry run python fetch_reviews.py 52080305 --stdout --pretty
```

### 6) 신상마켓 상품 URL → 상품명(TSV/CSV) 추출

엑셀에서 복사한 **신상마켓 상품 URL 목록**을 넣으면, URL에서 `gid`를 추출해
`detail` API(`https://abara.sinsang.market/api/v1/goods/<gid>/detail`)를 호출하고 `content.name`(상품명)을 저장합니다.

지원 URL 형태:

- `https://sinsangmarket.kr/sinsangLens?modalGid=<gid>`
- `https://sinsangmarket.kr/search?...&modalGid=<gid>`
- `https://sinsangmarket.kr/goods/<gid>/0` (뒤에 `/0` 등 추가 경로가 붙어도 처리)

stdin으로 붙여넣고 TSV 생성:

```bash
poetry run python sinsang_product_names.py --out sinsang_product_names.tsv
```

txt 파일로 입력(추천):

```bash
poetry run python sinsang_product_names.py sinsang-urls.txt --out sinsang_product_names.tsv
```

상품명만 1열로 출력(엑셀 붙여넣기용, 헤더 없음):

```bash
poetry run python sinsang_product_names.py --names-only --out sinsang_names.txt
```

> 실행 시 `access-token`을 입력하라는 프롬프트가 뜹니다(입력 숨김).
> 현재는 프롬프트 대신 env `SINSANGMARKET_ACCESS_TOKEN`을 사용합니다.
> `.env` 파일을 루트에 만들고 아래처럼 넣어두면 자동으로 로드됩니다:
>
> ```bash
> SINSANGMARKET_ACCESS_TOKEN="YOUR_TOKEN"
> ```
>
> 필요하면 `--access-token`으로 직접 덮어쓸 수도 있어요:
>
> ```bash
> poetry run python sinsang_product_names.py --access-token "YOUR_TOKEN" --out sinsang_product_names.tsv
> ```

## 출력 폴더 구조(요약)

- `output/products.json`
- `output/images/<sno>/...` (다운로드 이미지 + `meta.json`)
- `output/images/<sno>/classification.json` (단일/전체 분류 결과)
- `output/images/<sno>/selected/` (단일/전체 분류에서 선택된 이미지)
- `output/classifications_batch/*.json` (배치 분류 결과)

## 주의사항

- 이 프로젝트는 Gemini 호출로 인해 **비용/쿼터**가 발생할 수 있습니다.
- Ably API 호출은 헤더/토큰에 의존합니다. 응답이 바뀌거나 토큰이 만료되면 수집이 실패할 수 있습니다(필요 시 `main_api.py`의 `HEADERS` 갱신).
