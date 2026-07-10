# BuildSpec Contract — KPubData Builder

## 1. 목적

BuildSpec은 Builder가 실행하는 모든 빌드의 **단일 입력 계약**입니다. Builder는 BuildSpec을 기준으로 source 실행, export, output 디렉터리 결정, publish 요청, manifest 기록을 수행합니다.

> 참고: Bronze/Silver/Gold는 Builder 내부의 Medallion pipeline stage이며 orchestrator가 관리합니다. 현재 BuildSpec에는 이를 직접 제어하는 사용자 노출 필드(예: `pipeline.stages`)를 추가하지 않습니다.

## 2. 최소 필수 구조

다음 구조가 최소 요구사항입니다.

```yaml
dataset_id: weather-village-forecast
title: "동네예보 데이터셋"
description: "기상청 동네예보 서비스에서 수집한 기상 예보 데이터"

sources:
  - provider: datago
    dataset: village_fcst
    params:
      base_date: "20250401"
      nx: 55
      ny: 127

exports:
  - kind: markdown
    output_path: artifacts/weather_report.md
```

## 3. 필드 분류

### 3.1 Required fields

| 필드 | 타입 | 설명 |
| :--- | :--- | :--- |
| `dataset_id` | string | 빌드 대상 데이터셋의 전역 식별자 |
| `title` | string | 사람이 읽는 데이터셋 제목 |
| `description` | string | 빌드 목적과 데이터 설명 |
| `sources` | array<object> | 1개 이상의 입력 소스 정의 |
| `exports` | array<object> | 1개 이상의 출력 대상 정의 |

### 3.2 Optional fields

| 필드 | 타입 | 설명 |
| :--- | :--- | :--- |
| `transforms` | array<string> | 적용 예정인 변환 단계 이름 목록 |
| `publish` | boolean | 빌드 후 게시까지 수행할지 여부 (기본값: `false`) |
| `metadata` | object | 산출물에 실을 임의 메타데이터 (문자열 키/값 쌍) |
| `splits` | object | 데이터셋 분할 정의 |

## 4. 필드 상세

### 4.1 `dataset_id`

- 타입: `string`
- 예시: `weather-village-forecast`
- 의미: 빌드 전체를 식별하는 dataset ID입니다.

### 4.2 `title`

- 타입: `string`
- 예시: `"2025년 동네예보 데이터셋"`
- 의미: 사람이 읽는 데이터셋 제목입니다.

### 4.3 `description`

- 타입: `string`
- 예시: `"기상청 동네예보 서비스에서 수집한 기상 예보 및 실제 관측 데이터"`
- 의미: 빌드 목적과 데이터 내용을 설명합니다.

### 4.4 `sources` (배열)

각 소스는 다음 필드를 가집니다.

```yaml
sources:
  - provider: datago
    dataset: village_fcst
    params:
      base_date: "20250401"
      nx: 55
      ny: 127
    alias: forecast
    normalization_mode: canonical
```

| 필드 | 타입 | 필수 | 설명 |
| :--- | :--- | :--- | :--- |
| `provider` | string | 예 | `kpubdata` provider 이름 |
| `dataset` | string | 예 | provider 내부 dataset 이름 |
| `params` | object | 아니오 | source 호출 파라미터 (JSON 호환 값) |
| `alias` | string | 아니오 | 조립 단계에서 사용할 사용자 정의 소스 이름 |
| `normalization_mode` | string | 아니오 | 정규화 모드 (`canonical` 기본값, `raw` 지원) |

### 4.5 `exports` (배열)

각 export 대상은 다음 필드를 가집니다.

```yaml
exports:
  - kind: markdown
    output_path: artifacts/weather_report.md
  - kind: jsonl
    output_path: artifacts/data.jsonl
    options:
      indent: 2
```

| 필드 | 타입 | 필수 | 설명 |
| :--- | :--- | :--- | :--- |
| `kind` | string | 예 | exporter 레지스트리 키 (`markdown`, `jsonl`, `parquet`, `csv`, `huggingface`, `kaggle` 등) |
| `output_path` | string | 예 | output_dir 기준 상대 출력 경로 |
| `options` | object | 아니오 | exporter별 선택 옵션 (JSON 호환 값) |

#### Studio → Builder 필드명 매핑 (#264)

KPubData Studio에서는 `ExportTarget.format`이라는 필드명을 사용하지만, Builder에서는 `kind`를 사용합니다. Studio에서 Builder로 BuildSpec을 전송할 때, Studio의 `specMapping.ts:toBuilderSpec()` 함수가 자동으로 필드명을 변환합니다.

