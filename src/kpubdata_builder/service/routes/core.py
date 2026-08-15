"""Service metadata와 동기 build route adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from ...pipeline import DEFAULT_PREVIEW_SEED
from ...spec import JsonValue
from ...tabular import DEFAULT_PREVIEW_LIMIT
from ..auth import Principal
from ..responses import ServiceResponse
from ._parsing import optional_run_id, spec_from_body
from ._types import RouteResponse

if TYPE_CHECKING:
    from ..app import BuilderService

# dataset을 메모리에 올린 뒤 slice하므로 limit 자체가 fetch량을 줄이지는 않지만,
# 응답에 실리는 sample/diff 크기는 이 값으로 명확히 bound한다. 값은 stage
# preview의 기존 상한(MAX_STAGE_PREVIEW_LIMIT, service/stages.py)과 맞췄다.
MAX_PREVIEW_LIMIT = 1000


def route(
    service: BuilderService,
    method: str,
    path: str,
    body: Mapping[str, JsonValue] | None,
    query: str,
    principal: Principal,
) -> RouteResponse | None:
    del query
    if method == "GET" and path == "/version":
        return service.version()
    if method == "GET" and path == "/catalog":
        return service.catalog()
    if method == "POST" and path == "/validate":
        spec = spec_from_body(body)
        return spec if isinstance(spec, ServiceResponse) else service.validate(spec)
    if method == "POST" and path == "/preview":
        spec = spec_from_body(body)
        if isinstance(spec, ServiceResponse):
            return spec
        # limit이 명시되면 양의 정수(상한 이내)여야 한다 — 잘못된 값을 조용히
        # 기본값으로 떨어뜨리지 않는다.
        if body is not None and "limit" in body:
            limit_value = body["limit"]
            # bool은 int의 하위 타입이지만 limit 의미가 없으므로 거부.
            if (
                not isinstance(limit_value, int)
                or isinstance(limit_value, bool)
                or limit_value < 1
                or limit_value > MAX_PREVIEW_LIMIT
            ):
                return ServiceResponse(
                    400,
                    {"error": f"'limit' must be a positive integer up to {MAX_PREVIEW_LIMIT}"},
                )
            limit = limit_value
        else:
            limit = DEFAULT_PREVIEW_LIMIT
        # sample_mode/seed도 같은 원칙: 잘못된 값을 조용히 기본값으로 떨어뜨리지 않는다 (#497).
        sample_mode = "first"
        if body is not None and "sample_mode" in body:
            sample_mode_value = body["sample_mode"]
            if not isinstance(sample_mode_value, str) or sample_mode_value not in (
                "first",
                "random",
            ):
                return ServiceResponse(400, {"error": "'sample_mode' must be 'first' or 'random'"})
            sample_mode = sample_mode_value
        seed = DEFAULT_PREVIEW_SEED
        if body is not None and "seed" in body:
            seed_value = body["seed"]
            if not isinstance(seed_value, int) or isinstance(seed_value, bool):
                return ServiceResponse(400, {"error": "'seed' must be an integer"})
            seed = seed_value
        return service.preview(
            spec, limit=limit, sample_mode=sample_mode, seed=seed, principal=principal
        )
    if method == "POST" and path == "/build":
        spec = spec_from_body(body)
        if isinstance(spec, ServiceResponse):
            return spec
        run_id = optional_run_id(body)
        if isinstance(run_id, ServiceResponse):
            return run_id
        return service.build(
            spec,
            run_id=run_id,
            created_by=principal.label,
            owner_id=principal.owner_id,
            principal=principal,
        )
    return None
