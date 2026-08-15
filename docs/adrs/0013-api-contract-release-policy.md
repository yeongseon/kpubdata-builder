# ADR 0013 — API 계약 버전과 릴리스 경계 정책

- 상태: 승인됨(Accepted)
- 관련 이슈: #521, #484
- 관련 ADR: [ADR 0005](./0005-api-contract-single-source.md)
- 관련 문서: [API_CONTRACT.md](../API_CONTRACT.md), `contract/builder-api.yaml`

## 결정

1. `main`의 OpenAPI와 `API_CONTRACT_VERSION`은 항상 동일한 **stable SemVer**다. 서로 다른
   wire 계약을 같은 버전으로 병합하지 않는다.
2. wire 계약이 바뀌는 PR이 버전을 올린다. 문서, example, test만 바꾸고 wire가 같으면
   버전을 올리지 않는다.
3. Epic #484 완료를 vNext **release freeze/tag 경계**로 삼는다. 개발 중 생성된 minor
   버전을 다시 쓰거나 합치지 않고, freeze 시점의 최종 버전을 release manifest에 한 번 기록한다.
4. Studio는 Builder 버전과 exact equality를 요구하지 않는다. 같은 major인지 확인하고,
   사용하는 기능별 최소 SemVer를 충족하는지 판정한다. 더 높은 additive minor/patch는 허용한다.
5. Studio의 schema/client 갱신은 Builder가 minor를 올릴 때마다가 아니라 Studio가 새
   endpoint/field를 실제 소비할 때 수행한다.

## SemVer 규칙

| 변경 | 버전 | 예 |
| :--- | :--- | :--- |
| 기존 wire에 영향 없는 구현·문서·example·test | 유지 | 내부 refactor, OpenAPI example 추가 |
| 기존 의미를 바꾸지 않는 오류 수정 | patch | serializer가 문서대로 응답하지 않던 버그 수정 |
| endpoint, optional request field, response field/status 추가 | minor | `/query`, `startup_ms` 추가 |
| field 제거/rename/type 변경, 기존 의미 변경, endpoint 제거 | major | `problems: string[]`을 객체 배열로 교체 |

Additive response field는 기존 소비자가 알 수 없는 field를 무시할 수 있어야 한다. 기존 field를
필수에서 선택으로 낮추는 변경도 실제 소비자 의미가 달라지는지 검토하며, 애매하면 major로
취급한다.

## Pre-release 표기

현재 workflow에서는 `1.9.0-dev.N` 같은 pre-release를 사용하지 않는다.

- 기능 PR이 각각 `main`에 병합되는 구조에서 pre-release를 쓰면 `main`이 안정 계약이라는
  가정과 package/release 소비자의 SemVer 비교가 복잡해진다.
- 여러 기능을 같은 버전 문자열로 유지하면 그 버전만으로 wire 계약을 식별할 수 없다.
- 전용 integration branch와 자동 prerelease publish workflow를 도입하기 전에는 stable
  version을 순차 증가시키는 편이 더 결정적이다.

향후 전용 integration branch를 도입한다면 prerelease 번호도 계약 변경마다 증가시키고,
stable `main`에는 prerelease 계약을 병합하지 않는 별도 ADR이 필요하다.

## Studio 호환 판정

Studio는 하나의 전역 exact version 대신 기능별 최소 요구 버전을 가진다.

```text
Builder 1.10.0, Studio core 최소 1.6.0, query 최소 1.7.0
→ 같은 major이고 각 사용 기능의 최소 minor 이상이므로 허용

Builder 1.6.0에서 query 화면 진입
→ core 화면은 허용하되 query capability는 비활성화하고 필요한 최소 버전을 표시

Builder 2.0.0
→ major가 다르므로 기본적으로 차단하고 Studio schema/client 동기화 필요
```

호환 판정은 `server.major == required.major`와 `server >= required`를 모두 만족해야 한다.
버전 범위는 존재하지 않는 endpoint를 호출하기 위한 추측 수단이 아니다. Studio가 새 기능을
사용하려면 해당 기능의 최소 버전과 runtime response schema를 함께 추가해야 한다. 인증/네트워크
오류를 버전 불일치로 바꾸지 않는다.

## 계약 변경 PR 체크리스트

- [ ] `contract/builder-api.yaml`을 먼저 또는 같은 PR에서 수정
- [ ] `API_CONTRACT_VERSION`과 OpenAPI `info.version` 동시 변경
- [ ] additive/patch/breaking 분류와 근거를 PR에 기록
- [ ] route/status/wire conformance test 갱신
- [ ] request/response example이 있다면 schema·runtime fixture와 동기화
- [ ] Studio가 즉시 새 기능을 소비하면 Studio schema/client PR과 기능별 최소 버전 갱신
- [ ] Studio가 소비하지 않는 additive 변경이면 불필요한 exact-version PR을 만들지 않음

## Epic #484 릴리스 절차

1. #484에 포함된 contract PR을 모두 병합하고 OpenAPI 변경을 freeze한다.
2. Builder 전체 contract/conformance/example test를 통과시킨다.
3. 최종 `API_CONTRACT_VERSION`, OpenAPI commit SHA, Builder image/package version을 release
   manifest와 changelog에 기록하고 tag한다.
4. Studio는 실제 사용하는 operation의 schema/client와 기능별 최소 버전을 갱신한다.
5. Builder 직전 minor와 최종 minor를 대상으로 Studio 핵심 흐름 smoke/E2E를 수행한다.

freeze 이후 wire 변경은 같은 release에 몰래 추가하지 않고 새 SemVer 변경으로 처리한다.

## 영향

- Builder minor가 늘어나는 것 자체는 허용한다. 각 버전은 정확한 계약 식별자다.
- 릴리스/tag는 #484 완료 시 한 번 묶되 개발 중 contract honesty를 희생하지 않는다.
- Studio 동기화 비용은 exact 문자열 추적이 아니라 실제 capability 소비 시점에만 발생한다.
- major 변경은 두 저장소의 coordinated release가 필요하다.
