"""소스 레코드를 표준 산출물로 결합하는 조립 계층.

이 모듈은 여러 소스에서 가져온 레코드를 BuildSpec 순서에 맞춰 병합하고,
exporter가 바로 소비할 수 있는 ArtifactDataset으로 감싼다.

주요 구성:
    - AssemblyResult: 조립 결과와 경고를 담는 값 객체
    - assemble_artifact: 소스별 레코드 묶음을 하나의 산출물로 결합하는 함수
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .artifact import ArtifactDataset
from .errors import AssemblyError
from .spec import BuildSpec, JsonValue


@dataclass(frozen=True)
class AssemblyResult:
    """조립 결과와 경고 메시지를 함께 보관한다.

    속성:
        artifact: 최종 조립된 데이터셋 산출물.
        warnings: 누락된 소스처럼 치명적이지 않은 문제 목록.
    """

    artifact: ArtifactDataset
    warnings: tuple[str, ...] = ()


def assemble_artifact(
    spec: BuildSpec,
    records_by_source: dict[str, Sequence[dict[str, JsonValue]]],
) -> AssemblyResult:
    """최소한의 결정적 병합 동작으로 소스 레코드를 조립한다.

    BuildSpec에 선언된 소스 순서를 유지하면서 각 레코드 목록을 병합한다.
    지정된 소스의 레코드가 없으면 경고를 남기고 계속 진행하지만,
    모든 소스가 비어 있으면 AssemblyError를 발생시킨다.

    매개변수:
        spec: 어떤 소스를 어떤 순서로 조립할지 정의한 빌드 명세.
        records_by_source: 소스 키별 레코드 시퀀스 매핑.

    반환값:
        AssemblyResult: 병합된 산출물과 경고 목록.

    예외:
        AssemblyError: 조립 가능한 레코드가 하나도 없을 때.

    예시:
        >>> result = assemble_artifact(spec, {"datago.air": ({"id": "1"},)})
        >>> result.artifact.statistics["record_count"]
        1
    """
    merged_records: list[dict[str, JsonValue]] = []
    warnings: list[str] = []
    present_sources: list[str] = []

    for source in spec.sources:
        source_key = source.alias if source.alias else f"{source.provider}.{source.dataset}"
        source_records = records_by_source.get(source_key)
        if source_records is None:
            warnings.append(f"Missing records for source: {source_key}")
            continue
        present_sources.append(source_key)
        merged_records.extend(source_records)

    if not present_sources:
        raise AssemblyError(f"No source records available for dataset {spec.dataset_id}")

    statistics = {"record_count": len(merged_records)}
    artifact = ArtifactDataset(
        records=tuple(merged_records),
        metadata=dict(spec.metadata),
        provenance=tuple(present_sources),
        statistics=statistics,
    )
    return AssemblyResult(artifact=artifact, warnings=tuple(warnings))


__all__ = ["AssemblyResult", "assemble_artifact"]
