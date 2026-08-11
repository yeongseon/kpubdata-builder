# API 계약 — KPubData Builder

## 1. 문서 목적

이 문서는 **Studio 중심 계약이 아니라 Builder 중심 계약**을 정의합니다.

- Builder는 BuildSpec 검증, preview, build 실행, manifest 조회, publish 실행을 제공하는 실행 서비스입니다.
- Studio는 이 계약을 호출하는 외부 UI 클라이언트일 뿐입니다.
- CLI와 향후 HTTP service mode는 같은 도메인 계약을 공유해야 합니다.

## 2. 실행 모델

Builder는 **동기식 실행 모델**을 사용합니다.

| 모델 | 설명 | 적용 범위 |
| :--- | :--- | :--- |
| **동기식** | 요청-응답 안에서 결과를 바로 반환 | 모든 엔드포인트 (`/version`, `/validate`, `/preview`, `/build`, `/artifacts/{run_id}`, `/builds`) |

원칙:

- **검증, preview, build는 동기식**으로 제공합니다.
- 비동기 build 모델(`POST /builds` / `GET /builds/{run_id}`)은 후속 ADR에서 구현 예정입니다.

> **결정 기록**: ADR 0002에서 v0.4는 동기 `POST /build`만 유지하기로 결정했습니다.
> 비동기 job 모델은 상태 머신·취소·멱등성 등 시맨틱을 완비한 뒤 별도 이슈에서 구현합니다.

추가 방향:

- 현재 엔드포인트는 build 단위 계약을 유지합니다.
- 향후 버전에서는 Medallion stage별 artifact/preview 조회를 위해 `/builds/{id}/stages/{stage}/artifacts` 같은 stage-specific endpoint를 노출할 수 있습니다.

## 3. 응답 코드 정책

| 상황 | 상태 코드 | body 형태 |
| :--- | :--- | :--- |
| 정상 | `200` | 각 엔드포인트별 정상 응답 참고 |
| BuildSpec 파싱/로드 실패 | `400` | `{"status": "error", "error": "<메시지>"}` |
| BuildSpec 검증 실패 | `400` | `{"status": "invalid", "problems": ["...","..."]}` |
| 빌드 실패 (upstream 소스 오류 등) | `502` | `{"status": "failed", "outcomes": [...], ...}` |
| 리소스 없음 | `404` | `{"error": "<메시지>"}` |

> **실제 구현**: `POST /validate`에서 검증 실패 시 `422`가 아닌 `400`을 반환하며,
> body는 error 봉투 형식이 아닌 `{"status": "invalid", "problems": [...]}` 형태입니다.
> `POST /build`에서 빌드 실패 시 `502`를 반환하며, body에 `outcomes` 배열이 실립니다.

## 4. 엔드포인트 요약

| 엔드포인트 | 메서드 | 목적 | 실행 모델 |
| :--- | :--- | :--- | :--- |
| `/version` | `GET` | Builder API 계약 버전 조회 | 동기식 |
| `/validate` | `POST` | BuildSpec 검증 | 동기식 |
| `/preview` | `POST` | 샘플 실행 및 소스별 스키마 preview | 동기식 |
| `/build` | `POST` | 빌드 실행 (동기식) | 동기식 |
| `/builds/{run_id}/manifest` | `GET` | 실행 manifest JSON 본문 조회 | 동기식 |
| `/artifacts/{run_id}` | `GET` | 실행 워크스페이스 산출물 목록 조회 | 동기식 |
| `/builds` | `GET` | 빌드 이력 목록 조회 (최신 수정 시각 기준 내림차순) | 동기식 |
| `/healthz` | `GET` | 무인증 liveness probe (`{"status":"ok"}`) | 동기식 |

## 5. 엔드포인트 상세

### 5.0 `GET /healthz` (#372)

무인증 liveness/readiness probe용. 인증 게이트 밖에서 처리되어 버전·메타 정보 없이 `{"status":"ok"}`만 반환합니다.

### 5.1 `GET /version`

Builder API 계약 버전을 반환합니다.

