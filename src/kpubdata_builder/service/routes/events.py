"""Run event timeline route adapter (#496)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from ...spec import JsonValue
from ...stages._path_safety import validate_path_segment
from .. import events as events_service
from ..auth import Principal
from ..responses import ServiceResponse
from ._guards import check_active_run_access
from ._types import RouteResponse

if TYPE_CHECKING:
    from ..app import BuilderService

_PREFIX = "/builds/"
_SUFFIX = "/events"


def route(
    service: BuilderService,
    method: str,
    path: str,
    body: Mapping[str, JsonValue] | None,
    query: str,
    principal: Principal,
) -> RouteResponse | None:
    del body
    # "/stages" route(service/routes/stages.py)와 동일한 순서를 따른다: run_id는
    # segments[0]에서 바로 뽑아 path의 나머지 모양과 무관하게 먼저 검증한다 —
    # "../escape/events"처럼 안전하지 않은 run_id는 이 adapter가 실제로 매칭되는
    # 경로 모양인지와 상관없이 400으로 거부되어야 한다(#317 conformance와
    # 동일한 path-traversal 방어 관례).
    if method != "GET" or not path.startswith(_PREFIX) or _SUFFIX not in path:
        return None
    segments = path[len(_PREFIX) :].split("/")
    run_id = segments[0]
    try:
        validate_path_segment(run_id, field_name="run_id")
    except ValueError as exc:
        return ServiceResponse(400, {"error": str(exc)})
    if len(segments) != 2 or segments[1] != "events":
        return None

    limit_or_error = _parse_limit(query)
    if isinstance(limit_or_error, ServiceResponse):
        return limit_or_error
    tail_or_error = _parse_tail(query)
    if isinstance(tail_or_error, ServiceResponse):
        return tail_or_error

    # 순서는 다른 /builds/{run_id}/* route와 동일하다(#488 관례, #496도 따른다):
    # run_id 검증 -> 존재/ownership 확인 -> 실제 조회. cross-owner 접근이
    # events 조회 로직에 도달하기 전에 403으로 막혀야 run 존재 여부가 이
    # endpoint로 새어나가지 않는다.
    #
    # 존재/ownership 판정 자체는 check_active_run_access(#496 follow-up)가
    # 맡는다 — persisted run(manifest 기반)뿐 아니라 아직 run
    # directory/manifest가 없는 active async job(queued/running)도 async job
    # registry로 인지해야 events polling이 그 구간에서 404/403으로 막히지
    # 않는다. 다른 /builds/{run_id}/* route(manifest, stages 등)는 여전히
    # persisted run만 다루므로 이 helper를 쓰지 않는다.
    error = check_active_run_access(service, run_id, principal)
    return error or service.get_build_events(run_id, limit=limit_or_error, tail=tail_or_error)


def _parse_limit(query: str) -> int | ServiceResponse:
    query_params = parse_qs(query)
    if "limit" not in query_params:
        return events_service.DEFAULT_EVENTS_LIMIT
    raw_limit = query_params["limit"][-1]
    try:
        limit = int(raw_limit)
    except ValueError:
        return ServiceResponse(400, {"error": "'limit' must be a positive integer"})
    if limit < 1 or limit > events_service.MAX_EVENTS_LIMIT:
        return ServiceResponse(
            400,
            {
                "error": (
                    f"'limit' must be a positive integer up to {events_service.MAX_EVENTS_LIMIT}"
                )
            },
        )
    return limit


def _parse_tail(query: str) -> bool | ServiceResponse:
    query_params = parse_qs(query)
    if "tail" not in query_params:
        return False
    raw_tail = query_params["tail"][-1]
    if raw_tail == "true":
        return True
    if raw_tail == "false":
        return False
    return ServiceResponse(400, {"error": "'tail' must be 'true' or 'false'"})
