"""명시적 순서를 갖는 service route adapters."""

from __future__ import annotations

from . import (
    artifacts,
    builds,
    core,
    datasets,
    events,
    monitoring,
    providers,
    quality,
    query,
    stages,
)
from ._types import RouteAdapter

# 순서는 기존 app.dispatch 조건문의 우선순위를 고정한다. events(#496)는
# builds 바로 다음에 둔다 — 둘 다 "/builds/{run_id}/..." 경로를 다루므로 논리적
# 이웃이다(실제 매칭은 각 adapter의 path suffix 검사로 서로 겹치지 않는다).
ROUTE_ADAPTERS: tuple[RouteAdapter, ...] = (
    core.route,
    providers.route,
    query.route,
    datasets.route,
    builds.route,
    events.route,
    quality.route,
    stages.route,
    artifacts.route,
    monitoring.route,
)

__all__ = ["ROUTE_ADAPTERS", "RouteAdapter"]
