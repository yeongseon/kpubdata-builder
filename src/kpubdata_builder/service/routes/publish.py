"""Build publish readiness/실행 route adapter (#491)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from ...spec import JsonValue
from ...stages._path_safety import validate_path_segment
from ..auth import Principal
from ..responses import ServiceResponse
from ._guards import check_active_run_access
from ._types import RouteResponse

if TYPE_CHECKING:
    from ..app import BuilderService

_READINESS_SUFFIX = "/publish/readiness"
_PUBLISH_SUFFIX = "/publish"


def route(
    service: BuilderService,
    method: str,
    path: str,
    body: Mapping[str, JsonValue] | None,
    query: str,
    principal: Principal,
) -> RouteResponse | None:
    if not path.startswith("/builds/"):
        return None
    rest = path[len("/builds/") :]

    if method == "GET" and rest.endswith(_READINESS_SUFFIX):
        run_id = rest[: -len(_READINESS_SUFFIX)]
        error = _validate_run_id(run_id)
        if error is not None:
            return error
        query_params = parse_qs(query)
        target_values = query_params.get("target")
        if not target_values or not target_values[-1]:
            return ServiceResponse(400, {"error": "'target' query parameter is required"})
        target = target_values[-1]
        # #496 follow-up과 동일하게 manifest 유무와 무관하게(queued/running도)
        # 존재/소유권을 판정한다 — publish readiness는 running/queued run도
        # 404가 아니라 "아직 안 끝남" blocker로 보고해야 한다(#491).
        access_error = check_active_run_access(service, run_id, principal)
        if access_error is not None:
            return access_error
        return service.publish_readiness(run_id, target)

    if method == "POST" and rest.endswith(_PUBLISH_SUFFIX):
        run_id = rest[: -len(_PUBLISH_SUFFIX)]
        error = _validate_run_id(run_id)
        if error is not None:
            return error
        access_error = check_active_run_access(service, run_id, principal)
        if access_error is not None:
            return access_error
        return service.publish(run_id, body, principal=principal)

    return None


def _validate_run_id(run_id: str) -> ServiceResponse | None:
    try:
        validate_path_segment(run_id, field_name="run_id")
    except ValueError as exc:
        return ServiceResponse(400, {"error": str(exc)})
    return None
