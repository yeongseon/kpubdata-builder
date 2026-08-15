"""Dataset catalog/detail route adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING
from urllib.parse import unquote

from ...spec import JsonValue
from ..auth import Principal
from ..responses import ServiceResponse
from ._parsing import positive_limit_query
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
    del body
    if method == "GET" and path == "/datasets":
        limit = positive_limit_query(query)
        if isinstance(limit, ServiceResponse):
            return limit
        return service.list_datasets(limit=limit, principal=principal)
    if method != "GET" or not path.startswith("/datasets/"):
        return None

    raw_rest = path[len("/datasets/") :]
    is_runs_route = raw_rest.endswith("/runs")
    is_quality_history_route = raw_rest.endswith("/quality/history")
    if is_runs_route:
        raw_dataset_id = raw_rest[: -len("/runs")]
    elif is_quality_history_route:
        raw_dataset_id = raw_rest[: -len("/quality/history")]
    else:
        raw_dataset_id = raw_rest
    if not raw_dataset_id:
        return ServiceResponse(400, {"error": "dataset_id must not be empty"})
    dataset_id = unquote(raw_dataset_id)
    if not dataset_id:
        return ServiceResponse(400, {"error": "dataset_id must not be empty"})
    if is_runs_route:
        limit = positive_limit_query(query)
        if isinstance(limit, ServiceResponse):
            return limit
        return service.list_dataset_runs(dataset_id, limit=limit, principal=principal)
    if is_quality_history_route:
        limit = positive_limit_query(query, default=30)
        if isinstance(limit, ServiceResponse):
            return limit
        return service.get_dataset_quality_history(dataset_id, limit=limit, principal=principal)
    return service.get_dataset(dataset_id, principal=principal)
