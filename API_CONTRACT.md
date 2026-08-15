# API 계약 — KPubData Builder

## 1. 단일 소스

Builder HTTP wire 계약의 단일 소스는 [contract/builder-api.yaml](./contract/builder-api.yaml)입니다.

- endpoint, request body, response body, status code, security scheme은 OpenAPI 문서를 기준으로 합니다.
- `contract/builder-api.yaml`의 `info.version`은 `kpubdata_builder.service.API_CONTRACT_VERSION`과 일치해야 합니다.
- `tests/unit/test_service_contract.py`가 버전 일치, 정적 route/status 일치, 실제 `dispatch()` 응답의 wire-level conformance를 검증합니다.
- Studio 같은 소비자는 이 문서가 아니라 OpenAPI SSOT와 `GET /version`을 기준으로 호환성을 판단합니다.

이 문서는 사람이 읽는 운영 가이드입니다. wire 형태를 옮겨 적지 않습니다.

## 2. 실행 모델

v0.4 Builder service는 동기식 실행 모델을 유지합니다.

| 범위 | 모델 | 기준 |
| :--- | :--- | :--- |
| `/validate`, `/preview`, `/build`, 조회 계열 | 요청-응답 동기 처리 | ADR 0002 |
| 비동기 job 모델 | 후속 설계 범위 | Builder #334 |

원칙:

- `POST /build`는 현재 요청 안에서 파이프라인을 실행하고 성공/실패 결과를 반환합니다.
- `POST /builds` / `GET /builds/{run_id}` 같은 비동기 job 계약은 상태·취소·멱등성·부분 산출물 정책이 확정된 뒤 별도 ADR/이슈로 도입합니다.
- Medallion stage별 artifact/preview 조회가 필요해지면 OpenAPI SSOT에 stage-specific endpoint를 먼저 추가합니다.

## 3. 응답 정책

자세한 schema와 status code는 OpenAPI SSOT를 따릅니다. 정책 수준의 의미는 다음과 같습니다.

| 상황 | 정책 |
| :--- | :--- |
| 정상 요청 | endpoint별 성공 응답을 `200`으로 반환 |
| BuildSpec 파싱/로드 실패 | 클라이언트가 수정할 수 있는 입력 오류로 처리 |
| BuildSpec 검증 실패 | 문제 목록을 포함한 입력 오류로 처리 |
| preview source 실패 | HTTP 요청 자체는 성공할 수 있으며, source별 `status`/`error`로 실패를 표현 |
| build source 실패 | upstream/source 의존 실패로 처리하고 가능한 경우 manifest를 남김 |
| run별 BuildSpec 조회 | `GET /builds/{run_id}/spec`으로 redaction된 canonical YAML과 그 bytes의 digest를 반환 |
| 인증 실패 | `401`은 재인증 대상, `403`은 권한 요청 대상, `503`은 JWKS 일시 장애 대상 |

정책과 구현이 다르면 구현 각주를 늘리지 말고 다음 순서로 정리합니다.

1. 실제 의도한 계약이면 `contract/builder-api.yaml`을 수정합니다.
2. 구현 버그이면 service 코드와 conformance test를 수정합니다.
3. Studio 영향이 있으면 Studio 클라이언트/문서 PR을 별도로 엽니다.

### BuildSpec과 preview 계약 해석

- HTTP 요청의 `spec` 필드는 현재와 같이 YAML 문자열입니다. OpenAPI의 `BuildSpec`
  컴포넌트는 그 YAML이 표현하는 canonical 도메인 구조를 타입 생성기가 읽을 수 있게
  정의합니다.
- `metadata`, `sources[].params`, `exports[].options` 값은 표준 JSON 호환 범위입니다.
- source preview는 성공과 실패 모두 `source_key`, `status`, `error`, `schema`,
  `sample`, `total_rows`, `statistics`를 반환합니다. source 실패는 HTTP 200 안에서
  `status: failed`, 비어 있는 schema/sample, 0 기반 statistics, 문자열 `error`로
  표현합니다.
- 제거된 `transforms`, top-level `normalization_mode`,
  `sources[].normalization_mode`는 계약 필드가 아니며 파서가 명시적으로 거부합니다.
