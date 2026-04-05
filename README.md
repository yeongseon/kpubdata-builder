# KPubData Builder — Korea Public Data Builder

**KPubData Builder (Korea Public Data Builder)** is the dataset artifact pipeline
that sits on top of [`kpubdata`](https://github.com/yeongseon/kpubdata).

It turns normalized Korea public data records into publishable artifacts such as:

- Markdown datasets and reports
- Hugging Face datasets
- JSONL / Parquet / CSV exports
- Dataset cards and metadata manifests

## Product family

| Package | Role |
|---|---|
| [`kpubdata`](https://github.com/yeongseon/kpubdata) | Korea Public Data access + parsing + normalization core |
| [`kpubdata-builder`](https://github.com/yeongseon/kpubdata-builder) | Dataset assembly + export pipeline |
| [`kpubdata-studio`](https://github.com/yeongseon/kpubdata-studio) | Visual interface for authoring and running builds |

## 📖 문서 가이드 (Document Guide)

### 핵심 설계
- [ARCHITECTURE.md](./ARCHITECTURE.md): 시스템 아키텍처 및 레이어 설계
- [DOMAIN_MODEL.md](./DOMAIN_MODEL.md): 핵심 도메인 모델(BuildSpec, Artifact 등) 정의
- [EXPORT_MODEL.md](./EXPORT_MODEL.md): 데이터 변환 및 Exporter 구현 모델
- [API_CONTRACT.md](./API_CONTRACT.md): CLI 및 파이썬 인터페이스 규약

### 개발 가이드
- [AGENTS.md](./AGENTS.md): AI 에이전트를 위한 개발 규칙 및 가이드
- [CONTRIBUTING.md](./CONTRIBUTING.md): 프로젝트 기여 방법 및 개발 환경 설정

### 프로젝트 관리
- [PRD.md](./PRD.md): 제품 요구사항 및 목표 정의
- [ROADMAP.md](./ROADMAP.md): 향후 개발 계획 및 마일스톤
- [PLAN.md](./PLAN.md): 초기 구축 및 작업 계획

### 자세한 참고
- [docs/adrs/0001-builder-as-orchestrator.md](./docs/adrs/0001-builder-as-orchestrator.md): 빌더 아키텍처 결정 기록

---

## 📚 관련 문서

### 이 저장소 내 문서
| 문서 | 설명 |
| :--- | :--- |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | 시스템 아키텍처 설계 |
| [DOMAIN_MODEL.md](./DOMAIN_MODEL.md) | 도메인 모델 정의 |
| [EXPORT_MODEL.md](./EXPORT_MODEL.md) | 데이터 변환 모델 |
| [API_CONTRACT.md](./API_CONTRACT.md) | API 인터페이스 규약 |
| [AGENTS.md](./AGENTS.md) | 에이전트 개발 가이드 |
| [CONTRIBUTING.md](./CONTRIBUTING.md) | 프로젝트 기여 가이드 |
| [PRD.md](./PRD.md) | 제품 요구사항 정의서 |
| [ROADMAP.md](./ROADMAP.md) | 프로젝트 로드맵 |
| [PLAN.md](./PLAN.md) | 작업 실행 계획 |

### KPubData Product Family
| 저장소 | 문서 | 설명 |
| :--- | :--- | :--- |
| [kpubdata](https://github.com/yeongseon/kpubdata) | [ARCHITECTURE.md](https://github.com/yeongseon/kpubdata/blob/main/ARCHITECTURE.md) | Core 아키텍처 |
| [kpubdata-studio](https://github.com/yeongseon/kpubdata-studio) | [ARCHITECTURE.md](https://github.com/yeongseon/kpubdata-studio/blob/main/ARCHITECTURE.md) | Studio 아키텍처 |

