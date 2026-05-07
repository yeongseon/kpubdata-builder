# 에러 처리 설계 — KPubData Builder

## 1. 개요

이 문서는 KPubData Builder의 에러/예외 처리 설계를 정의합니다.
Builder는 여러 단계(Spec 로딩 → 검증 → 실행 → 조립 → 내보내기 → Manifest)를 거치며,
각 단계에서 발생할 수 있는 실패를 명확하게 분류하고 처리하는 것이 핵심입니다.

## 2. 설계 원칙

1. **모든 런타임 실패는 `BuildError` 계층으로 수렴** — stdlib 예외(`ValueError` 등)가 사용자에게 직접 노출되지 않아야 함
2. **원인 보존** — `kpubdata`의 `PublicDataError`는 `__cause__`로 항상 보존
3. **단계별 에러 분리** — 어떤 단계에서 실패했는지 예외 타입만으로 구분 가능
4. **Manifest는 항상 생성** — 실패한 빌드도 manifest를 남겨 감사 추적 가능
5. **로그 1회 + 예외 전파** — 같은 에러를 매 레이어에서 중복 로깅하지 않음

## 3. 예외 계층

```
BuildError (base)
├── SpecLoadError          — YAML 파일 I/O 또는 파싱 실패
├── SpecValidationError    — 스펙 검증 실패 (여러 이슈 집계 가능)
├── SourceExecutionError   — source 데이터 fetch 실패
├── AssemblyError          — 데이터 조립 실패 (누락 source 등)
├── ExportError            — 파일 생성/쓰기 실패
├── ManifestWriteError     — manifest 쓰기 실패
└── PublishError           — 외부 저장소 배포 실패
```

### 3.1 예외 컨텍스트

```python
class BuildError(Exception):
    """Builder 모든 에러의 기반 클래스."""

class SpecValidationError(BuildError):
    """스펙 검증 실패. 여러 이슈를 집계."""
    def __init__(self, message: str, *, issues: tuple[str, ...] = ()):
        super().__init__(message)
        self.issues = issues

class SourceExecutionError(BuildError):
    """source fetch 실패. 원본 에러를 __cause__로 보존."""
    def __init__(self, message: str, *, source_alias: str = "", provider: str = "", dataset: str = ""):
        super().__init__(message)
        self.source_alias = source_alias
        self.provider = provider
        self.dataset = dataset
```

## 4. 단계별 에러 처리

### 4.1 Spec 로딩 (`spec_io.py` / `spec.py`)

| 실패 원인 | 변환 |
|:---|:---|
| 파일 없음 / 읽기 권한 없음 (`OSError`) | `SpecLoadError` |
| YAML 문법 오류 (`yaml.YAMLError`) | `SpecLoadError` |
| YAML은 유효하나 스키마 불일치 | `SpecValidationError` |

### 4.2 Spec 검증 (`validator.py`)

| 실패 원인 | 변환 |
|:---|:---|
| `dataset_id` 비어 있음 | `SpecValidationError` (issues에 추가) |
| sources 없음 | `SpecValidationError` (issues에 추가) |
| exports 없음 | `SpecValidationError` (issues에 추가) |
| source.provider/dataset 비어 있음 | `SpecValidationError` (issues에 추가) |
| source.alias 중복 | `SpecValidationError` (issues에 추가) |
| export.output_path 중복 | `SpecValidationError` (issues에 추가) |

**핵심**: 첫 번째 오류에서 즉시 중단하지 않고, 모든 이슈를 모아 한 번에 보고

### 4.3 Source 실행 (`executor.py`)

```
kpubdata.PublicDataError
  ├── TransportTimeoutError       → SourceExecutionError (cause.retryable 계승)
  ├── AuthError                   → SourceExecutionError + 같은 provider 후속 source 중단
  ├── RateLimitError              → SourceExecutionError (cause.retryable 계승 — 항상 retryable은 아님)
  ├── ServiceUnavailableError     → SourceExecutionError (cause.retryable 계승)
  ├── ParseError                  → SourceExecutionError
  ├── ProviderResponseError       → SourceExecutionError
  ├── DatasetNotFoundError        → SourceExecutionError
  ├── ConfigError                 → SourceExecutionError (API 키 미설정 등)
  ├── InvalidRequestError         → SourceExecutionError (잘못된 쿼리 파라미터)
  ├── UnsupportedCapabilityError  → SourceExecutionError (미지원 operation)
  └── ProviderNotRegisteredError  → SourceExecutionError (미등록 provider)
```

