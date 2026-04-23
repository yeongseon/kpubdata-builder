# BuildSpec Contract — KPubData Builder

## 1. 목적

BuildSpec은 Builder가 실행하는 모든 빌드의 **단일 입력 계약**입니다. Builder는 BuildSpec을 기준으로 source 실행, export, output 디렉터리 결정, publish 요청, manifest 기록을 수행합니다.

> 참고: Bronze/Silver/Gold는 Builder 내부의 Medallion pipeline stage이며 orchestrator가 관리합니다. 현재 BuildSpec에는 이를 직접 제어하는 사용자 노출 필드(예: `pipeline.stages`)를 추가하지 않습니다.

## 2. 최소 필수 구조

다음 구조가 최소 요구사항입니다.

```yaml
version: "1"
dataset: weather-village-forecast

source:
  provider: datago
  dataset: village_fcst
  params:
    base_date: "20250401"
    nx: 55
    ny: 127

export:
  format: markdown

output:
  dir: ./dist/weather
```

## 3. 필드 분류

### 3.1 Required fields

| 필드 | 타입 | 설명 |
| :--- | :--- | :--- |
| `version` | string | BuildSpec 계약 버전 |
| `dataset` | string | 빌드 대상 데이터셋 식별자 |
| `source` | object | source 실행 정의 |
| `export` | object or array<object> | artifact 생성 방식 |
| `output` | object | 로컬 출력 경로/정책 |

### 3.2 Optional fields

| 필드 | 타입 | 설명 |
| :--- | :--- | :--- |
| `transform` | object or array<object> | 필터링, 정렬, 매핑, 파생 필드 정의 |
| `publish` | object or array<object> | 원격 게시 대상 정의 |
| `metadata` | object | 제목, 설명, 라이선스, 태그 등 부가 메타데이터 |

## 4. 필드 상세

### 4.1 `version`

- 타입: `string`
- 예시: `"1"`
- 의미: Builder가 어떤 계약 해석 규칙을 써야 하는지 결정합니다.

### 4.2 `dataset`

- 타입: `string`
- 예시: `weather-village-forecast`
- 의미: 빌드 전체를 식별하는 dataset ID입니다.

### 4.3 `source`

```yaml
source:
  provider: datago
  dataset: village_fcst
  params:
    base_date: "20250401"
    nx: 55
    ny: 127
```

| 필드 | 타입 | 필수 | 설명 |
| :--- | :--- | :--- | :--- |
| `provider` | string | 예 | `kpubdata` provider 이름 |
| `dataset` | string | 예 | provider 내부 dataset 이름 |
| `params` | object | 아니오 | source 호출 파라미터 |
| `auth` | object | 아니오 | 인증 관련 확장 필드 |

### 4.4 `export`

```yaml
export:
  format: markdown
  filename: README.md
```

또는 다중 export:

```yaml
export:
  - format: markdown
    filename: README.md
  - format: jsonl
    filename: data.jsonl
```

| 필드 | 타입 | 필수 | 설명 |
| :--- | :--- | :--- | :--- |
| `format` | string | 예 | `markdown`, `jsonl`, `parquet` 등 |
| `filename` | string | 아니오 | artifact 파일명 |
| `options` | object | 아니오 | exporter별 옵션 |

### 4.5 `output`

```yaml
output:
  dir: ./dist/weather
  overwrite: false
```

| 필드 | 타입 | 필수 | 설명 |
| :--- | :--- | :--- | :--- |
| `dir` | string | 예 | artifact가 생성될 루트 디렉터리 |
| `overwrite` | boolean | 아니오 | 기존 출력 덮어쓰기 허용 여부 |

### 4.6 `transform` (optional)

```yaml
transform:
  filter:
    field: category
    equals: TMP
  sort:
    by: baseDate
    order: asc
```

Builder는 transform을 **선언적으로** 받으며, 구현 가능한 범위만 지원해야 합니다.

### 4.7 `publish` (optional)

```yaml
publish:
  target: huggingface
  repository: org/weather-village-forecast
```

게시 실행은 build와 구분되는 후속 단계일 수 있습니다.

### 4.8 `metadata` (optional)

```yaml
metadata:
  title: "2025년 동네예보 데이터셋"
  description: "기상청 동네예보 기준 산출물"
  license: "CC-BY-4.0"
```

## 5. 검증 규칙

Builder는 최소한 다음 규칙을 검증해야 합니다.

1. `version`은 지원되는 BuildSpec 버전이어야 합니다.
2. `dataset`은 비어 있지 않은 문자열이어야 합니다.
3. `source.provider`는 비어 있지 않아야 합니다.
4. `source.dataset`은 비어 있지 않아야 합니다.
5. `export`는 1개 이상 정의되어야 합니다.
6. 모든 `export.format`은 Builder가 지원하는 exporter여야 합니다.
7. `output.dir`은 비어 있지 않은 경로여야 합니다.
8. `publish`가 존재하면 필요한 target별 필수 필드가 충족되어야 합니다.
9. 지원하지 않는 필드 조합은 검증 단계에서 실패해야 합니다.

### 예시 오류

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

## 6. 버전 호환성

| BuildSpec version | 상태 | 설명 |
| :--- | :--- | :--- |
| `1` | current | v0.1 기준 기본 계약 |

호환성 원칙:

- minor 문서 확장은 가능한 한 backward compatible하게 추가합니다.
- breaking change는 새 `version` 값으로만 도입합니다.
- Studio는 Builder가 지원하는 version만 전송해야 하며 임의 해석을 추가하면 안 됩니다.

## 7. 계약 원칙 요약

- BuildSpec은 Builder가 소유합니다.
- BuildSpec은 실행 계획이지 UI 상태 저장 포맷이 아닙니다.
- export/publish는 source 정의를 대체하지 않습니다.
- manifest는 BuildSpec의 결과 기록물이지 입력이 아닙니다.

## 8. 관련 문서

| 문서 | 설명 |
| :--- | :--- |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | BuildSpec 중심 설계 |
| [API_CONTRACT.md](./API_CONTRACT.md) | BuildSpec을 받는 서비스 계약 |
| [BOUNDARY.md](./BOUNDARY.md) | Builder-Studio 경계 |
