# 로드맵 — kpubdata-builder

> kpubdata-builder는 **원시 공공데이터를 정제된, 검증된, 배포 가능한 데이터셋으로 변환하는 빌드 엔진**입니다.
> 원시 공공데이터 → Bronze → Silver → Gold → export → manifest → publish

## 개발 축

| 축 | 설명 |
| :--- | :--- |
| **Build Pipeline** | spec 파싱 → orchestrator → export → manifest 핵심 흐름 |
| **Medallion Pipeline** | Bronze/Silver/Gold stage 책임, 승격 규칙, run workspace 구조 |
| **Export & Publish** | 출력 형식(Markdown, JSONL, Parquet, HF layout) + 배포 대상(HF Hub, Kaggle, 로컬) |
| **Dataset Identity** | dataset card, schema summary, split, provenance, version history |

---

## v0.1

Medallion 파이프라인 기반 구축.

- BuildSpec 계약 안정화 (YAML 파싱, 검증)
- Medallion 디렉터리 재구성 (`stages/bronze`, `stages/silver`, `stages/gold`)
- Bronze/Silver/Gold stage 구현
- Polars 기반 tabular engine
- Pipeline orchestrator
- 서울 아파트 실거래가(`datago.apt_trade`) end-to-end 예제
- preview 계약 안정화
- manifest 스키마 안정화
- CLI 기반 build 실행 흐름 정리
- BuildError 에러 계층 정리
- spec loader, executor, assembler 에러 처리 강화

## v0.2

export 확장, stage-aware exporter, Dataset Identity 도입.

### Export & Publish
- Markdown exporter (#5)
- JSONL exporter (#6)
- Parquet exporter (#8)
- Hugging Face layout exporter (#9)
- stage-aware exporters (Gold package 기반 출력 최적화)
- Publish command — 로컬 → 원격 배포 (#10)

### Dataset Identity
- Manifest를 **dataset release record**로 승격 — build manifest writer (#7)
- Schema summary in manifest (#11)
- Richer provenance tracking (#12)
- **Dataset card 생성**: README / schema summary / sample preview / source attribution / license / version history

### Build Pipeline
- Build command and source execution (#4)
- Validate command (#2)
- Preview command (#3)
- Build spec parsing (YAML) (#1)

## v0.3

Plugin 생태계와 고급 빌드 기능.

- Plugin exporter API (#13)
- Reusable build templates (#14)
- Snapshot-aware builds (#15)
- Build diff/compare tools (#16)
- Exporter / Publisher 경계 분리 (#28)
- **Split 지원**: train/validation/test, by year/region/category, distribution-aware segmentation
- **Kaggle dataset export** 지원
- **Data catalog page** 생성 — 브랜드 배포용 정적 페이지

## v1.0 기준

- BuildSpec 계약 안정, 하위 호환성 보장
- 3개 이상 exporter 안정 (Markdown, JSONL, Parquet)
- 2개 이상 publish 대상 안정 (Hugging Face, Kaggle)
- Dataset card + manifest가 모든 빌드에 자동 생성
- Plugin exporter API로 외부 확장 가능
- kpubdata-studio에서 전체 워크플로우 제어 가능

---

## 출력 타깃

| 타깃 | 설명 | 버전 |
| :--- | :--- | :--- |
| Hugging Face datasets | ML/AI 친화적 데이터셋 배포 | v0.2 |
| Kaggle datasets | 데이터 분석/경진대회 배포 | v0.3 |
| CSV / Parquet package | 범용 데이터 패키지 | v0.2 |
| Data catalog page | 브랜드 배포용 정적 카탈로그 페이지 | v0.3 |

## Dataset Identity 개념

Builder가 생성하는 모든 데이터셋에는 다음 정보가 포함됩니다:

| 항목 | 설명 |
| :--- | :--- |
| source | 원본 데이터 출처 (provider, dataset, params) |
| build date | 빌드 실행 일시 |
| schema | 필드 정의 및 타입 |
| split info | 데이터 분할 정보 |
| validation result | 검증 결과 |
| export targets | 출력 형식 및 대상 |
| version | 데이터셋 버전 |

---

## 관련 문서

| 문서 | 설명 |
| :--- | :--- |
| [README.md](./README.md) | 프로젝트 포지셔닝 |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | 아키텍처 원칙 |
| [BUILD_SPEC.md](./BUILD_SPEC.md) | BuildSpec 계약 |
| [API_CONTRACT.md](./API_CONTRACT.md) | 서비스/API 계약 |
