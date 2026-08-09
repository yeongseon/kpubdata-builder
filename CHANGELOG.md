# 변경 이력

## v0.4 (Unreleased)

### 추가됨
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
- API 계약 1.0.0 → 1.2.0 (/healthz + bearerAuth + /catalog + StructuredProblem)
- BuildIndex 스키마 v2 → v3 (created_by)
- BuildManifest에 created_by 필드
- ValidationError에 structured_problems 추가
- README 인증 서술 fail-closed 정책에 맞게 수정 (#423)

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
