"""HTTP 서비스 인증 (#384 B2, #385 B3, ADR 0006/0009).

인증 결과를 ``bool`` 대신 ``Principal`` 로 반환해 "누가 요청했는가"를 보존한다 (B2).
본 모듈은 두 인증 경로를 통합한다 (B3, ADR 0009):

- ``X-API-Key`` — 서비스 계정(스케줄 워크플로 등 Google 로그인 불가 소비자).
- ``Authorization: Bearer <Google ID token>`` — 사람 사용자(Studio). JWKS로 오프라인 검증.

``OIDC_ISSUER`` 미설정 시 Bearer 경로가 비활성화되어 기존 배포에 영향이 없다.
설정 시 ``OIDC_AUDIENCE`` 가 필수이고 ``pyjwt`` extra가 설치되어야 한다 (fail-closed).
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass

# 서버가 요구하는 API 키. 환경변수로만 주입한다 (#248).
# ADR 0006에 따라 fail-closed로 동작: dev-mode 미설정 + API 키 미설정 시 인증을 거부한다.
_API_KEY_ENV = "KPUBDATA_BUILDER_API_KEY"
_DEV_MODE_ENV = "KPUBDATA_BUILDER_DEV_MODE"

# OIDC 설정 (ADR 0009, #385). OIDC_ISSUER 미설정 시 Bearer 비활성.
_OIDC_ISSUER_ENV = "OIDC_ISSUER"
_OIDC_AUDIENCE_ENV = "OIDC_AUDIENCE"
_OIDC_JWKS_URL_ENV = "OIDC_JWKS_URL"
_OIDC_JWKS_TTL_ENV = "OIDC_JWKS_TTL"
_DEFAULT_JWKS_TTL_SECONDS = 3600
_TOKEN_LEEWAY_SECONDS = 60


@dataclass(frozen=True)
class Principal:
    """인증된 요청 주체 (#384).

    인가(C1/C2)에서 run 소유권 판단에 쓰인다. ``kind`` 는 확장 가능 —
    ``'dev'``(로컬 개발), ``'service'``(X-API-Key), 향후 ``'oidc'``/``'user'``(Bearer, ADR 0009).
    식별자는 민감 값(원본 API 키 등)을 담지 않는다 — manifest created_by 등에는
    안전한 라벨(``apikey:<name>``)만 쓴다.
    """

    kind: str
    identifier: str | None = None


@dataclass(frozen=True)
class AuthError:
    """인증 실패. ``status_code`` 로 dispatch 응답 코드를 결정한다 (#385).

    기본 401(인증 거부). JWKS 조회 실패 등 일시적 인프라 장애는 503로 구분해
    클라이언트가 재시도를 구분하게 한다.
    """

    reason: str
    status_code: int = 401


def _is_dev_mode() -> bool:
    """로컬 개발 모드인지 확인한다 (#321, ADR 0006).

    KPUBDATA_BUILDER_DEV_MODE가 'true'/'1'이면 dev-mode로 간주하여 인증을 생략한다.
    프로덕션 배포에서는 이 환경변수를 설정하지 않아야 한다.
    """
    return os.environ.get(_DEV_MODE_ENV, "").lower() in ("true", "1")


def _verify_api_key(api_key: str | None) -> Principal | AuthError:
    """X-API-Key 경로 (B2). fail-closed: 키 미설정·불일치 → AuthError."""
    expected = os.environ.get(_API_KEY_ENV)
    if not expected:
        return AuthError(reason="api key not configured")
    if api_key is not None and hmac.compare_digest(api_key, expected):
        return Principal(kind="service")
    return AuthError(reason="invalid api key")


# --- OIDC Bearer (B3, ADR 0009) -------------------------------------------------
# JWKS 클라이언트는 지연 생성·캐시한다. OIDC 비활성 시 None.
_jwks_client: object | None = None
_jwks_url_cached: str | None = None


def _oidc_issuers() -> list[str]:
    raw = os.environ.get(_OIDC_ISSUER_ENV, "")
    return [s.strip() for s in raw.split(",") if s.strip()]


def _oidc_jwks_url() -> str:
    """JWKS URL. OIDC_JWKS_URL 우선, 없으면 첫 issuer의 well-known에서 유도."""
    explicit = os.environ.get(_OIDC_JWKS_URL_ENV, "").strip()
    if explicit:
        return explicit
    issuers = _oidc_issuers()
    # Google은 https://accounts.google.com 와 accounts.google.com 두 형태를 쓴다.
    issuer = issuers[0]
    if not issuer.startswith("http"):
        issuer = "https://" + issuer
    return issuer.rstrip("/") + "/.well-known/jwks.json"


def validate_oidc_config() -> None:
    """서버 기동 시 호출 (serve). OIDC 설정 오류면 RuntimeError (fail-closed, #385).

    - OIDC_ISSUER 미설정 → no-op (Bearer 비활성, 기존 배포 무영향).
    - OIDC_ISSUER 설정 + OIDC_AUDIENCE 미설정 → 거부.
    - pyjwt 미설치 → 거부 (``auth`` extra 필요).
    """
    if not _oidc_issuers():
        return
    if not os.environ.get(_OIDC_AUDIENCE_ENV, "").strip():
        raise RuntimeError(
            "OIDC_ISSUER is set but OIDC_AUDIENCE is missing; "
            "refusing to start (fail-closed, ADR 0009)."
        )
    try:
        import jwt  # noqa: F401
    except ImportError as e:  # pragma: no cover - dev 환경에서 extra 누락 시
        raise RuntimeError(
            "OIDC is enabled but pyjwt is not installed; install with: uv sync --extra auth"
        ) from e


def _get_jwks_client() -> object:
    """PyJWKClient 를 지연 생성·캐시한다."""
    global _jwks_client, _jwks_url_cached
    url = _oidc_jwks_url()
    if _jwks_client is None or _jwks_url_cached != url:
        from jwt import PyJWKClient

        ttl = int(os.environ.get(_OIDC_JWKS_TTL_ENV, "") or _DEFAULT_JWKS_TTL_SECONDS)
        _jwks_client = PyJWKClient(url, cache_jwk_set=True, lifespan=ttl)
        _jwks_url_cached = url
    return _jwks_client


def _verify_bearer_token(token: str) -> Principal | AuthError:
    """Google ID token을 JWKS로 오프라인 검증한다 (#385, ADR 0009).

    - RS256 고정 (alg:none / HS* 거부).
    - iss/aud/exp/nbf/iat 검증(60s leeway), email_verified 강제.
    - JWKS 조회 실패 → 503(401이 아님, 일시적 인프라 장애).
    """
    import jwt
    from jwt import PyJWKClientError

    try:
        client = _get_jwks_client()
        signing_key = client.get_signing_key_from_jwt(token)  # type: ignore[attr-defined]
    except (PyJWKClientError, ConnectionError):
        return AuthError(reason="auth service unavailable (jwks)", status_code=503)
    except Exception:
        return AuthError(reason="auth service unavailable (jwks)", status_code=503)

    audience = os.environ.get(_OIDC_AUDIENCE_ENV, "").strip()
    try:
        payload = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=audience,
            issuer=_oidc_issuers(),
            leeway=_TOKEN_LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "iss", "sub"]},
        )
    except jwt.PyJWTError as exc:
        return AuthError(reason=f"invalid token: {type(exc).__name__}")

    if not payload.get("email_verified", False):
        return AuthError(reason="email not verified")

    sub = str(payload.get("sub", ""))
    # 로그 추적용 식별자로 sub 앞 8자만(전체 sub 노출 최소화).
    return Principal(kind="oidc", identifier=sub[:8] if sub else None)


def authenticate(
    *, api_key: str | None = None, bearer_token: str | None = None
) -> Principal | AuthError:
    """요청을 인증해 ``Principal`` 또는 ``AuthError`` 를 반환한다 (B2/B3).

    우선순위:
    - dev-mode → ``Principal(kind='dev')`` (로컬 개발 편의).
    - Bearer 토큰 + OIDC 활성 → Bearer 검증 (사람 사용자).
    - 그 외 → X-API-Key 검증 (서비스 계정).

    OIDC 비활성 시 Bearer는 무시되어 기존 배포에 영향이 없다.
    """
    if _is_dev_mode():
        return Principal(kind="dev")

    if bearer_token and _oidc_issuers():
        if bearer_token.lower().startswith("bearer "):
            return _verify_bearer_token(bearer_token[7:].strip())
        return AuthError(reason="malformed authorization header")

    return _verify_api_key(api_key)


__all__ = ["AuthError", "Principal", "authenticate", "validate_oidc_config"]
