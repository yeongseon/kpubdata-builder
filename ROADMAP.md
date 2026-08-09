# 로드맵 — kpubdata-builder

> kpubdata-builder는 **원시 공공데이터를 정제된, 검증된, 배포 가능한 데이터셋으로 변환하는 빌드 엔진**입니다.

## 개발 축

| 축 | 설명 |
| :--- | :--- |
| **Build Pipeline** | spec 파싱 → orchestrator → export → manifest |
| **Medallion Pipeline** | Bronze/Silver/Gold stage, 승격 규칙 |
| **Export & Publish** | 출력 형식 + 배포 대상 |
| **Service** | HTTP 서비스 모드, 인증, 카탈로그 |

---

## v0.1 ✅ 완료

Medallion 파이프라인 기반 구축.

- ✅ BuildSpec 계약 안정화 (YAML 파싱, 검증)
- ✅ Medallion 디렉터리 재구성 (stages/bronze, silver, gold)
- ✅ Bronze/Silver/Gold stage 구현
- ✅ Polars 기반 tabular engine
- ✅ Pipeline orchestrator
- ✅ manifest 스키마 안정화

## v0.2 ✅ 완료

Export 확장, Dataset Identity, CLI.

- ✅ Markdown / JSONL / Parquet / HuggingFace layout exporter
- ✅ stage-aware exporters (Gold 기반)
- ✅ Publish command — 로컬 → 원격 배포
- ✅ Manifest를 dataset release record로 승격
- ✅ Schema summary + provenance tracking
- ✅ Build / Validate / Preview CLI command

## v0.3 ✅ 완료

Plugin 생태계와 고급 빌드 기능.

- ✅ Plugin exporter API (register_exporter_factory/instance, ADR 0004)
- ✅ Split 지원 (train/validation/test, by key)
- ✅ Kaggle dataset export
- ✅ Snapshot-aware builds
- ✅ Build diff/compare tools

## v0.4 🔧 진행 중

인증, 카탈로그, BuildSpec 어시스턴트.

- ✅ 컨테이너 배포 — Dockerfile fail-closed, HEALTHCHECK, SIGTERM
- ✅ 인증 B2-B5 — Principal, Google OIDC Bearer, 허용 목록, 계약 1.1.0
- ✅ 인가 C1/C2 — manifest principal 기록, ENFORCE_OWNERSHIP
- ✅ BuildSpec 어시스턴트 BL1-BL4 — GET /catalog, problems 구조화, 계약 1.2.0
- ✅ ADR 0008 (async job), 0009 (auth), 0010 (state backend), 0011 (assistant)
- ✅ Azure Bicep IaC, 배포 가이드, request ID 추적
- 🔲 비동기 job 모델 구현 (#334, ADR 0008 제안됨)

## v1.0 기준

- ✅ BuildSpec 계약 안정
- ✅ 4개 exporter 안정 (Markdown, JSONL, Parquet, HuggingFace)
- ✅ 2개 publish 대상 (Hugging Face, Kaggle)
- ✅ Dataset card + manifest 자동 생성
- ✅ Plugin exporter API로 외부 확장 가능
- 🔲 kpubdata-studio에서 전체 워크플로우 제어

---

## ADR 인덱스

| ADR | 제목 | 상태 |
| :--- | :--- | :--- |
| 0001 | 오케스트레이터로서의 Builder | 승인됨 |
| 0002 | Build 실행 모델 | 승인됨 |
| 0003 | 영속 Build 저장소 (SQLite) | 승인됨 |
| 0004 | Plugin Exporter API | 승인됨 |
| 0005 | API 계약 단일 소스 | 승인됨 |
| 0006 | 서비스 인증 & 배포 | 승인됨 |
| 0007 | kpubdata 버전 호환성 | 승인됨 |
| 0008 | 비동기 build job 모델 | 제안됨 |
| 0009 | 사용자 인증 Google OIDC | 제안됨 |
| 0010 | ArtifactStore + 상태 백엔드 | 제안됨 |
| 0011 | BuildSpec 어시스턴트 그라운딩 | 제안됨 |
