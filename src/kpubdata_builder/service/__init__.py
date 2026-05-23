"""Builder HTTP 서비스 façade 패키지 (#36).

Studio 등 외부 UI가 Builder를 호출할 수 있도록 validate/preview/build/artifacts
엔드포인트를 제공한다. 로직(app)과 stdlib HTTP 전송(http)을 분리한다.

주요 구성:
    - BuilderService / ServiceResponse / dispatch: 전송 무관 서비스 로직
    - serve / make_handler: stdlib http.server 어댑터
"""

from __future__ import annotations

from .app import BuilderService, ServiceResponse, dispatch
from .http import make_handler, serve

__all__ = [
    "BuilderService",
    "ServiceResponse",
    "dispatch",
    "make_handler",
    "serve",
]
