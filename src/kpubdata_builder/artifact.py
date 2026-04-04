"""Canonical assembled artifact model used by exporters."""

from __future__ import annotations

from dataclasses import dataclass, field

from .spec import JsonValue


@dataclass(frozen=True)
class ArtifactDataset:
    """Assembled dataset representation before concrete export."""

    records: tuple[dict[str, JsonValue], ...] = ()
    schema: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, str] = field(default_factory=dict)
    provenance: tuple[str, ...] = ()
    statistics: dict[str, int] = field(default_factory=dict)
