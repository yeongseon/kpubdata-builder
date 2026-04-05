# API Contract — KPubData Builder

KPubData Builder는 터미널(CLI)과 파이썬 코드(Service-level API) 두 가지 방식으로 사용할 수 있습니다.

## 1. CLI Commands (명령줄 도구)

터미널에서 `kpubdata-builder` 명령어를 통해 실행할 수 있습니다.

```mermaid
flowchart TD
    V[validate] -- "Spec YAML" --> P[preview]
    P -- "Sample Data" --> B[build]
    B -- "Files + Manifest" --> PUB[publish]
    
    subgraph "Input / Output"
        V1[YAML File] -.-> V
        V -- "Result" -.-> V2[OK / Error Msg]
        P -- "Output" -.-> P2[Schema + 5 Rows]
        B -- "Output" -.-> B2[Artifacts + manifest.json]
        PUB -- "Output" -.-> PUB2[URL / Success]
    end
```

### 1.1 validate (검증)
작성한 빌드 기획서(BuildSpec YAML)에 문법 오류나 논리적 오류가 없는지 확인합니다.
- **사용 예시:** `kpubdata-builder validate spec.yaml`
- **옵션:** `--strict` (더 엄격한 규칙 적용)
- **출력:** "Success" 또는 상세 오류 메시지

### 1.2 preview (미리보기)
데이터를 아주 조금만 가져와서, 실제로 어떤 항목들이 들어있고 어떤 모양으로 파일이 만들어질지 살짝 보여줍니다.
- **사용 예시:** `kpubdata-builder preview spec.yaml`
- **출력:** 데이터 스키마(항목 이름들)와 처음 5건의 샘플 데이터

### 1.3 build (빌드 실행)
실제로 공공데이터를 모두 가져와서 파일을 만들고, 결과 보고서(Manifest)까지 생성합니다.
- **사용 예시:** `kpubdata-builder build spec.yaml --output-dir ./dist`
- **출력:** 생성된 파일 목록 및 `manifest.json`

### 1.4 publish (배포)
빌드가 완료된 파일들을 외부 저장소(Hugging Face 등)로 올립니다.
- **사용 예시:** `kpubdata-builder publish spec.yaml`

---

## 2. Service-level Operations (파이썬 코드 API)

다른 파이썬 프로그램에서 Builder의 기능을 가져와 쓰고 싶을 때 사용합니다.

```mermaid
sequenceDiagram
    participant C as Client (App)
    participant B as Builder Service
    participant V as Validator
    participant E as Executor
    participant EX as Exporter
    participant M as ManifestWriter

    C->>B: execute_build(spec_path, output_dir)
    B->>V: validate_spec(spec_path)
    V-->>B: ValidationResult (OK)
    B->>E: run_fetching(spec)
    E-->>B: ArtifactDataset
    B->>EX: export_all(dataset, output_dir)
    EX-->>B: List of file paths
    B->>M: generate_manifest(spec, results)
    M-->>B: BuildManifest object
    B-->>C: Return BuildManifest
```

### 2.1 validate_spec
- **시그니처:** `validate_spec(spec_path: str) -> ValidationResult`
- **파라미터:** `spec_path` (YAML 파일 경로)
- **반환값:** 오류 목록을 포함한 결과 객체

### 2.2 preview_build
- **시그니처:** `preview_build(spec_path: str) -> ArtifactDataset`
- **파라미터:** `spec_path` (YAML 파일 경로)
- **반환값:** 샘플 데이터가 들어있는 데이터셋 객체

### 2.3 execute_build
- **시그니처:** `execute_build(spec_path: str, output_dir: str) -> BuildManifest`
- **파라미터:** `spec_path` (기획서 경로), `output_dir` (저장할 폴더 경로)
- **반환값:** 빌드 실행 요약 정보가 담긴 Manifest 객체

### 2.4 publish_build
- **시그니처:** `publish_build(spec_path: str, target: Optional[str] = None) -> bool`
- **파라미터:** `target` (특정 배포처 이름)
- **반환값:** 배포 성공 여부 (True/False)

---

## 3. Manifest Contract (결과 명세서 예시)

빌드가 성공하면 항상 `manifest.json` 파일이 생성됩니다. 이 파일은 빌드가 제대로 되었는지 확인하는 영수증과 같습니다.

```mermaid
graph TD
    M[manifest.json] --> B[build_id]
    M --> S[spec_digest]
    M --> T[timestamps]
    M --> SRC[sources]
    M --> ART[artifact_paths]
    M --> RC[record_count]
    M --> WE[warnings_errors]
    
    T --> SA[started_at]
    T --> FA[finished_at]
    
    SRC --> PRV[provider]
    SRC --> DST[dataset]
    SRC --> RF[records_fetched]
```

### 3.1 JSON Output 예시
```json
{
  "build_id": "bld-20250401-abc1234",
  "spec_digest": "sha256:d8e8f8...",
  "started_at": "2025-04-01T10:00:00Z",
  "finished_at": "2025-04-01T10:05:30Z",
  "sources": [
    {
      "provider": "datago",
      "dataset": "village_fcst",
      "records_fetched": 1500
    }
  ],
  "artifact_paths": [
    "artifacts/weather_report.md",
    "artifacts/data.jsonl"
  ],
  "record_count": 1500,
  "warnings": [],
  "errors": []
}
```

### 3.2 핵심 필드 설명
- `build_id`: 이번 빌드에 부여된 고유 번호
- `spec_digest`: 어떤 기획서로 빌드했는지 식별하는 지문 (기획서가 바뀌면 이 값도 바뀝니다)
- `record_count`: 최종적으로 수집된 데이터의 총 개수
- `warnings`: 빌드 중에 발생한 사소한 문제들 (데이터 항목 누락 등)

---

## 📚 관련 문서

### 이 저장소 내 문서
| 문서 | 설명 |
| :--- | :--- |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | 시스템 아키텍처 설계 |
| [DOMAIN_MODEL.md](./DOMAIN_MODEL.md) | 도메인 모델 정의 |
| [EXPORT_MODEL.md](./EXPORT_MODEL.md) | 데이터 변환 모델 |

### KPubData Product Family
| 저장소 | 문서 | 설명 |
| :--- | :--- | :--- |
| [kpubdata](https://github.com/yeongseon/kpubdata) | [API_SPEC.md](https://github.com/yeongseon/kpubdata/blob/main/API_SPEC.md) | Core API 명세 |
| [kpubdata-studio](https://github.com/yeongseon/kpubdata-studio) | [API_CONTRACT.md](https://github.com/yeongseon/kpubdata-studio/blob/main/API_CONTRACT.md) | Studio 인터페이스 규약 |

