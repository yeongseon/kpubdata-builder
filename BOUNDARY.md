# Builder-Studio Boundary Rules — KPubData Builder

## 1. 목적

이 문서는 `kpubdata-builder`와 `kpubdata-studio` 사이의 책임 경계를 고정합니다. 목적은 중복 구현과 계약 드리프트를 막는 것입니다.

## 2. 핵심 원칙

1. **BuildSpec source of truth는 Builder입니다.**
2. **Preview logic는 Builder가 계산하고 Studio는 표시만 합니다.**
3. **Manifest schema는 Builder가 소유합니다.**
4. **Publish workflow는 Builder가 실행하고 Studio는 요청합니다.**
5. **Studio는 BuildSpec 계약이나 파이프라인 로직을 재정의할 수 없습니다.**

## 3. 책임 분리표

| 영역 | Builder 책임 | Studio 책임 |
| :--- | :--- | :--- |
| BuildSpec | 계약 정의, 버전 관리, 검증 | 작성 UI, 폼 입력, 저장/불러오기 보조 |
| Preview | source 샘플 실행, schema 계산, export preview 생성 | preview 결과 렌더링 |
| Build execution | 상태 전이, artifact 생성, manifest 기록 | 실행 요청, 상태 표시 |
| Manifest | 스키마 정의, 직렬화, 보관 정책 | manifest 표시, 링크 제공 |
| Publish | 원격 게시 수행, 성공/실패 기록 | publish 요청, 결과 표시 |

## 4. Studio가 해서는 안 되는 일

Studio는 다음을 구현하거나 소유하면 안 됩니다.

- 자체 BuildSpec 문법 정의
- Builder와 다른 별도 검증 규칙
- 독자적인 preview 계산 엔진
- manifest 스키마의 임의 확장/변형
- exporter/publisher 파이프라인 로직 재구현

## 5. 허용되는 Studio 역할

Studio는 다음 역할을 수행할 수 있습니다.

- BuildSpec 편집기 제공
- Builder API 호출 래퍼 제공
- 실행 상태 시각화
- artifact/manifest 브라우징
- publish 요청 UX 제공

단, 계산 결과의 기준은 항상 Builder 응답이어야 합니다.

## 6. 변경 관리 규칙

- BuildSpec 변경은 Builder 문서와 구현에서 먼저 정의합니다.
- Manifest 필드 변경은 Builder 릴리스 노트와 계약 문서에서 먼저 공지합니다.
- Studio는 Builder 계약 버전을 따라가며, 선행 독자 확장을 금지합니다.

## 7. 한 문장 요약

> Builder는 파이프라인 엔진이고, Studio는 그 엔진을 조작하는 외부 UI 클라이언트입니다.

## 8. 관련 문서

| 문서 | 설명 |
| :--- | :--- |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | 전체 레이어 구조 |
| [API_CONTRACT.md](./API_CONTRACT.md) | Builder 서비스 계약 |
| [BUILD_SPEC.md](./BUILD_SPEC.md) | Builder 소유 입력 계약 |