> **`retryable` 플래그 계승**: Builder는 `cause.retryable` 값을 그대로 계승합니다.
> `RateLimitError`라도 quota 소진(retryable=False)일 수 있으므로, 예외 타입만으로 retryable 여부를 판단하지 않습니다.

**정책**:
- 기본: source 하나라도 실패하면 build 실패
- executor는 여러 source 결과를 계속 수집 (에러 집계용)
- assembler/export/publish는 errors가 비어 있을 때만 실행
- 향후: `allow_partial=True` 옵션으로 부분 빌드 허용

**반환 타입**: `ArtifactDataset` 대신 `ExecutionResult`

```python
@dataclass
class ExecutionResult:
    records_by_source: dict[str, Sequence[dict[str, JsonValue]]]
    row_counts: dict[str, int]
    warnings: list[str]
    errors: list[SourceExecutionError]
```

### 4.4 데이터 조립 (`assembler.py`)

| 실패 원인 | 변환 |
|:---|:---|
| `records_by_source`에 spec의 source key 누락 | `AssemblyError` |
| 빈 레코드 (0건) | 허용 (warning) |

**핵심**: silent skip 제거. 누락 key는 명시적 실패.

### 4.5 내보내기 (`exporters/`)

| 실패 원인 | 변환 |
|:---|:---|
| 디스크 공간 부족 (`OSError`) | `ExportError` |
| 파일 쓰기 권한 없음 (`OSError`) | `ExportError` |
| 지원하지 않는 export kind | `ExportError` |

**부분 산출물 정리 정책**:
- exporter가 여러 파일을 생성하는 도중 실패하면, 이미 생성된 파일을 정리(cleanup)합니다
- 방법: staging 디렉토리(`output_dir/.staging/`)에 먼저 쓰고, 전체 성공 시 최종 위치로 이동
- 실패 시 staging 디렉토리를 삭제하여 부분 산출물이 남지 않도록 보장
- 이 정책은 "partial artifact 없음" 원칙과 일관됨

### 4.6 Manifest 쓰기 (`manifest.py`)

| 실패 원인 | 변환 |
|:---|:---|
| 디스크 I/O 실패 (`OSError`) | `ManifestWriteError` |

**원자적 쓰기**: temp file → `os.replace()`

### 4.7 배포 (`publishers/`)

| 실패 원인 | 변환 |
|:---|:---|
| 네트워크 실패 | `PublishError` |
| 인증 실패 | `PublishError` |
| 원격 저장소 오류 | `PublishError` |

## 5. Manifest와 에러의 관계

### 5.1 BuildManifest 확장

```python
@dataclass(frozen=True)
class BuildManifest:
    build_id: str
    status: str  # "succeeded" | "failed" | "partial"
    started_at: datetime
    finished_at: datetime
    spec_digest: str  # 어떤 스펙으로 빌드했는지 식별
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    sources: tuple[SourceSummary, ...] = ()  # source별 결과
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    row_counts: dict[str, int] = field(default_factory=dict)
```

### 5.2 에러 흐름

```
Orchestrator
  ├── executor → ExecutionResult (errors 수집)
  ├── if errors → status="failed", manifest 생성, export/publish 건너뜀
  ├── assembler → AssemblyError 시 status="failed"
  ├── exporter → ExportError 시 status="failed"
  └── manifest_writer → 항상 실행 (실패 빌드도 기록)
```

### 5.3 역할 분리

| 컴포넌트 | 역할 |
|:---|:---|
| `build_manifest()` | 실행 결과를 받아 `BuildManifest` 객체 생성 |
| `manifest_writer()` | 직렬화 + 원자적 파일 쓰기만 담당 |
| Orchestrator | 에러 수집 → `build_manifest()` 호출 → `manifest_writer()` 호출 |

## 6. 에러 → 사용자 메시지 변환

에러 메시지 변환은 **CLI/Studio 같은 최외곽 boundary**에서 수행합니다.

- Builder 내부: 구조화된 예외 + 메타데이터만 유지
- CLI: `format_user_error(exc)` 같은 함수로 한국어 메시지 변환
- Manifest: machine-readable summary (UI 문장 아님)

