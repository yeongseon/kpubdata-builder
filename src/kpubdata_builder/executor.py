"""Source execution layer for producing artifact-ready records."""

from __future__ import annotations

from .artifact import ArtifactDataset
from .spec import BuildSpec
from .validator import validate_spec


def source_executor(spec: BuildSpec) -> ArtifactDataset:
    """Execute declared sources and return a minimal assembled artifact stub."""
    validate_spec(spec)
    provenance = tuple(f"{source.provider}.{source.dataset}" for source in spec.sources)
    return ArtifactDataset(metadata=dict(spec.metadata), provenance=provenance)
