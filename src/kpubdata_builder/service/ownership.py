"""run/dataset 소유권 판정의 단일 canonical 구현 (#389/#505).

``service.app`` / ``service.datasets`` / ``query.resolver`` 등 모든
ownership consumer가 이 모듈의 predicate를 공유한다 — endpoint마다
비교 로직을 중복 구현하지 않는다(#504 review).

정책:
    - ``ENFORCE_OWNERSHIP`` off → 항상 허용(하위 호환).
    - dev/service principal → 관리자 권한으로 모든 run 허용.
    - 그 외(oidc 등) → ``created_by``가 principal label과 일치해야 한다.
      ``created_by``가 없으면 거부한다(fail-closed — "owner 정보 없음 =
      누구나 접근 가능"으로 취급하지 않는다).
"""

from __future__ import annotations

import os

from .auth import Principal

_OWNERSHIP_ENV = "ENFORCE_OWNERSHIP"


def enforce_ownership() -> bool:
    """소유권 강제가 활성화되어 있는지 (#389). 기본 off — 하위 호환."""
    return os.environ.get(_OWNERSHIP_ENV, "").lower() in ("true", "1")


def ownership_allows(
    *, created_by: str | None, principal: Principal, enforce: bool | None = None
) -> bool:
    """레코드(created_by)를 principal이 접근할 수 있는지 판정한다.

    ``enforce``를 생략하면 환경변수(``ENFORCE_OWNERSHIP``)를 읽는다.
    """
    if enforce is None:
        enforce = enforce_ownership()
    if not enforce:
        return True
    if principal.kind in ("dev", "service"):
        return True
    return created_by is not None and created_by == principal.label