#### 응답 `200`

```json
{
  "service": "kpubdata-builder",
  "api_version": "1.1.0"
}
```

### 5.2 `POST /validate`

BuildSpec을 실행 전에 검증합니다. body의 `spec` 키에 YAML 문자열을 전달합니다.

#### 요청

```json
{
  "spec": "dataset_id: weather-village-forecast\ntitle: 날씨 데이터\ndescription: 설명\nsources:\n  - provider: datago\n    dataset: village_fcst\nexports:\n  - kind: markdown\n    output_path: out.md\n"
}
```

#### 응답 `200` (유효한 스펙)

```json
{
  "status": "valid",
  "dataset_id": "weather-village-forecast",
  "api_version": "1.0.0"
}
```

#### 응답 `400` (스펙 로드 실패)

```json
{
  "status": "error",
  "error": "Failed to parse build spec: dataset_id is required"
}
```

#### 응답 `400` (검증 실패)

```json
{
  "status": "invalid",
  "problems": [
    "at least one source is required",
    "at least one export target is required"
  ]
}
```

### 5.3 `POST /preview`

각 소스의 스키마와 샘플 행을 산출합니다 (파일 미기록).

#### 요청

```json
{
  "spec": "...",
  "limit": 5
}
```

#### 응답 `200`

```json
{
  "dataset_id": "weather-village-forecast",
  "previews": [
    {
      "source_key": "datago.village_fcst",
      "status": "ok",
      "error": null,
      "schema": [
        {"name": "baseDate", "dtype": "Utf8", "nullable": false, "unique_count": 1}
      ],
      "sample": [["20250401", "0500", "TMP", "15"]],
      "total_rows": 288
    }
  ]
}
```

### 5.4 `POST /build`

파이프라인을 실행하고 결과를 반환합니다.

#### 요청

```json
{
  "spec": "...",
  "run_id": "my-run-001"
}
```

#### 응답 `200` (빌드 성공)

```json
{
  "status": "ok",
  "run_id": "my-run-001",
  "outcomes": [
    {
      "source_key": "datago.village_fcst",
      "status": "ok",
      "stages_completed": ["bronze", "silver", "gold"],
      "error": null
    }
  ],
  "manifest": "/path/to/dist/my-run-001/manifest.json",
  "api_version": "1.0.0"
}
```

#### 응답 `502` (빌드 실패 — upstream 소스 오류 등)

```json
{
  "status": "failed",
  "run_id": "my-run-001",
  "outcomes": [
    {
      "source_key": "datago.village_fcst",
      "status": "failed",
      "stages_completed": ["bronze"],
      "error": "fetch failed: ..."
    }
  ],
  "manifest": "/path/to/dist/my-run-001/manifest.json",
  "api_version": "1.0.0"
}
```

### 5.5 `GET /builds/{run_id}/manifest`

실행 워크스페이스에 기록된 `manifest.json` 파일의 JSON 본문을 반환합니다. Studio는 파일 다운로드 엔드포인트를 파싱하지 않고 이 엔드포인트로 manifest를 직접 소비할 수 있습니다.

#### 응답 `200`

```json
{
  "schema_version": "1.0.0",
  "build_id": "my-run-001",
  "started_at": "2025-04-01T10:30:00+00:00",
  "finished_at": "2025-04-01T10:32:15+00:00",
  "inputs": ["datago.village_fcst"],
  "outputs": ["/path/to/dist/my-run-001/gold/package.json"],
  "warnings": [],
  "errors": [],
  "row_counts": {"datago.village_fcst": 288}
}
```

#### 응답 `404`

```json
{
  "error": "manifest not found: my-run-001"
}
```

### 5.6 `GET /artifacts/{run_id}`

실행 워크스페이스의 산출물 파일 목록을 반환합니다.

#### 응답 `200`

```json
{
  "run_id": "my-run-001",
  "files": [
    "gold/artifacts/weather_report.md",
    "gold/artifacts/data.jsonl",
    "manifest.json"
  ]
}
```

#### 응답 `404`

