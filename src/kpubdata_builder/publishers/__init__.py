"""Publisher registry for remote artifact publication integrations."""

from __future__ import annotations

from .base import BasePublisher
from .huggingface import HuggingFacePublisher
from .local import LocalPublisher

PUBLISHER_REGISTRY: dict[str, type[BasePublisher]] = {
    "local": LocalPublisher,
    "huggingface": HuggingFacePublisher,
}

__all__ = [
    "BasePublisher",
    "HuggingFacePublisher",
    "LocalPublisher",
    "PUBLISHER_REGISTRY",
]