| Studio (format) | Builder (kind) | 비고 |
| :--- | :--- | :--- |
| `"markdown"` | `"markdown"` | |
| `"jsonl"` | `"jsonl"` | |
| `"parquet"` | `"parquet"` | |
| `"csv"` | `"csv"` | |
| `"huggingface"` | `"huggingface"` | |
| `"kaggle"` | `"kaggle"` | |

> **참고**: 값 자체는 동일하며, 단지 필드명만 `format` → `kind`로 변환됩니다. Builder API를 직접 호출하는 경우(CLI 등)에는 항상 `kind`를 사용해야 합니다.

### 4.6 `transforms` (optional)

```yaml
transforms:
  - normalize_dates
  - filter_outliers
```

적용 예정인 변환 단계 이름의 목록입니다.

> 계획(planned)/미구현: `transforms`는 현재 파싱되어 BuildSpec에 보존되지만, 실제 변환 로직은 아직 구현되지 않았습니다.

### 4.7 `publish` (optional)

```yaml
publish: true
```

빌드 후 자동으로 게시(publish) 단계까지 실행할지 여부입니다. 기본값은 `false`입니다.

> 계획(planned)/미구현: 현재 `publish: true`로 설정해도 게시 로직이 실행되지 않습니다. 게시 기능은 향후 릴리스에서 활성화될 예정입니다.

### 4.8 `metadata` (optional)

```yaml
metadata:
  author: "Sisyphus-Junior"
  license: "CC-BY-4.0"
  version: "1.0.0"
```

산출물에 실을 임의 메타데이터입니다. 키와 값 모두 문자열이어야 합니다.

### 4.9 `splits` (optional)

```yaml
splits:
  mode: ratio
  ratios:
    train: 0.8
    val: 0.1
    test: 0.1
  seed: 42
```

데이터셋을 명명된 분할로 나누는 방법을 정의합니다.

| 필드 | 타입 | 필수 | 설명 |
| :--- | :--- | :--- | :--- |
| `mode` | string | 예 | 분할 방식 (`ratio`: 비율 기반, `key`: 컬럼 값 기반) |
| `ratios` | object | 아니오 | ratio 모드에서 분할 이름 → 비율 매핑 (합이 1.0이어야 함) |
| `key` | string | 아니오 | key 모드에서 분할 기준이 되는 컬럼 이름 |
| `seed` | integer | 아니오 | ratio 모드의 결정적 셔플 시드 (기본값: `0`) |

> 계획(planned)/미구현: `splits`는 현재 파싱되어 BuildSpec에 보존되지만, 실제 분할 로직은 아직 구현되지 않았습니다.

## 5. 검증 규칙

Builder는 최소한 다음 규칙을 검증해야 합니다.

1. `dataset_id`는 비어 있지 않은 문자열이어야 합니다.
2. `title`은 비어 있지 않은 문자열이어야 합니다.
3. `description`은 비어 있지 않은 문자열이어야 합니다.
4. `sources`는 1개 이상 정의되어야 합니다.
5. 각 `sources[].provider`는 비어 있지 않아야 합니다.
6. 각 `sources[].dataset`은 비어 있지 않아야 합니다.
7. `exports`는 1개 이상 정의되어야 합니다.
8. 각 `exports[].kind`는 비어 있지 않아야 합니다.
9. 각 `exports[].output_path`는 비어 있지 않아야 합니다.
10. 지원하지 않는 필드 조합은 검증 단계에서 실패해야 합니다.

## 6. 버전 호환성

현재 BuildSpec 계약은 단일 버전(`schema_version` 필드 없음)으로 운영됩니다.

호환성 원칙:

- minor 문서 확장은 가능한 한 backward compatible하게 추가합니다.
- breaking change는 신규 계약 버전으로만 도입합니다.
- Studio는 Builder가 지원하는 버전만 전송해야 하며 임의 해석을 추가하면 안 됩니다.

## 7. 계약 원칙 요약

- BuildSpec은 Builder가 소유합니다.
- BuildSpec은 실행 계획이지 UI 상태 저장 포맷이 아닙니다.
- exports/publish는 source 정의를 대체하지 않습니다.
- manifest는 BuildSpec의 결과 기록물이지 입력이 아닙니다.

## 8. 관련 문서

| 문서 | 설명 |
| :--- | :--- |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | BuildSpec 중심 설계 |
| [API_CONTRACT.md](./API_CONTRACT.md) | BuildSpec을 받는 서비스 계약 |
| [BOUNDARY.md](./BOUNDARY.md) | Builder-Studio 경계 |
| [ALGORITHM.md](./ALGORITHM.md) | 전체 빌드 알고리즘 명세 |
