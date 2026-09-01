# 변경 이력

## v0.4 (Unreleased)

### 추가됨
- **Dashboard aggregate contract (#488/#486 후속, additive)**: `GET /datasets` 응답에 `total`(canonical grouping + ownership 이후, pagination 이전의 접근 가능한 distinct dataset_id 개수) 추가 — Studio Home DATASETS KPI가 `datasets` 길이/limit을 total로 오인하지 않게 한다. 새 `GET /quality/summary?window=24h`는 최근 24h 안 접근 가능한 run의 structured quality를 `total_runs`/`evaluated_runs`/`pass_runs`/`warn_runs`/`fail_runs`로 요약(도메인 Quality의 bounded cross-run aggregate — 시스템 observability `/monitoring/*`와 분리). 두 조회 모두 ownership을 적용하며 unavailable/0-check run을 PASS로 세지 않는다. API 계약 1.21.0 → 1.22.0
- **Multi-source Join/Composition (#506)**: `BuildSpec.composition`(`CompositionSpec`/`JoinSpec`)으로 두 source의 검증된 Silver를 equi-join해 결합 Gold dataset(`gold/{composition.name}/`) 생성. alias 필수/중복 검증, join key 존재/dtype 일치 런타임 게이트, duplicate-key many-to-many 폭증 warn/fail 게이트, manifest `composition`(`CompositionProvenance`, additive)과 `POST /build` 응답 `composition` 키로 결합 결과를 source별 결과와 구분해 노출. API 계약 1.11.0 → 1.12.0
- **Built Dataset Catalog·Detail·Stage Summary API (#488)**: `GET /datasets`, `GET /datasets/{dataset_id}`, `GET /datasets/{dataset_id}/runs`로 `BuildSpec.dataset_id` 단위 grouping/latest run/run history 조회. `GET /builds/{run_id}/stages`, `GET /builds/{run_id}/stages/{stage}`로 source별 Bronze/Silver/Gold 상태와 안전한 summary/preview 조회
- **인증 시스템 (B2-B5)**: Principal 추상화(#384), Google OIDC Bearer 검증(#385), 허용 목록 게이트(#386), API 계약 bearerAuth(#387)
- **인가 (C1/C2)**: manifest·BuildIndex에 principal 기록(#388), ENFORCE_OWNERSHIP 플래그(#389)
- **BuildSpec 어시스턴트 (BL1-BL4)**: ADR 0011(#415), GET /catalog(#416), /validate problems 구조화(#417), API 1.2.0(#418)
- **무인증 /healthz** + Dockerfile HEALTHCHECK (#372)
- **SIGTERM 우아운 종료** + max_workers env/CLI (#374)
- **CORS Authorization 헤더** + 파일 응답 Origin (#382)
- **Dockerfile ARG EXTRAS** (#373)
- **Azure Bicep IaC** (#378)
- **배포 가이드** docs/deploy.md (#390)
- **request ID 추적** (#379)
- **ADR 0008** 비동기 job 모델 (#334)
- **ADR 0009** 사용자 인증 Google OIDC (#383)
- **ADR 0010** ArtifactStore + 상태 백엔드 (#375)
- **ADR 0011** BuildSpec 어시스턴트 그라운딩 (#415)
- **환경변수 대조 테스트** (#424)
- 컨테이너 진입점 fail-closed (ADR 0006)

### 변경됨
- API 계약 1.21.0 → 1.22.0 (`GET /datasets`에 `total` 추가, `GET /quality/summary` 추가, additive, #488/#486 후속)
- README에 Provider credential store(`KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY`) 운영 절 추가 — master key 필수/재사용/rotation 시 기존 credential 복호화 불가, 503(store 미구성)과 `configured:false`(미등록) 구분, secret 비노출 원칙
- API 계약 1.0.0 → 1.2.0 (/healthz + bearerAuth + /catalog + StructuredProblem)
- API 계약 1.4.0 → 1.5.0 (Dataset Catalog·Detail·Stage Summary API 추가, additive, #488)
- BuildIndex 스키마 v2 → v3 (created_by)
- BuildIndex 스키마 v3 → v4 (dataset_id 파생 검색 컬럼, #488)
- BuildManifest에 created_by 필드
- ValidationError에 structured_problems 추가
- README 인증 서술 fail-closed 정책에 맞게 수정 (#423)

### 수정됨
- `stages/_path_safety.ensure_within`이 Windows에서 여러 source를 병렬(ThreadPoolExecutor)로 빌드할 때 간헐적으로 traversal 오탐하던 버그 수정 — `root`/`target` 중 한쪽만 `Path.resolve()`의 `\\?\` 확장 프리픽스를 얻는 비대칭이 원인 (#506 조사 중 발견, composition과 무관한 기존 버그)

### 제거됨
- .omc/state/sessions 추적 해제 (#380)
- PLAN.md를 .github/로 이동 (#425)

## v0.3

Plugin 생태계와 고급 빌드 기능.

- Plugin exporter API — register_exporter_factory/instance (#310, ADR 0004)
- Exporter / Publisher 경계 분리 (#28)
- Split 지원 (train/validation/test, by key)
- Kaggle dataset export
- Snapshot-aware builds (#15)
- Build diff/compare tools (#16)
- Reusable build templates (#14)

## v0.2

Export 확장, Dataset Identity, CLI build 실행.

- Markdown / JSONL / Parquet / HuggingFace layout exporter
- stage-aware exporters (Gold 기반)
- Publish command (#10)
- Manifest를 dataset release record로 승격 (#7)
- Schema summary in manifest (#11)
- Provenance tracking (#12)
- Dataset card 생성
- Build / Validate / Preview CLI command (#1-4)
- Polars 기반 tabular engine
- 서울 아파트 실거래가 end-to-end 예제

## v0.1

Medallion 파이프라인 기반 구축.

- BuildSpec 계약 안정화 (YAML 파싱, 검증)
- Medallion 디렉터리 구조 (stages/bronze, stages/silver, stages/gold)
- Bronze/Silver/Gold stage 구현
- Pipeline orchestrator
- BuildError 에러 계층
- manifest 스키마 안정화