```json
{
  "error": "run not found: my-run-001"
}
```

### 5.7 `GET /builds`

빌드 이력 목록을 최신 수정 시각 기준 내림차순으로 반환합니다. `output_root` 아래의 디렉터리를 스캔해 `manifest.json`이 있는 실행만 포함합니다.

#### 요청

**쿼리 파라미터**:
- `limit` (선택): 반환할 최대 빌드 수. 기본값은 50이며, 1 이상의 정수여야 합니다.

#### 응답 `200`

```json
{
  "builds": [
    {
      "run_id": "my-run-002",
      "status": "ok",
      "started_at": "2025-04-01T10:30:00Z",
      "finished_at": "2025-04-01T10:32:15Z"
    },
    {
      "run_id": "my-run-001",
      "status": "failed",
      "started_at": "2025-04-01T09:00:00Z",
      "finished_at": "2025-04-01T09:01:30Z"
    }
  ]
}
```

#### 응답 `400`

```json
{
  "error": "'limit' must be a positive integer"
}
```

## 6. 상태와 응답 원칙

- `BuildResult.status`는 `"ok"` | `"failed"` 두 값을 가집니다.
- `manifest`는 빌드 성공/실패 모두에서 생성 시도됩니다.
- `artifacts`는 빌드 완료 후 `/artifacts/{run_id}` 엔드포인트로 조회 가능합니다.

## 7. CLI 대응 관계

| CLI | 대응 API |
| :--- | :--- |
| `kpubdata-builder validate spec.yaml` | `POST /validate` |
| `kpubdata-builder preview spec.yaml` | `POST /preview` |
| `kpubdata-builder build spec.yaml` | `POST /build` |

## 8. 구현 현황과 Studio 향 계획

본 계약(`contract/builder-api.yaml`, info.version)은 단일 소스이며, 코드의
`kpubdata_builder.service.API_CONTRACT_VERSION`과 일치해야 합니다
(`test_service_contract`가 강제). 소비자는 `GET /version`으로 계약 버전을 먼저
확인할 수 있고, `POST /validate`·`POST /build` 응답에도 `api_version`이 실립니다.

