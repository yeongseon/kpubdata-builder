# API 계약 — KPubData Builder

## 1. 문서 목적

이 문서는 **Studio 중심 계약이 아니라 Builder 중심 계약**을 정의합니다.

- Builder는 BuildSpec 검증, preview, build 실행, manifest 조회, publish 실행을 제공하는 실행 서비스입니다.
- Studio는 이 계약을 호출하는 외부 UI 클라이언트일 뿐입니다.
- CLI와 향후 HTTP service mode는 같은 도메인 계약을 공유해야 합니다.

## 2. 실행 모델

Builder는 두 가지 실행 모델을 가집니다.

| 모델 | 설명 | 적합한 작업 |
| :--- | :--- | :--- |
| **동기식** | 요청-응답 안에서 결과를 바로 반환 | `/validate`, `/preview` |
| **비동기식** | build를 생성하고 상태를 폴링 | `/build`, `/artifacts/{run_id}` |

원칙:

- **검증과 preview는 동기식**으로 제공 가능합니다.
- **실제 build와 publish는 비동기식**으로 모델링하는 것을 기본값으로 둡니다.

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
| `/build` | `POST` | 빌드 실행 (동기; 계약은 비동기 `/builds` 지향) | 동기식(현재) |
| `/artifacts/{run_id}` | `GET` | 실행 워크스페이스 산출물 목록 조회 | 동기식 |

## 5. 엔드포인트 상세

### 5.1 `GET /version`

Builder API 계약 버전을 반환합니다.

#### 응답 `200`

```json
{
  "service": "kpubdata-builder",
  "api_version": "1.0.0"
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

### 5.5 `GET /artifacts/{run_id}`

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

| 계약 operationId | 상태 | 현재 구현 경로 |
| :--- | :--- | :--- |
| `validateSpec` | 구현됨 | `POST /validate` (동기) |
| `previewBuild` | 구현됨 | `POST /preview` (동기) |
| `createBuild` | 구현됨 | `POST /build` (동기; 계약은 비동기 `POST /builds` 지향) |
| `listBuildArtifacts` | 구현됨 | `GET /artifacts/{run_id}` |
| `listDatasets` | 계획(planned)/미구현 | — |
| `getBuild` | 계획(planned)/미구현 | — |
| `getBuildManifest` | 계획(planned)/미구현 | — |
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

## 9. Python API — BuilderService

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

## 10. 관련 문서

| 문서 | 설명 |
| :--- | :--- |
| [BUILD_SPEC.md](./BUILD_SPEC.md) | BuildSpec 입력 계약 |
| [BUILD_STATE.md](./BUILD_STATE.md) | build 상태 머신 |
| [BOUNDARY.md](./BOUNDARY.md) | Builder-Studio 경계 |
