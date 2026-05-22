"""원격 산출물 게시 연동을 위한 게시 도구 레지스트리."""

from __future__ import annotations

from .base import BasePublisher

PUBLISHER_REGISTRY: dict[str, BasePublisher] = {}

__all__ = ["BasePublisher", "PUBLISHER_REGISTRY"]
