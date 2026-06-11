"""조립 단계의 경고 처리와 실패 조건을 검증한다."""

from __future__ import annotations

import pytest

from kpubdata_builder import AssemblyError
from kpubdata_builder.assembler import AssemblyResult, assemble_artifact
from kpubdata_builder.spec import BuildSpec, ExportTarget, SourceRef


def test_assemble_artifact_reports_missing_source_warning() -> None:
    # 일부 소스만 존재할 때 경고를 남기고 조립은 계속하는지 검증한다.
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=(
            SourceRef(provider="datago", dataset="air_quality", alias="present"),
            SourceRef(provider="datago", dataset="village_fcst", alias="missing"),
        ),
        exports=(ExportTarget(kind="jsonl", output_path="out/data.jsonl"),),
    )

    result = assemble_artifact(spec, {"present": ({"id": "1"},)})

    assert isinstance(result, AssemblyResult)
    assert result.artifact.records == ({"id": "1"},)
    assert result.artifact.provenance == ("present",)
    assert result.warnings == ("Missing records for source: missing",)


def test_assemble_artifact_injects_spec_core_metadata() -> None:
    # spec의 title/dataset_id/description이 artifact metadata에 채워져야 한다 (#179).
    spec = BuildSpec(
        dataset_id="kpub/air",
        title="Air Quality",
        description="Hourly air quality",
        sources=(SourceRef(provider="datago", dataset="air_quality", alias="present"),),
        exports=(ExportTarget(kind="kaggle", output_path="out/data.csv"),),
        metadata={"license": "CC0-1.0"},
    )

    result = assemble_artifact(spec, {"present": ({"id": "1"},)})

    meta = result.artifact.metadata
    assert meta["title"] == "Air Quality"
    assert meta["dataset_id"] == "kpub/air"
    assert meta["description"] == "Hourly air quality"
    # 명시적 metadata 값은 보존된다.
    assert meta["license"] == "CC0-1.0"


def test_assemble_artifact_explicit_metadata_overrides_spec_fields() -> None:
    # metadata에 동일 키가 있으면 spec 핵심 필드보다 우선한다 (#179).
    spec = BuildSpec(
        dataset_id="kpub/air",
        title="Air Quality",
        description="desc",
        sources=(SourceRef(provider="datago", dataset="air_quality", alias="present"),),
        exports=(ExportTarget(kind="kaggle", output_path="out/data.csv"),),
        metadata={"title": "Override Title", "dataset_id": "kpub/override"},
    )

    result = assemble_artifact(spec, {"present": ({"id": "1"},)})

    assert result.artifact.metadata["title"] == "Override Title"
    assert result.artifact.metadata["dataset_id"] == "kpub/override"


def test_assemble_artifact_raises_when_all_sources_missing() -> None:
    # 조립 가능한 레코드가 하나도 없으면 AssemblyError가 발생해야 한다.
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=(SourceRef(provider="datago", dataset="air_quality", alias="missing"),),
        exports=(ExportTarget(kind="jsonl", output_path="out/data.jsonl"),),
    )

    with pytest.raises(AssemblyError):
        assemble_artifact(spec, {})
