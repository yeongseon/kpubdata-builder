"""Builder HTTP 서비스 façade 패키지 (#36).

Studio 등 외부 UI가 Builder를 호출할 수 있도록 validate/preview/build/artifacts
엔드포인트를 제공한다. 로직(app)과 stdlib HTTP 전송(http)을 분리한다.

주요 구성:
    - BuilderService / ServiceResponse / dispatch: 전송 무관 서비스 로직
    - serve / make_handler: stdlib http.server 어댑터
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .app import API_CONTRACT_VERSION, BuilderService, FileResponse, ServiceResponse, dispatch
    from .http import make_handler, serve
    from .jobs import AsyncBuildExecutor, BuildJobSnapshot, BuildJobStatus

__all__ = [
    "API_CONTRACT_VERSION",
    "BuilderService",
    "FileResponse",
    "ServiceResponse",
    "AsyncBuildExecutor",
    "BuildJobSnapshot",
    "BuildJobStatus",
    "dispatch",
    "make_handler",
    "serve",
]


def __getattr__(name: str) -> Any:
    """facade export를 lazy하게 로드해 query/jobs 의존성이 순환하지 않게 한다."""
    if name in {
        "API_CONTRACT_VERSION",
        "BuilderService",
        "FileResponse",
        "ServiceResponse",
        "dispatch",
    }:
        from . import app

        return getattr(app, name)
    if name in {"make_handler", "serve"}:
        from . import http

        return getattr(http, name)
    if name in {"AsyncBuildExecutor", "BuildJobSnapshot", "BuildJobStatus"}:
        from . import jobs

        return getattr(jobs, name)
    raise AttributeError(name)