## 7. kpubdata (core)와의 에러 경계

```
[kpubdata]                              [kpubdata-builder]
PublicDataError 계층                    BuildError 계층
  ├── TransportError              →       SourceExecutionError.__cause__
  ├── TransportTimeoutError       →       SourceExecutionError.__cause__
  ├── AuthError                   →       SourceExecutionError.__cause__
  ├── RateLimitError              →       SourceExecutionError.__cause__
  ├── ServiceUnavailableError     →       SourceExecutionError.__cause__
  ├── ParseError                  →       SourceExecutionError.__cause__
  ├── ProviderResponseError       →       SourceExecutionError.__cause__
  ├── DatasetNotFoundError        →       SourceExecutionError.__cause__
  ├── ConfigError                 →       SourceExecutionError.__cause__
  ├── InvalidRequestError         →       SourceExecutionError.__cause__
  ├── UnsupportedCapabilityError  →       SourceExecutionError.__cause__
  └── ProviderNotRegisteredError  →       SourceExecutionError.__cause__

Builder는 kpubdata 예외를 직접 surface하지 않음.
항상 BuildError 하위로 감싸서 전파.
cause.retryable, cause.provider 등 메타데이터는 SourceExecutionError에서 접근 가능.
```

## 8. 놓치기 쉬운 에러 시나리오

| 시나리오 | 대응 |
|:---|:---|
| 부분 데이터 수신 / 깨진 payload | `ParseError` → `SourceExecutionError` |
| API 키 mid-build 만료 | `AuthError` → 같은 provider 후속 source 중단 |
| 디스크 공간 부족 | `OSError` → `ExportError` / `ManifestWriteError` |
| Concurrent build manifest 충돌 | output_dir을 `build_id` 기준으로 분리 |
| YAML spec 파일 I/O 에러 | `OSError` → `SpecLoadError` |
| 미완성 adapter 호출 (seoul, airkorea) | `NotImplementedError` → `SourceExecutionError` |
| Concurrent build artifact 충돌 | output_dir을 `build_id` 기준으로 분리 (manifest뿐 아니라 artifact 경로도) |
| Exporter 실패 시 부분 파일 잔존 | staging 디렉토리 사용 → 실패 시 cleanup |
| `RateLimitError` quota 소진 (retryable=False) | `cause.retryable` 계승하여 재시도 여부 판단 |

## 9. 빌드 취소 (`cancelled`) 정책

빌드 취소는 예외 타입이 아닌 **상태**로 처리합니다.

- `BuildManifest.status`에 `"cancelled"` 값 허용
- 취소 시 진행 중인 source fetch를 중단하고, 수집된 결과까지만 manifest에 기록
- 취소된 빌드의 manifest: `status="cancelled"`, `errors`에 취소 사유 기록
- 예외 계층에 `BuildCancelledError`는 추가하지 않음 — 취소는 정상 흐름의 일부

> `partial` 상태는 v0.1에서는 사용하지 않습니다 (reserved for future use).
> v0.1에서 `status`는 `"succeeded"` | `"failed"` | `"cancelled"` 중 하나입니다.

## 10. Manifest 생성 보장 범위

"Manifest는 항상 생성"은 **best-effort** 원칙입니다.

- 정상 빌드: manifest 생성 보장
- 빌드 실패: manifest 생성 **시도** — 실패 정보를 기록
- `ManifestWriteError` 발생 시: manifest 자체를 쓸 수 없는 상황 (디스크 꽉 참 등)
  - 이 경우 manifest는 생성되지 않으며, `ManifestWriteError`가 최종 에러로 전파됨
  - CLI/Studio는 이 예외를 잡아 "빌드 결과를 기록할 수 없습니다" 메시지를 표시

---

## 관련 문서

| 문서 | 설명 |
|:---|:---|
| [ARCHITECTURE.md](../ARCHITECTURE.md) | 시스템 아키텍처 설계 |
| [API_CONTRACT.md](../API_CONTRACT.md) | API 인터페이스 규약 |
| [DOMAIN_MODEL.md](../DOMAIN_MODEL.md) | 도메인 모델 정의 |

### KPubData Product Family
| 저장소 | 문서 | 설명 |
|:---|:---|:---|
| [kpubdata](https://github.com/yeongseon/kpubdata) | exceptions.py | Core 예외 계층 정의 |
