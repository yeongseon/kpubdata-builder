from __future__ import annotations

from dataclasses import dataclass

from ..spec import JsonPrimitive


@dataclass(frozen=True)
class QualityCheckResult:
    name: str
    status: str
    observed: JsonPrimitive
    threshold: JsonPrimitive
    message: str
    column: str | None = None


@dataclass(frozen=True)
class SourceQualityResult:
    source_key: str
    status: str
    checks: tuple[QualityCheckResult, ...] = ()


@dataclass(frozen=True)
class SchemaDriftFinding:
    kind: str
    column: str | None
    detail: str


@dataclass(frozen=True)
class SourceSchemaDrift:
    source_key: str
    findings: tuple[SchemaDriftFinding, ...] = ()


__all__ = [
    "QualityCheckResult",
    "SchemaDriftFinding",
    "SourceQualityResult",
    "SourceSchemaDrift",
]
