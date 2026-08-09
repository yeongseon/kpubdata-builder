"""HTTP 서비스 인증 (#384, ADR 0006).

인증 결과를 ``bool`` 대신 ``Principal`` 로 반환해 "누가 요청했는가"를 보존한다.
이는 인가(C1/C2 — run 소유권) 도입의 토대다. 본 모듈은 인증 게이트만 담당하며,
허용 목록·Bearer(OIDC) 검증은 ADR 0009 이후 확장한다.

외부 동작은 기존 ``_verify_api_key`` 와 동일하다 — 동일 환경변수, 동일 fail-closed 정책.
"""

from __future__ import annotations

import hmac
import os
from dataclasses import dataclass

# 서버가 요구하는 API 키. 환경변수로만 주입한다 (#248).
# ADR 0006에 따라 fail-closed로 동작: dev-mode 미설정 + API 키 미설정 시 인증을 거부한다.
_API_KEY_ENV = "KPUBDATA_BUILDER_API_KEY"
_DEV_MODE_ENV = "KPUBDATA_BUILDER_DEV_MODE"


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
    """인증 실패 (#384). ``dispatch`` 가 401로 변환한다."""

    reason: str


def _is_dev_mode() -> bool:
    """로컬 개발 모드인지 확인한다 (#321, ADR 0006).

    KPUBDATA_BUILDER_DEV_MODE가 'true'/'1'이면 dev-mode로 간주하여 인증을 생략한다.
    프로덕션 배포에서는 이 환경변수를 설정하지 않아야 한다.
    """
    return os.environ.get(_DEV_MODE_ENV, "").lower() in ("true", "1")


def authenticate(api_key: str | None) -> Principal | AuthError:
    """요청을 인증해 ``Principal`` 또는 ``AuthError`` 를 반환한다 (#384, ADR 0006).

    기존 ``_verify_api_key -> bool`` 을 대체하되 주체 정보를 보존한다.
    fail-closed 정책은 동일:

    - dev-mode인 경우: ``Principal(kind='dev')`` 로 인증 생략 (로컬 개발 편의).
    - dev-mode가 아닌 경우:
      - API 키가 설정되어 있고 일치하면 ``Principal(kind='service')``.
      - 키 미설정 또는 불일치면 ``AuthError`` (거부).
    """
    if _is_dev_mode():
        return Principal(kind="dev")

    expected = os.environ.get(_API_KEY_ENV)
    if not expected:
        # fail-closed: dev-mode 미설정 + API 키 미설정 시 인증 거부
        return AuthError(reason="api key not configured")
    if api_key is not None and hmac.compare_digest(api_key, expected):
        return Principal(kind="service")
    return AuthError(reason="invalid api key")


__all__ = ["AuthError", "Principal", "authenticate"]
