"""Build specification models for orchestrating dataset builds."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TypeAlias

JsonPrimitive: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonPrimitive | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True)
class SourceRef:
    """Reference to a normalized source query from kpubdata."""

    provider: str
    dataset: str
    params: dict[str, JsonValue] = field(default_factory=dict)
    normalization_mode: str = "canonical"
    alias: str = ""


@dataclass(frozen=True)
class ExportTarget:
    """Concrete exporter target definition for a build."""

    kind: str
    output_path: str
    options: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True)
class BuildSpec:
    """Declarative build specification for a dataset artifact."""

    dataset_id: str
    title: str
    description: str
    sources: tuple[SourceRef, ...]
    exports: tuple[ExportTarget, ...]
    transforms: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)
    publish: bool = False
