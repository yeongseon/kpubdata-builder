"""Error hierarchy for builder pipeline failures."""

from __future__ import annotations


class BuildError(Exception):
    """Base exception for all builder errors."""


class SpecLoadError(BuildError):
    """Failed to load or parse a build spec."""


class ValidationError(BuildError):
    """Build spec validation failed."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(f"Validation failed: {'; '.join(problems)}")


class ExecutionError(BuildError):
    """Source execution failed."""


class AssemblyError(BuildError):
    """Artifact assembly failed."""


class ExportError(BuildError):
    """Export operation failed."""


class ManifestError(BuildError):
    """Manifest write failed."""


__all__ = [
    "AssemblyError",
    "BuildError",
    "ExecutionError",
    "ExportError",
    "ManifestError",
    "SpecLoadError",
    "ValidationError",
]
