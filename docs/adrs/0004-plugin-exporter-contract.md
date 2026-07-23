# ADR 0004 — Plugin Exporter API 계약 안정화

- 상태: 제안됨(Proposed)
- 관련 이슈: #311
- 관련 문서: [EXPORT_MODEL.md](../../EXPORT_MODEL.md), [ARCHITECTURE.md](../../ARCHITECTURE.md)

## 배경

AGENTS.md는 "exporter는 플러그형으로 유지할 것"을 기본 규칙으로 규정한다. 현재 exporter는 `exporters/base.py`의 `BaseExporter`를 상속하고 `export()`를 구현하는 방식으로 추가된다. BuildSpec의 `ExportTarget.kind`가 exporter 레지스트리 키로 사용된다.

## 문제

플러그형이라는 목표에 비해 다음이 명문화되어 있지 않다.

- **등록/발견(discovery)**: exporter가 어떻게 레지스트리에 등록되는가(내부 하드코딩 vs 동적 등록 vs entry points).
- **계약 안정성**: `export()` 시그니처·반환 규약의 버저닝과 하위호환 보증 범위.
- **실패 시맨틱**: 부분 출력/실패 시 manifest 반영 규약.
- **서드파티 확장**: 외부 패키지가 exporter를 제공할 수 있는 공개 계약인가.

## 결정 필요 사항

1. Exporter 등록 메커니즘.
2. `export()` 계약의 안정성 보증 및 버전 정책.
3. 실패/부분출력 시맨틱과 manifest 통합 규약.
4. 서드파티 exporter 지원 범위(공개 API vs 내부 확장점).

## 검토한 대안

### 대안 A — 내부 명시적 레지스트리(현행 유지)
- kind→클래스 매핑을 코드 내부 dict로 유지.
- 장점: 단순·결정적(AGENTS.md '결정적 동작 우선'과 정합), 발견 비용 0.
- 단점: 서드파티가 코어 수정 없이 추가 불가.

### 대안 B — Python entry points(`importlib.metadata`)
- `kpubdata_builder.exporters` 그룹으로 외부 패키지가 exporter 등록.
- 장점: 진정한 플러그인, 코어 수정 불필요.
- 단점: 동적 발견 → 결정성/보안 검토 필요(신뢰 경계).

### 대안 C — 하이브리드
- 코어 exporter는 명시적 레지스트리, 확장은 명시적 opt-in 등록 API(`register_exporter`) 제공. entry points는 후속.

## 권고 (제안)

**대안 C(하이브리드)를 제안**한다.

1. 코어 exporter(markdown/jsonl/parquet/csv/huggingface 등)는 명시적 레지스트리로 결정성 유지.
2. 공개 등록 API `register_exporter(kind, factory)`를 노출해 확장 지점을 문서화.
3. `export()` 계약을 EXPORT_MODEL.md에 안정 계약으로 명문화:
   - 입력: 레코드/메타데이터, 출력 base_dir(경로 안전은 `_path_safety`가 강제).
   - 반환: 생성된 `Artifact` 목록.
   - 실패: 예외 규약 + 부분출력 금지(원자성) 또는 명시적 partial 표기.
4. entry points 기반 자동 발견은 신뢰 경계 검토 후 후속 ADR로 분리.

## 영향

- `EXPORT_MODEL.md`에 exporter 계약 절 신설(현재 AGENTS.md의 간단 가이드를 정식 계약으로 승격).
- 신규 exporter 추가 시 골든 테스트 요구(AGENTS.md 체크리스트)와 연결.

## 미해결 질문

- `export()`가 스트리밍(대용량)에 대응해야 하는가, 전량 메모리 로드로 충분한가?
- exporter별 옵션 스키마 검증을 어디서 수행할지(validator vs exporter 내부)?
