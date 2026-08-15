"""Dataset query route adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from ...spec import JsonValue
from ..auth import Principal
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
    del query
    if method == "POST" and path == "/query":
        return service.query(body, principal=principal)
    return None
