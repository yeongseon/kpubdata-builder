"""산출물 준비가 된 레코드를 만드는 소스 실행 계층.

이 모듈은 아직 완전한 원격 수집 파이프라인이 도입되기 전 단계에서,
BuildSpec 검증과 최소 provenance 구성을 수행하는 실행 스텁을 제공한다.

주요 구성:
    - ExecutionResult: 실행 결과 산출물과 경고/오류를 담는 값 객체
    - source_executor: 선언된 소스를 실행 가능한 형태로 정리하는 함수
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from .artifact import ArtifactDataset
from .errors import ExecutionError, ValidationError
from .spec import BuildSpec, JsonValue, SourceRef
from .spec.validator import validate_spec


@dataclass(frozen=True)
class ExecutionResult:
    """실행 단계 결과를 표현한다.

    속성:
        artifact: 후속 조립/내보내기에 사용할 산출물.
        warnings: 실행은 계속 가능하지만 사용자에게 알려야 하는 경고.
        errors: 부분 실패 상황을 담기 위한 예약 필드.
    """

    artifact: ArtifactDataset
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


def source_executor(spec: BuildSpec) -> ExecutionResult:
    """선언된 소스를 실행하고 최소한의 조립 산출물 스텁을 반환한다.

    현재 구현은 실제 네트워크 호출 대신 BuildSpec 검증과 provenance 구성에
    집중한다. 따라서 후속 단계가 기대하는 최소 ArtifactDataset을 안정적으로
    만들어 주는 것이 목적이다.

    매개변수:
        spec: 실행할 빌드 명세.

    반환값:
        ExecutionResult: provenance가 채워진 최소 산출물.

    예외:
        ValidationError: 명세 검증 실패 시.
        ExecutionError: provenance 구성 중 예기치 않은 오류가 발생한 경우.
    """
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


class DatasetResult(Protocol):
    """Minimal result shape returned by a kpubdata dataset query."""

    @property
    def items(self) -> Iterable[dict[str, JsonValue]]:
        """Return fetched records."""


class SourceDataset(Protocol):
    """Minimal dataset shape used to fetch source records."""

    def list(self, **params: JsonValue) -> DatasetResult:
        """Fetch records for one parameter set."""


class SourceClient(Protocol):
    """Minimal client shape used to resolve source datasets."""

    def dataset(self, dataset_id: str) -> SourceDataset:
        """Return a dataset object for a dataset_id."""


def _source_key(source: SourceRef) -> str:
    """Key a source by its alias when present, else provider.dataset."""
    return source.alias if source.alias else f"{source.provider}.{source.dataset}"


def execute_sources(
    spec: BuildSpec,
    client: SourceClient,
) -> dict[str, Sequence[dict[str, JsonValue]]]:
    """Fetch records for every declared source via a kpubdata-compatible client.

    Returns a mapping of source key (alias or provider.dataset) to its records,
    suitable for ``assemble_artifact``. The client is injected so tests can
    supply a fake without hitting the network.
    """
    records_by_source: dict[str, Sequence[dict[str, JsonValue]]] = {}
    for source in spec.sources:
        key = _source_key(source)
        try:
            dataset = client.dataset(f"{source.provider}.{source.dataset}")
            result = dataset.list(**source.params)
            records_by_source[key] = list(result.items)
        except Exception as exc:
            raise ExecutionError(
                f"Failed to fetch source {key} for dataset {spec.dataset_id}: {exc}"
            ) from exc
    return records_by_source


__all__ = ["ExecutionResult", "execute_sources", "source_executor"]
