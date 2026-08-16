"""비동기 build job과 run metadata route adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from ...spec import JsonValue
from ...stages._path_safety import validate_path_segment
from ..auth import Principal
from ..responses import ServiceResponse
from ._guards import check_ownership, check_run_exists
from ._parsing import optional_run_id, spec_from_body
from ._types import RouteResponse

if TYPE_CHECKING:
    from ..app import BuilderService


def route(
    service: BuilderService,
    method: str,
    path: str,
    body: Mapping[str, JsonValue] | None,
    query: str,
    principal: Principal,
) -> RouteResponse | None:
    if method == "POST" and path == "/builds":
        spec = spec_from_body(body)
        if isinstance(spec, ServiceResponse):
            return spec
        run_id = optional_run_id(body)
        if isinstance(run_id, ServiceResponse):
            return run_id
        return service.submit_build(
            spec, run_id=run_id, created_by=principal.label, owner_id=principal.owner_id
        )

    if method == "GET" and path.startswith("/builds/") and "/" not in path[len("/builds/") :]:
        return service.build_status(path[len("/builds/") :])

    if method == "GET" and path.startswith("/builds/") and path.endswith("/manifest"):
        rest = path[len("/builds/") :]
        parts = rest.split("/", 1)
        run_id = parts[0]
        if len(parts) == 2 and parts[1] == "manifest":
            error = _validate_run_id(run_id)
            if error is not None:
                return error
            ownership_error = check_ownership(service, run_id, principal)
            return ownership_error or service.manifest(run_id)

    if method == "GET" and path.startswith("/builds/") and path.endswith("/spec"):
        run_id = path[len("/builds/") : -len("/spec")]
        error = _validate_run_id(run_id)
        if error is not None:
            return error
        existence_error = check_run_exists(service, run_id)
        if existence_error is not None:
            return existence_error
        ownership_error = check_ownership(service, run_id, principal)
        return ownership_error or service.spec(run_id)

    if method == "GET" and path == "/builds":
        limit = 50
        query_params = parse_qs(query)
        if "limit" in query_params:
            raw_limit = query_params["limit"][-1]
            try:
                query_limit = int(raw_limit)
            except ValueError:
                return ServiceResponse(400, {"error": "'limit' must be a positive integer"})
            if query_limit < 1:
                return ServiceResponse(400, {"error": "'limit' must be a positive integer"})
            limit = query_limit
        elif body is not None and "limit" in body:
            limit_value = body["limit"]
            if not isinstance(limit_value, int) or isinstance(limit_value, bool) or limit_value < 1:
                return ServiceResponse(400, {"error": "'limit' must be a positive integer"})
            limit = limit_value
        return service.list_builds(limit=limit, principal=principal)
    return None


def _validate_run_id(run_id: str) -> ServiceResponse | None:
    try:
        validate_path_segment(run_id, field_name="run_id")
    except ValueError as exc:
        return ServiceResponse(400, {"error": str(exc)})
    return None
