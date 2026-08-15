"""run/dataset 소유권 판정의 단일 canonical 게이트 (#389/#504/#505).

``service.app`` / ``service.datasets`` / ``query.resolver`` 등 모든
ownership consumer가 이 모듈을 공유한다 — endpoint마다 비교 로직을 중복
구현하지 않는다(#504 review).

게이팅 정책:
    - ``ENFORCE_OWNERSHIP`` off → 항상 허용(하위 호환).
    - dev/service principal → 관리자 권한으로 모든 run 허용.

레코드 비교 자체는 ``service.auth.principal_owns``(#505 canonical:
stable ``owner_id`` 우선, legacy ``created_by``/label 폴백, fail-closed)를
그대로 사용한다 — 두 구현이 드리프트하지 않게 비교 로직은 auth에 단일로
둔다.
"""

from __future__ import annotations

import os

from .auth import Principal, principal_owns

_OWNERSHIP_ENV = "ENFORCE_OWNERSHIP"


def enforce_ownership() -> bool:
    """소유권 강제가 활성화되어 있는지 (#389). 기본 off — 하위 호환."""
    return os.environ.get(_OWNERSHIP_ENV, "").lower() in ("true", "1")


def ownership_allows(
    *,
    created_by: str | None,
    owner_id: str | None,
    principal: Principal,
    enforce: bool | None = None,
) -> bool:
    """레코드(created_by/owner_id)를 principal이 접근할 수 있는지 판정한다.

    ``enforce``를 생략하면 환경변수(``ENFORCE_OWNERSHIP``)를 읽는다.
    비교는 ``principal_owns``(#505)에 위임한다.
    """
    if enforce is None:
        enforce = enforce_ownership()
    if not enforce:
        return True
    if principal.kind in ("dev", "service"):
        return True
    return principal_owns(created_by=created_by, owner_id=owner_id, principal=principal)
