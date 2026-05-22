"""산출물 준비가 된 레코드를 만드는 소스 실행 계층."""

from __future__ import annotations

from dataclasses import dataclass

from .artifact import ArtifactDataset
from .errors import ExecutionError, ValidationError
from .spec import BuildSpec
from .validator import validate_spec


@dataclass(frozen=True)
class ExecutionResult:
    artifact: ArtifactDataset
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


def source_executor(spec: BuildSpec) -> ExecutionResult:
    """선언된 소스를 실행하고 최소한의 조립 산출물 스텁을 반환한다."""
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
