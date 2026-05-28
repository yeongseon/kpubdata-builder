"""Public package surface for kpubdata-builder."""

from __future__ import annotations

from .artifact import ArtifactDataset
from .errors import (
    AssemblyError,
    BuildError,
    ExecutionError,
    ExportError,
    ManifestError,
    SpecLoadError,
    ValidationError,
)
from .manifest import BuildManifest, SourceProvenance, manifest_writer
from .spec import BuildSpec, ExportTarget, SourceRef
from .validator import validate_spec

__version__ = "0.1.0a0"

__all__ = [
    "ArtifactDataset",
    "BuildError",
    "BuildManifest",
    "BuildSpec",
    "ExecutionError",
    "ExportError",
    "ManifestError",
    "ExportTarget",
    "SourceProvenance",
    "SourceRef",
    "SpecLoadError",
    "ValidationError",
    "AssemblyError",
    "__version__",
    "manifest_writer",
    "validate_spec",
]
