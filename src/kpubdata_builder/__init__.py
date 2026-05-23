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
from .manifest import BuildManifest, manifest_writer
from .preview import PreviewResult, preview_build
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
    "PreviewResult",
    "SourceRef",
    "SpecLoadError",
    "ValidationError",
    "AssemblyError",
    "__version__",
    "manifest_writer",
    "preview_build",
    "validate_spec",
]
