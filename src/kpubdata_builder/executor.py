"""Source execution layer for producing artifact-ready records.

.. deprecated::
    This module is part of the legacy package pipeline. The canonical pipeline
    is the Medallion pipeline in ``stages/`` and ``pipeline/``. This module
    will be removed in a future version.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

from .artifact import ArtifactDataset
from .errors import ExecutionError, ValidationError
from .spec import BuildSpec
from .validator import validate_spec

_DEPRECATION_MSG = (
    "source_executor() is part of the legacy pipeline. "
    "Use the Medallion pipeline (stages/bronze → silver → gold) instead."
)


@dataclass(frozen=True)
class ExecutionResult:
    artifact: ArtifactDataset
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


def source_executor(spec: BuildSpec) -> ExecutionResult:
    """Execute declared sources and return a minimal assembled artifact stub.

    .. deprecated::
        Use the Medallion pipeline instead.
    """
    warnings.warn(_DEPRECATION_MSG, DeprecationWarning, stacklevel=2)

    try:
        validate_spec(spec)
    except ValidationError:
        raise
    except ValueError as exc:
        raise ValidationError([str(exc)]) from exc

    try:
        provenance = tuple(f"{source.provider}.{source.dataset}" for source in spec.sources)
        artifact = ArtifactDataset(metadata=dict(spec.metadata), provenance=provenance)
    except Exception as exc:
        raise ExecutionError(
            f"Failed to execute sources for dataset {spec.dataset_id}: {exc}"
        ) from exc

    return ExecutionResult(artifact=artifact)


__all__ = ["ExecutionResult", "source_executor"]