- 검증을 통과해 실행을 시작한 spec은 pipeline 진입 전에
  `{output_root}/{run_id}/buildspec.yaml`에 원자 저장됩니다. legacy run처럼 snapshot이
  없는 경우 API는 manifest에서 추측·복원하지 않고 unavailable `404`를 반환합니다.
- snapshot redaction은 범용 매핑의 명시적인 credential 키에만 적용됩니다. inline secret은
  `<redacted>`로 대체되므로 해당 snapshot만으로 credential이 필요한 실행을 그대로 재실행할
  수는 없으며, credential은 환경/서비스 설정에서 다시 공급해야 합니다.
- `spec_digest`는 원본 객체가 아니라 **실제로 저장된 redaction 후 canonical snapshot bytes**의
  SHA-256입니다. credential 값만 다른 두 spec은 의도적으로 같은 snapshot/digest가 될 수 있습니다.

## 4. 인증과 CORS

브라우저 클라이언트(Studio 등)와의 연동을 위해 CORS는 default-deny입니다.

- `KPUBDATA_BUILDER_ALLOWED_ORIGINS` 미설정 시 cross-origin 요청을 거부합니다.
- Same-origin 요청은 허용합니다.
- preflight는 허용 origin에 대해 `GET, POST, OPTIONS`와 `Content-Type, X-API-Key, Authorization`을 허용합니다.

인증은 두 경로를 지원합니다.

| 방식 | 헤더 | 용도 | 환경변수 |
| :--- | :--- | :--- | :--- |
| API Key | `X-API-Key: <secret>` | 서비스 계정, 스케줄 워크플로 | `KPUBDATA_BUILDER_API_KEY` |
| Bearer (OIDC) | `Authorization: Bearer <jwt>` | 사람 사용자, Studio | `OIDC_ISSUER` + `OIDC_AUDIENCE` |

OIDC는 `OIDC_ISSUER`/`OIDC_AUDIENCE`와 허용 목록(`OIDC_ALLOWED_HD`, `OIDC_ALLOWED_SUBJECTS`, `OIDC_ALLOWED_EMAILS`)이 설정된 경우에만 활성화됩니다.

## 5. CLI 대응 관계

CLI와 HTTP service mode는 같은 도메인 계약을 공유합니다.

| CLI | 대응 API operation |
| :--- | :--- |
| `kpubdata-builder validate spec.yaml` | `validateSpec` |
| `kpubdata-builder preview spec.yaml` | `previewBuild` |
| `kpubdata-builder build spec.yaml` | `createBuild` |

CLI와 HTTP 응답 의미가 갈라지면 OpenAPI와 service conformance test를 먼저 확인합니다.

## 6. Python API — BuilderService

Python 코드에서 직접 사용하는 경우 `BuilderService`를 통해 HTTP 없이 같은 service 로직을 호출할 수 있습니다.

```python
from pathlib import Path

from kpubdata_builder.service import BuilderService

service = BuilderService(
    output_root=Path("./dist"),
    client_factory=lambda: my_kpubdata_client,
)

validate_response = service.validate(spec_yaml_str)
build_response = service.build(spec_yaml_str, run_id="my-run-001")
```

반환 객체의 body shape도 OpenAPI SSOT와 conformance test 대상입니다.

## 7. 관련 문서

| 문서 | 역할 |
| :--- | :--- |
| [contract/builder-api.yaml](./contract/builder-api.yaml) | HTTP wire 계약 SSOT |
| [BUILD_SPEC.md](./BUILD_SPEC.md) | BuildSpec 입력 계약 |
| [BUILD_STATE.md](./BUILD_STATE.md) | build 상태 모델 |
| [BOUNDARY.md](./BOUNDARY.md) | Builder-Studio 경계 |
| [docs/adrs/0002-build-execution-model.md](./docs/adrs/0002-build-execution-model.md) | v0.4 동기 build 모델 결정 |
| [docs/adrs/0005-api-contract-single-source.md](./docs/adrs/0005-api-contract-single-source.md) | OpenAPI SSOT 결정 |
