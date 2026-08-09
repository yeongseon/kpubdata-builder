# 배포 가이드

Builder HTTP 서비스를 로컬 개발 이상으로 운영하기 위한 배포·인증 스토리. 본 문서는 [ADR 0006](./adrs/0006-service-auth-and-deployment.md)(인증·배포)과 ADR 0009(사용자 인증, PR #398)의 운영 가이드를 통합한다.

> **상태**: ADR 0009는 제안됨(Proposed). 인증(Bearer) 구현은 B3(#385)/B4(#386) 진행 중이며, 본 문서의 Bearer 관련 절은 구현 완료 후 적용된다. 컨테이너 배포(fail-closed, HEALTHCHECK)는 이미 구현되었다.

## 1. 기본 배포 형태 — internal ingress

**Builder는 공개 인터넷 인그레스를 갖지 않는다** (ADR 0009 결정 3). Studio가 같은 네트워크/VPC에서 호출하는 구조가 기본형이다. 공격면을 최소화하고, 허용 목록을 심층 방어로 둔다.

- ACA(Azure Container Apps) / K8s에서 Builder 서비스를 클러스터 내부 서비스로 노출.
- Studio(정적 SPA)는 같은 네트워크에서 Builder를 호출. 외부 인터넷은 Studio 프론트만 접근.

## 2. 인증 — 두 경로 병행

| 소비자 | 인증 | 비고 |
| :--- | :--- | :--- |
| 스케줄 워크플로(데이터 갱신) | `X-API-Key` | Google 로그인 불가 → 서비스 키 병행 유지 |
| Studio(사람 사용자) | `Authorization: Bearer <Google ID token>` | ADR 0009, ADR 0006의 "다중 소비자" 후속 |
| 로컬 개발 | `KPUBDATA_BUILDER_DEV_MODE=1` | 컨테이너 외부에서만 (fail-closed) |

두 경로 모두 `Principal`(`service`/`oidc`/`dev`)로 정규화된다 (B2/#384).

## 3. Google OAuth client 설정

1. Google Cloud Console → APIs & Services → Credentials → **OAuth client ID**.
2. **Web application** 타입으로 생성.
3. **Authorized JavaScript origins**에 Studio 오리진 등록:
   - 로컬: `http://localhost:5173`
   - 실배포: `https://<studio-host>`
4. Client ID는 `VITE_GOOGLE_CLIENT_ID`로 Studio 빌드에 주입. **Client ID는 public 값**이라 번들 포함 무방.

> **절대 금지**: Builder API 키(`KPUBDATA_BUILDER_API_KEY`)를 `VITE_*` 환경변수로 Studio에 주입하지 말 것. `VITE_*`는 빌드 타임에 번들에 **평문으로 박힌다**. Studio는 토큰을 발급받지 않고(서버가 없으므로) Builder가 직접 Bearer를 검증한다 (ADR 0009).

## 4. 오리진 정합 — Google Console ↔ Builder CORS

**같은 오리진 목록**을 양쪽에 등록해야 한다:

- **Google Console**: Authorized JavaScript origins (§3)
- **Builder**: `KPUBDATA_BUILDER_ALLOWED_ORIGINS` 환경변수 (CORS default-deny, `service/http.py`)

두 값이 어긋나면 증상이 **CORS 오류**로 나타나 원인 추적이 어렵다. 로컬과 실배포 오리진을 모두 양쪽에 등록할 것.

> GitHub Pages 데모(`https://yeongseon.github.io`)는 mock 모드(`VITE_USE_REAL_BUILDER` 미설정)라 Builder를 호출하지 않으므로 등록 대상이 아니다.

## 5. SPA 토큰 보관

- Studio는 토큰을 **메모리(zustand 스토어, persist 미들웨어 없음)**에만 보관한다.
- `localStorage`/`sessionStorage`에 토큰을 쓰지 않는다 — XSS 한 번이면 탈취된다.
- 새로고침 시 재로그인(GIS 자동 로그인으로 마찰 완화).

## 6. 상태 저장 제약 — 단일 replica 전제

산출물(artifacts)·완료 이력(`BuildIndex`, ADR 0003)이 로컬 FS/SQLite에 고정되어 있어 **replica를 2개 이상 띄울 수 없다** (ADR 0010/#375). 배포 시:

- `minReplicas: 1, maxReplicas: 1` 고정.
- `/data` 볼륨(Azure Files 등) 마운트 필수 — 산출물·인덱스 영속화.
- **Azure Files 위에 SQLite를 올리지 말 것** — 파일 잠금이 불안정하다 (ADR 0010 §5).

멀티 replica는 ADR 0010(`ArtifactStore` 추상화 + 백엔드 분리) 이행 후 가능하다.

## 7. 헬스체크·종료

- `GET /healthz` — 무인증 liveness probe (#372). 프로브가 API 키를 못 실을 때 사용.
- `Dockerfile` `HEALTHCHECK` — urllib로 `/healthz` 폴링 (#372).
- `SIGTERM` — 우아운 종료(진행 중 요청 drain, #374). ACA/K8s 롤링 업데이트 대응.

## 관련

- [ADR 0006](./adrs/0006-service-auth-and-deployment.md) — 인증·배포(fail-closed, Docker)
- ADR 0009(PR #398) — 사용자 인증(Google OIDC, 제안됨)
- ADR 0010(PR #399) — 상태 백엔드 분리(제안됨)
- [API_CONTRACT.md](./API_CONTRACT.md) — `/healthz`, 401/403/503 응답