버전 문자열 일치 외에, `test_service_contract`의 **`TestResponseConformance`** 는
실제 `dispatch()` 응답 본문이 선언된 OpenAPI 스키마에 부합하는지(wire-level
conformance) 순수 파이썬 validator(`tests/unit/_openapi.py`)로 검증합니다
(#209, ADR-0005 미해결 질문 #1). app.py 응답에서 필수 필드가 빠지거나 타입이
바뀌되 YAML이 갱신되지 않는 드리프트를 CI에서 차단합니다. 정적 경로/상태코드
대조(#317, #319)가 *선언* 일치를 본다면, 이 검사는 *실제 wire* 일치를 봅니다.

| 계약 operationId | 상태 | 현재 구현 경로 |
| :--- | :--- | :--- |
| `validateSpec` | 구현됨 | `POST /validate` (동기) |
| `previewBuild` | 구현됨 | `POST /preview` (동기) |
| `createBuild` | 구현됨 | `POST /build` (동기) |
| `getBuildManifest` | 구현됨 | `GET /builds/{run_id}/manifest` |
| `listBuildArtifacts` | 구현됨 | `GET /artifacts/{run_id}` |
| `listDatasets` | 계획(planned)/미구현 | — |
| `getBuild` | 계획(planned)/미구현 — 비동기 job 모델 | `GET /builds/{run_id}` (후속 ADR) |
| `publishArtifacts` | 계획(planned)/미구현 | — |
| (메타) | 구현됨 | `GET /version` → `{service, api_version}` |

### Studio 향 에러 봉투 (계획/미구현)

Studio 연동을 위해 향후 구조화된 에러 봉투 형식을 도입할 예정입니다.
현재는 미구현이며, 아래는 목표 형태입니다.

```json
{
  "error": {
    "code": "INVALID_BUILD_SPEC",
    "message": "sources must not be empty",
    "details": [
      {"field": "sources", "reason": "missing"}
    ]
  }
}
```

이 형식은 Studio와의 교차 레포 조율이 필요한 후속 작업에서 활성화될 예정입니다.

## 9. CORS 정책

브라우저 클라이언트(Studio 등)와의 연동을 위한 크로스-오리진 요청 정책입니다 (#322).

### 9.1 Default-Deny

- 기본 정책은 **default-deny**입니다: 환경변수 미설정 시 모든 크로스-오리진 요청을 거부합니다.
- Same-origin 요청(브라우저가 Origin 헤더를 보내지 않는 경우)은 항상 허용됩니다.

### 9.2 허용 오리진 설정

`KPUBDATA_BUILDER_ALLOWED_ORIGINS` 환경변수로 허용할 오리진을 콤마로 구분하여 설정합니다.

```bash
# 단일 오리진 허용
export KPUBDATA_BUILDER_ALLOWED_ORIGINS=http://localhost:5173

# 복수 오리진 허용
export KPUBDATA_BUILDER_ALLOWED_ORIGINS=http://localhost:5173,https://studio.example.com
```

### 9.3 Preflight 요청

`OPTIONS` 메서드로 preflight 요청을 지원합니다. 허용된 오리진에 대해 다음 헤더를 응답합니다:

- `Access-Control-Allow-Methods: GET, POST, OPTIONS`
- `Access-Control-Allow-Headers: Content-Type, X-API-Key, Authorization`
- `Access-Control-Max-Age: 86400` (24시간)

### 9.4 인증 (#248, ADR 0006/0009)

두 인증 경로를 지원합니다 (API 1.1.0+, #387):

| 방식 | 헤더 | 용도 | 환경변수 |
| :--- | :--- | :--- | :--- |
| API Key | `X-API-Key: <secret>` | 서비스 계정 (스케줄 워크플로) | `KPUBDATA_BUILDER_API_KEY` |
| Bearer (OIDC) | `Authorization: Bearer <jwt>` | 사람 사용자 (Studio, ADR 0009) | `OIDC_ISSUER` + `OIDC_AUDIENCE` |

**fail-closed** (ADR 0006): `KPUBDATA_BUILDER_API_KEY` 미설정 + dev-mode 미설정 시 모든 요청 401.

**OIDC 활성 시** (ADR 0009, #385/#386):
- `OIDC_ISSUER` + `OIDC_AUDIENCE` + 허용 목록(`OIDC_ALLOWED_HD`/`SUBJECTS`/`EMAILS`) 필수.
- 미설정 시 Bearer 비활성 — 기존 배포 무영향.

**응답 코드**:
- `401`: 인증 실패 (토큰 만료·무효·키 불일치) — 재로그인 필요
- `403`: 인가 실패 (허용 목록 밖) — 재로그인 무의미, 관리자에게 권한 요청
- `503`: JWKS 일시 장애 — 잠시 후 재시도

## 10. Python API — BuilderService

Python 코드에서 직접 사용하는 경우 `BuilderService`를 통해 HTTP 없이 같은 로직을 호출할 수 있습니다.

```python
from pathlib import Path
from kpubdata_builder.service import BuilderService

service = BuilderService(
    output_root=Path("./dist"),
    client_factory=lambda: my_kpubdata_client,
)

# 검증
response = service.validate(spec_yaml_str)
# response.status_code: 200 (valid) 또는 400 (error/invalid)
# response.body: {"status": "valid", ...} 또는 {"status": "invalid", "problems": [...]}

# 빌드
response = service.build(spec_yaml_str, run_id="my-run-001")
# response.status_code: 200 (ok) 또는 502 (failed)
# response.body: {"status": "ok"|"failed", "outcomes": [...], ...}
```

## 11. 관련 문서

| 문서 | 설명 |
| :--- | :--- |
| [BUILD_SPEC.md](./BUILD_SPEC.md) | BuildSpec 입력 계약 |
| [BUILD_STATE.md](./BUILD_STATE.md) | build 상태 머신 |
| [BOUNDARY.md](./BOUNDARY.md) | Builder-Studio 경계 |
