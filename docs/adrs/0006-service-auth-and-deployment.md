# ADR 0006 — 서비스 인증 & 배포(Docker) 스토리

- 상태: 승인됨(Accepted)
- 관련 이슈: #312, #237, #248, #253
- 관련 문서: [API_CONTRACT.md](../../API_CONTRACT.md), [ARCHITECTURE.md](../../ARCHITECTURE.md)

## 결정 (승인됨)

권고안을 **수정 채택**한다. 정적 API 키 유지는 OK이나, 배포/보안 기본값을 **fail-closed**로 강제한다.

1. **Docker는 fail-closed**로 동작한다: API 키가 설정되지 않았거나 명시적 dev-mode 플래그가 없으면 **기동을 거부하거나 인증 없는 요청을 차단**한다. '키 미설정=인증 생략'은 로컸 개발 편의 전용이며 컨테이너에 누출되지 않게 한다.
2. **CORS는 default-deny**: `KPUBDATA_BUILDER_ALLOWED_ORIGINS` env로 명시적 허용 오리진만 허가. `X-API-Key` 사용을 위한 **`OPTIONS` 프리플라이트 처리** 필요.
3. **아티팩트 서빙 경로 안전 검토**: `GET /artifacts/{run_id}` 경로 트래버설(path traversal)·MIME 처리를 점검한다.
4. Dockerfile(`uv sync --no-sources`, 진입점 `serve`)는 권고대로 채택. compose·키 로테이션·스코프는 후속 이슈.

> 근거: 보안 기본값은 안전 측(fail-closed, default-deny)으로 두어야 운영 사고를 막는다. 시퀀싱상 0005 다음(0006)으로 수행한다.

## 배경

Builder HTTP 서비스는 구현되어 있다(#237 해결). `service/http.py`의 `BoundedThreadingHTTPServer`(동시성 상한, #253)와 `cli.py serve` 커맨드로 실제 서빙이 가능하다.

인증도 **이미 부분 구현**되어 있다. `service/app.py`의 `_verify_api_key`가 `X-API-Key` 헤더를 `KPUBDATA_BUILDER_API_KEY` 환경변수와 `hmac.compare_digest`로 비교한다(#248). 환경변수 미설정 시 인증을 건너뛴다(로컬 개발 편의).

## 문제

운영(로컬 개발 이상)으로 가기 위한 다음이 정의되어 있지 않다.

- **배포 아티팩트 부재**: Dockerfile/compose가 없어 재현 가능한 배포가 어렵다.
- **CORS/오리진 정책**: Studio(브라우저)↔Builder 연동 시 오리진 정책이 명문화되지 않음.
- **인증 정책 성숙도**: 단일 정적 API 키만 지원. 키 로테이션/다중 소비자/스코프는 미정.
- **설정 표면**: 포트/바인드 주소/출력 루트/동시성 상한 등 운영 설정의 표준 규약 부재.

## 결정 필요 사항

1. 배포 아티팩트: Dockerfile(+ 선택적 compose)와 설정 규약(env/포트/볼륨).
2. CORS/오리진 정책(Studio 연동 전제).
3. 인증 모델의 목표 수준(정적 키 유지 vs 확장).

## 검토한 대안

### 인증
- **A. 현행 정적 API 키 유지**: 단순, 신뢰 네트워크/단일 소비자에 충분. 로테이션/스코프 없음.
- **B. 키 세트 + 스코프**: 다중 소비자·로테이션 지원. 복잡도 증가.
- 권고: v0.4는 **A 유지 + 문서화**(프로덕션 배포 시 키 필수 명시), B는 후속.

### 배포
- **A. Dockerfile 단일 이미지**: `uv sync --no-sources`로 PyPI kpubdata 설치, `serve` 진입점. 권고.
- **B. compose(빌더+스튜디오)**: 통합 데모/E2E에 유용. 후속 선택.

### CORS
- **A. 허용 오리진 환경변수(`KPUBDATA_BUILDER_ALLOWED_ORIGINS`)**: 기본은 동일 오리진/미설정 시 비허용. 권고.

## 권고 (제안)

v0.4 범위로 다음을 제안한다.

1. **Dockerfile** 추가: `uv sync --no-sources` 기반, 진입점 `kpubdata-builder serve`, 설정은 env(`KPUBDATA_BUILDER_API_KEY`, 포트, 출력 루트)로 주입.
2. **CORS 정책**을 허용 오리진 env로 도입하고 API_CONTRACT.md/README에 문서화.
3. **인증**은 현행 정적 키 유지 + "프로덕션에서 키 필수" 운영 가이드 명문화(코드 주석 #248 내용을 문서로 승격).
4. compose·키 로테이션·스코프는 별도 후속 이슈로 분해.

## 영향

- 신규 `Dockerfile`(+ 선택 `docker-compose.yml`), README '배포' 절 추가.
- `service/app.py`/`http.py`에 CORS 헤더 처리 추가(구현은 후속 이슈).
- Studio E2E(kpubdata-studio#104)가 컨테이너화된 Builder를 대상으로 실행 가능.

## 미해결 질문

- 이미지 베이스(경량 python-slim vs distroless)와 kpubdata 버전 핀 정합(#213)?
- CORS를 서비스 계층에서 처리할지, 배포 시 리버스 프록시에 위임할지?
