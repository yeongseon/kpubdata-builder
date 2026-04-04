"""Public package surface for kpubdata-builder."""

from __future__ import annotations

from .artifact import ArtifactDataset
from .manifest import BuildManifest, manifest_writer
from .spec import BuildSpec, ExportTarget, SourceRef
from .validator import validate_spec

__version__ = "0.1.0a0"

__all__ = [
    "ArtifactDataset",
    "BuildManifest",
    "BuildSpec",
    "ExportTarget",
    "SourceRef",
    "__version__",
    "manifest_writer",
    "validate_spec",
]
