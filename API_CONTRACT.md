# API Contract — KPubData Builder

## 1. 문서 목적

이 문서는 **Studio 중심 계약이 아니라 Builder 중심 계약**을 정의합니다.

- Builder는 BuildSpec 검증, preview, build 실행, manifest 조회, publish 실행을 제공하는 실행 서비스입니다.
- Studio는 이 계약을 호출하는 외부 UI 클라이언트일 뿐입니다.
- CLI와 향후 HTTP service mode는 같은 도메인 계약을 공유해야 합니다.

## 2. 실행 모델

Builder는 두 가지 실행 모델을 가집니다.

| 모델 | 설명 | 적합한 작업 |
| :--- | :--- | :--- |
| **Sync** | 요청-응답 안에서 결과를 바로 반환 | `/datasets`, `/spec/validate`, `/preview` |
| **Async** | build를 생성하고 상태를 폴링 | `/builds`, `/builds/{id}`, `/builds/{id}/manifest`, `/builds/{id}/artifacts`, `/publish` |

원칙:

- **검증과 preview는 동기식**으로 제공 가능합니다.
- **실제 build와 publish는 비동기식**으로 모델링하는 것을 기본값으로 둡니다.

## 3. 표준 에러 응답

모든 실패 응답은 아래 형태를 따릅니다.

```json
{
  "error": {
    "code": "INVALID_BUILD_SPEC",
    "message": "source.provider is required",
    "details": [
      {"field": "source.provider", "reason": "missing"}
    ]
  }
}
```

| 필드 | 타입 | 설명 |
| :--- | :--- | :--- |
| `error.code` | string | 안정적인 기계 판독용 오류 코드 |
| `error.message` | string | 사람이 읽는 요약 메시지 |
| `error.details` | array<object> | 필드별 세부 정보 |

## 4. 엔드포인트 요약

| 엔드포인트 | 메서드 | 목적 | 실행 모델 |
| :--- | :--- | :--- | :--- |
| `/datasets` | `GET` | 사용 가능한 dataset/source 메타데이터 조회 | Sync |
| `/spec/validate` | `POST` | BuildSpec 검증 | Sync |
| `/preview` | `POST` | 샘플 실행 및 export preview | Sync |
| `/builds` | `POST` | 새로운 build 실행 시작 | Async |
| `/builds/{id}` | `GET` | build 상태 조회 | Async |
| `/builds/{id}/manifest` | `GET` | build manifest 조회 | Async |
| `/builds/{id}/artifacts` | `GET` | build artifact 목록 조회 | Async |
| `/publish` | `POST` | artifact 게시 실행 | Async |

## 5. 엔드포인트 상세

### 5.1 `GET /datasets`

Builder가 실행 가능한 dataset/source 카탈로그를 반환합니다.

#### Response `200`

```json
{
  "datasets": [
    {
      "provider": "datago",
      "dataset": "village_fcst",
      "supports_preview": true,
      "supported_exports": ["markdown", "jsonl", "parquet"]
    }
  ]
}
```

### 5.2 `POST /spec/validate`

BuildSpec을 실행 전에 검증합니다.

#### Request

```json
{
  "spec": {
    "version": "1",
    "dataset": "weather-village-forecast",
    "source": {
      "provider": "datago",
      "dataset": "village_fcst"
    },
    "export": {
      "format": "markdown"
    },
    "output": {
      "dir": "./dist/weather"
    }
  }
}
```

#### Response `200`

```json
{
  "valid": true,
  "issues": []
}
```

#### Response `422`

```json
{
  "error": {
    "code": "INVALID_BUILD_SPEC",
    "message": "source.provider is required",
    "details": [
      {"field": "source.provider", "reason": "missing"}
    ]
  }
}
```

### 5.3 `POST /preview`

BuildSpec을 바탕으로 제한된 샘플 실행 결과와 export preview를 반환합니다.

#### Request

