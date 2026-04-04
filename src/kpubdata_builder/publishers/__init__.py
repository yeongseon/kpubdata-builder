"""Publisher registry for remote artifact publication integrations."""

from __future__ import annotations

from .base import BasePublisher

PUBLISHER_REGISTRY: dict[str, BasePublisher] = {}

__all__ = ["BasePublisher", "PUBLISHER_REGISTRY"]
