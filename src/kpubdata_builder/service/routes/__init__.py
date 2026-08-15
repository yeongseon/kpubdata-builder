"""명시적 순서를 갖는 service route adapters."""

from __future__ import annotations

from . import artifacts, builds, core, datasets, providers, quality, query, stages
from ._types import RouteAdapter

# 순서는 기존 app.dispatch 조건문의 우선순위를 고정한다.
ROUTE_ADAPTERS: tuple[RouteAdapter, ...] = (
    core.route,
    providers.route,
    query.route,
    datasets.route,
    builds.route,
    quality.route,
    stages.route,
    artifacts.route,
)

__all__ = ["ROUTE_ADAPTERS", "RouteAdapter"]