```json
{
  "spec": {
    "version": "1",
    "dataset": "weather-village-forecast",
    "source": {
      "provider": "datago",
      "dataset": "village_fcst",
      "params": {
        "base_date": "20250401",
        "nx": 55,
        "ny": 127
      }
    },
    "export": {
      "format": "markdown"
    },
    "output": {
      "dir": "./dist/weather"
    }
  },
  "limit": 5
}
```

#### Response `200`

```json
{
  "records": [
    {
      "baseDate": "20250401",
      "baseTime": "0500",
      "category": "TMP",
      "fcstValue": "15"
    }
  ],
  "schema": {
    "baseDate": "string",
    "baseTime": "string",
    "category": "string",
    "fcstValue": "string"
  },
  "export_preview": {
    "format": "markdown",
    "artifact_path": "./dist/weather/README.md"
  }
}
```

### 5.4 `POST /builds`

새로운 build run을 생성합니다.

#### Request

```json
{
  "spec": {
    "version": "1",
    "dataset": "weather-village-forecast",
    "source": {
      "provider": "datago",
      "dataset": "village_fcst"
    },
    "export": {
      "format": "markdown"
    },
    "output": {
      "dir": "./dist/weather"
    }
  }
}
```

#### Response `202`

```json
{
  "build_id": "bld_20260423_001",
  "state": "validated",
  "status_url": "/builds/bld_20260423_001"
}
```

### 5.5 `GET /builds/{id}`

build run 상태를 반환합니다.

#### Response `200`

```json
{
  "build_id": "bld_20260423_001",
  "dataset": "weather-village-forecast",
  "state": "running",
  "started_at": "2026-04-23T09:00:00Z",
  "updated_at": "2026-04-23T09:00:05Z"
}
```

### 5.6 `GET /builds/{id}/manifest`

manifest가 생성된 이후 manifest 본문을 반환합니다.

#### Response `200`

```json
{
  "build_id": "bld_20260423_001",
  "state": "manifested",
  "manifest": {
    "spec_version": "1",
    "dataset": "weather-village-forecast",
    "artifacts": [
      {
        "path": "./dist/weather/README.md",
        "format": "markdown"
      }
    ]
  }
}
```

### 5.7 `GET /builds/{id}/artifacts`

artifact 목록과 메타데이터를 반환합니다.

#### Response `200`

```json
{
  "build_id": "bld_20260423_001",
  "artifacts": [
    {
      "path": "./dist/weather/README.md",
      "format": "markdown",
      "size_bytes": 2048
    }
  ]
}
```

### 5.8 `POST /publish`

이미 생성된 build artifact를 게시합니다.

#### Request

```json
{
  "build_id": "bld_20260423_001",
  "target": {
    "kind": "huggingface",
    "repository": "org/weather-village-forecast"
  }
}
```

#### Response `202`

```json
{
  "publish_id": "pub_20260423_001",
  "build_id": "bld_20260423_001",
  "state": "queued"
}
```

## 6. 상태와 응답 원칙

- `draft`, `validated`, `running`, `exported`, `manifested`, `published`, `failed` 상태는 [BUILD_STATE.md](./BUILD_STATE.md)를 따릅니다.
- `manifest`는 `manifested` 이상 상태에서 조회 가능해야 합니다.
- `artifacts`는 `exported` 이상 상태에서 조회 가능해야 합니다.
- `published`는 publish 단계 성공을 의미하며 build artifact 생성 성공과는 구분됩니다.

## 7. CLI 대응 관계

| CLI | 대응 API |
| :--- | :--- |
| `kpubdata-builder validate spec.yaml` | `POST /spec/validate` |
| `kpubdata-builder preview spec.yaml` | `POST /preview` |
| `kpubdata-builder build spec.yaml` | `POST /builds` + `GET /builds/{id}` |
| `kpubdata-builder publish --build-id ...` | `POST /publish` |

## 8. 관련 문서

| 문서 | 설명 |
| :--- | :--- |
| [BUILD_SPEC.md](./BUILD_SPEC.md) | BuildSpec 입력 계약 |
| [BUILD_STATE.md](./BUILD_STATE.md) | build 상태 머신 |
| [BOUNDARY.md](./BOUNDARY.md) | Builder-Studio 경계 |
