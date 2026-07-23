# Architecture Decision Records (ADR)

KPubData Builder의 주요 설계 결정을 기록합니다. 각 ADR은 배경·문제·검토한 대안·권고·영향을 담으며, 상태(제안됨/승인됨/대체됨)를 표기합니다.

| ADR | 제목 | 상태 | 관련 이슈 |
| :--- | :--- | :--- | :--- |
| [0001](./0001-builder-as-orchestrator.md) | 오케스트레이터로서의 Builder | 승인됨 | — |
| [0002](./0002-build-execution-model.md) | Build 실행 모델: 동기 vs 비동기(job) | 승인됨 | #308 |
| [0003](./0003-persistent-build-store.md) | 영속 Build 저장소: 파일시스템 스캔 대체 | 승인됨 | #309 |
| [0004](./0004-plugin-exporter-contract.md) | Plugin Exporter API 계약 안정화 | 승인됨 | #311 |
| [0005](./0005-api-contract-single-source.md) | API 계약 단일 소스 & 코드 생성 전략 | 승인됨 | #310 |
| [0006](./0006-service-auth-and-deployment.md) | 서비스 인증 & 배포(Docker) 스토리 | 승인됨 | #312 |

## 작성 규칙

- 파일명: `NNNN-kebab-title.md` (4자리 일련번호).
- 언어: 한국어(문서 정책).
- 상태 흐름: `제안됨(Proposed) → 승인됨(Accepted) → (필요시) 대체됨(Superseded)`.
- 이 목록은 v0.4 마일스톤(Beyond-MVP 통합, epic #313)의 설계 결정을 추적합니다.
