# ADR 0012: 사용자별 Provider Credential 저장과 Client 격리

- 상태: 승인됨
- 관련 이슈: #492, #505, `yeongseon/kpubdata#263`

## 배경

Studio의 Provider 설정은 인증된 사용자별 credential CRUD와 Preview/Build/Test의
일관된 credential 사용을 요구한다. Builder는 #505의 stable `owner_id`를 이미
canonical ownership identity로 사용한다. kpubdata는 공개 `Client(provider_keys=...)`
주입 지점을 제공하지만 credential 저장이나 사용자 ownership은 책임지지 않는다.

## 결정

Builder가 `(owner_id, provider)`를 key로 credential을 저장한다. 저장소 abstraction은
원문을 AES-256-GCM으로 암호화하고 DB에는 ciphertext만 기록한다. master key는
`KPUBDATA_BUILDER_CREDENTIAL_MASTER_KEY` 환경변수로 별도 주입하며 credential DB에
기록하지 않는다.

Credential 해석 우선순위는 다음으로 고정한다.

1. 현재 principal의 저장 credential
2. 서버의 kpubdata 기본 credential 환경설정
3. not configured

Preview, Build, Provider Test는 같은 resolver를 사용한다. 요청마다 새 kpubdata
Client를 만들고 닫으며 credential-sensitive Client나 response cache를 전역 공유하지
않는다. Builder는 kpubdata provider 로직을 복제하지 않고 공개 `provider_keys`, runtime
catalog, 구조화된 예외만 사용한다.

API는 raw credential과 `owner_id`를 반환하지 않는다. credential GET/PUT 응답은
configured, 고정 masked 값, updated_at 등 metadata만 포함한다. Provider failure는
`auth|network|timeout|provider|unknown`으로 제한해 반환하고 원문 예외 메시지는
응답하지 않는다. response code는 kpubdata 구조화 예외가 제공할 때만 노출한다.

## 결과

- cross-user credential 조회와 client 재사용을 구조적으로 차단한다.
- master key 미설정 시 credential CRUD는 fail-closed(503)하지만 기존 비-credential
  Builder API와 서버 기본 credential은 계속 사용할 수 있다.
- OAuth consent, 조직 공유 credential, arbitrary Base URL override는 이 결정의 범위가
  아니다.
