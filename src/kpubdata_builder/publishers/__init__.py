"""원격 산출물 게시 연동을 위한 게시 도구 레지스트리.

현재는 구체 구현이 없지만, 향후 HuggingFace나 GitHub 같은 원격 대상에
게시할 구현체를 이 레지스트리로 연결할 예정이다.
"""

from __future__ import annotations

from .base import BasePublisher

PUBLISHER_REGISTRY: dict[str, BasePublisher] = {}

__all__ = ["BasePublisher", "PUBLISHER_REGISTRY"]
