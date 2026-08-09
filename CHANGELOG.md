# 변경 이력

## [Unreleased]

### 추가됨
- **인증 시스템 (B2-B5)**: Principal 추상화(#384), Google OIDC Bearer 검증(#385), 허용 목록 게이트(#386), API 계약 1.1.0 bearerAuth(#387)
- **인가 (C1/C2)**: manifest·BuildIndex에 principal 기록(#388), ENFORCE_OWNERSHIP 플래그로 run 소유권 강제(#389)
- **무인증 /healthz 엔드포인트** + Dockerfile HEALTHCHECK (#372)
- **SIGTERM 우아운 종료** + max_workers env/CLI 노출 (#374)
- **CORS Authorization 헤더** + 파일 응답 Origin 처리 (#382)
- **Dockerfile ARG EXTRAS** — publish extra 기본 포함 (#373)
- **dev-mode 환경변수 통일** KPUBDATA_BUILDER_DEV_MODE (#371)
- **request ID 추적** — X-Request-ID 헤더 + JSON body (#379)
- **Azure Bicep IaC** — ACA + Azure Files + Log Analytics (#378)
- **배포 가이드** docs/deploy.md (#390)
- **ADR 0008** 비동기 build job 모델 초안 (#334)
- **ADR 0009** 사용자 인증 Google OIDC (#383)
- **ADR 0010** ArtifactStore + 상태 백엔드 분리 (#375)
- HTTP 전송 계층 로그/예외 메시지에서 API 키 마스킹 (#260)
- 컨테이너 진입점 fail-closed 정책 (ADR 0006)

### 변경됨
- API 계약 버전 1.0.0 → 1.1.0 (bearerAuth 추가)
- BuildIndex 스키마 v2 → v3 (created_by 컬럼)
- BuildManifest에 created_by 필드 추가

### 제거됨
- .omc/state/sessions 스크래치 파일 추적 해제 (#380)
