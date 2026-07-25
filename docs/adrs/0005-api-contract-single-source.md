# ADR 0005 — API 계약 단일 소스 & 코드 생성 전략

- 상태: 승인됨(Accepted)
- 관련 이슈: #311, #209, #241, kpubdata-studio#85, kpubdata-studio#103
- 관련 문서: [API_CONTRACT.md](../../API_CONTRACT.md), `contract/builder-api.yaml`

## 결정 (승인됨)

권고안(대안 B, OpenAPI SSOT + 점진 도입)을 **조건부 채택**한다. `contract/builder-api.yaml`을 SSOT로 승격하되, 다음을 강제한다.

1. **SSOT는 구현된 엔드포인트만 포함**한다. 계획된(async 등) 엔드포인트는 codegen·커버리지 테스트에서 제외한다(ADR 0002와 정합).
2. **1단계**: 경로/메서드 커버리지 대조 테스트(yaml operation ↔ `dispatch` 양방향). **이후 단계**에서 상태코드 + 응답 스키마 대조까지 확장.
3. **인증 스키마를 OpenAPI에 표현**한다(`X-API-Key` securityScheme). 이미 구현된 `GET /builds`·인증을 yaml/API_CONTRACT.md에 반영(#241 잔여).
4. **Studio zod 검증은 오류 응답(에러 바디)도 검증**해야 한다(#103). Builder는 yaml만 안정적으로 유지.
5. 프레임워크 교체(대안 C)는 채택하지 않는다. 표준 라이브러리 `http.server` 기반의 결정성·무외부의존을 유지한다.

> 근거: SSOT가 호출 가능한 진실과 일치해야(contract honesty) 드리프트를 CI에서 차단할 수 있다. 이 ADR이 시퀀싱상 가장 먼저(0005 → 0006 → 0004 → 0003 → 0002) 수행된다.

## 배경

Builder API는 세 곳에서 표현된다.

1. 서버 라우팅: `service/app.py:dispatch`
2. 계약 문서: `contract/builder-api.yaml`(OpenAPI) + `API_CONTRACT.md`
3. 클라이언트: Studio `src/shared/lib/builderApi.ts`

현재도 일부 정합 장치가 존재한다. `contract/builder-api.yaml`의 `info.version`과 코드의 `API_CONTRACT_VERSION`이 일치해야 하며 이를 `test_service_contract`가 강제한다(API_CONTRACT.md §8).

## 문제

버전 문자열 정합은 있으나, **엔드포인트/스키마 수준의 드리프트**는 자동으로 잡히지 않는다.

- 이미 구현된 `GET /builds`(#250), `X-API-Key` 인증(#248)이 문서/계약에 반영되지 않을 수 있다(#241).
- Studio 클라이언트는 응답을 `return parsed as T`로 캐스팅만 하고 런타임 검증이 없다(kpubdata-studio#103).
- 세 표현이 수작업으로 유지되어 드리프트가 반복된다.

## 결정 필요 사항

1. 단일 진실원천(SSOT) 선정: `contract/builder-api.yaml`을 소스로 삼을지.
2. 서버 라우트가 SSOT를 만족하는지 검증하는 계약 테스트 범위(버전 → 경로/스키마로 확장).
3. Studio 타입/클라이언트 생성(codegen) 채택 여부.
4. CI 게이트 배치.

## 검토한 대안

### 대안 A — 문서 우선(수작업) 유지
- 장점: 단순. 단점: 드리프트 반복, 사람이 놓침.

### 대안 B — OpenAPI를 SSOT로, 양방향 검증
- 서버: 라우트/응답이 yaml 스키마를 만족하는지 스키마 대조 테스트.
- 클라이언트: `openapi-typescript` 등으로 Studio 타입 생성 + zod 스키마 파생(#103과 결합).
- 장점: 드리프트를 CI에서 차단, 3-레포 정합(#85).
- 단점: 초기 도구 도입 비용, 생성물 관리.

### 대안 C — 코드 우선(FastAPI 등으로 스키마 자동 생성)
- 현재는 표준 라이브러리 `http.server` 기반이므로 프레임워크 교체가 선행되어야 함 → 범위 과대.

## 권고 (제안)

**대안 B를 제안**하되 점진 도입한다.

1. **1단계(계약 테스트 확장)**: `test_service_contract`를 버전 일치 → **경로/메서드 커버리지 대조**까지 확장(yaml에 선언된 모든 operation이 `dispatch`에 존재하고 그 역도 성립). → #209 핵심.
2. **2단계(문서 갱신)**: 구현된 `GET /builds`·인증을 yaml/API_CONTRACT.md에 반영(#241 잔여).
3. **3단계(클라이언트 codegen)**: Studio에서 yaml→타입 생성 + zod 런타임 검증 도입(#103, #85). Builder는 yaml만 안정적으로 유지.

프레임워크 교체(대안 C)는 채택하지 않는다 — 표준 라이브러리 기반의 결정성·무의존성을 유지한다.

## 영향

- `contract/builder-api.yaml`이 명시적 SSOT로 승격.
- CI에 경로/스키마 대조 게이트 추가.
- Studio 클라이언트 신뢰성 향상(#103)과 3-레포 계약 정합(#85)이 이 SSOT에 수렴.

## 미해결 질문

- 스키마 대조를 순수 파이썬(경량)으로 할지, `openapi-core` 등 검증 라이브러리를 도입할지?
- codegen 산출물을 Studio 레포에 커밋할지, 빌드 타임 생성할지?
