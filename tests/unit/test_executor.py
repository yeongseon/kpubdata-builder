from __future__ import annotations

import pytest

from kpubdata_builder import ExecutionError, ValidationError
from kpubdata_builder.executor import ExecutionResult, source_executor
from kpubdata_builder.spec import BuildSpec, ExportTarget, SourceRef


def test_source_executor_returns_execution_result() -> None:
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=(SourceRef(provider="datago", dataset="air_quality"),),
        exports=(ExportTarget(kind="jsonl", output_path="out/data.jsonl"),),
        metadata={"owner": "team-data"},
    )

    result = source_executor(spec)

    assert isinstance(result, ExecutionResult)
    assert result.artifact.metadata == {"owner": "team-data"}
    assert result.artifact.provenance == ("datago.air_quality",)
    assert result.warnings == ()
    assert result.errors == ()


def test_source_executor_wraps_validation_failures() -> None:
    spec = BuildSpec(
        dataset_id=" ",
        title="Sample Dataset",
        description="Sample description",
        sources=(),
        exports=(),
    )

    with pytest.raises(ValidationError):
        source_executor(spec)


def test_source_executor_wraps_source_processing_failures() -> None:
    class BrokenString(str):
        def __str__(self) -> str:
            raise RuntimeError("boom")

    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=(SourceRef(provider=BrokenString("datago"), dataset="air_quality"),),
        exports=(ExportTarget(kind="jsonl", output_path="out/data.jsonl"),),
    )

    with pytest.raises(ExecutionError):
        source_executor(spec)
