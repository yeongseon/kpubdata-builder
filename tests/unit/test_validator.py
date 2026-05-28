"""BuildSpec 최소 검증 규칙과 오류 수집 동작을 검증한다."""

from __future__ import annotations

import pytest

from kpubdata_builder import ValidationError
from kpubdata_builder.spec import BuildSpec, ExportTarget, SourceRef
from kpubdata_builder.spec.validator import validate_spec


def test_validate_spec_accepts_valid_spec() -> None:
    """유효한 BuildSpec에 대해서는 validate_spec가 예외 없이 통과해야 한다."""
    # sources와 exports가 모두 있는 기본 명세를 허용하는지 확인한다.
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=(SourceRef(provider="datago", dataset="air_quality"),),
        exports=(ExportTarget(kind="markdown", output_path="README.md"),),
    )

    validate_spec(spec)


_SRC = (SourceRef(provider="datago", dataset="air_quality"),)
_EXP = (ExportTarget(kind="markdown", output_path="README.md"),)


@pytest.mark.parametrize(
    ("dataset_id", "title", "description", "sources", "exports", "expected_problems"),
    [
        (
            "   ",
            "Sample Dataset",
            "Sample description",
            _SRC,
            _EXP,
            ["dataset_id must be a non-empty string"],
        ),
        (
            "dataset.sample",
            "  ",
            "Sample description",
            _SRC,
            _EXP,
            ["title must be a non-empty string"],
        ),
        (
            "dataset.sample",
            "Sample Dataset",
            "  ",
            _SRC,
            _EXP,
            ["description must be a non-empty string"],
        ),
        (
            "dataset.sample",
            "Sample Dataset",
            "Sample description",
            (),
            _EXP,
            ["at least one source is required"],
        ),
        (
            "dataset.sample",
            "Sample Dataset",
            "Sample description",
            _SRC,
            (),
            ["at least one export target is required"],
        ),
    ],
)
def test_validate_spec_rejects_invalid_spec(
    dataset_id: str,
    title: str,
    description: str,
    sources: tuple[SourceRef, ...],
    exports: tuple[ExportTarget, ...],
    expected_problems: list[str],
) -> None:
    # 잘못된 입력 조합마다 기대한 problems 목록이 수집되는지 검증한다.
    spec = BuildSpec(
        dataset_id=dataset_id,
        title=title,
        description=description,
        sources=sources,
        exports=exports,
    )

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)

    assert exc_info.value.problems == expected_problems


def test_validate_spec_rejects_unsupported_export_kind() -> None:
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=_SRC,
        exports=(ExportTarget(kind="xml", output_path="out/data.xml"),),
    )

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)

    assert any("xml" in p and "not supported" in p for p in exc_info.value.problems)


def test_validate_spec_rejects_empty_output_path() -> None:
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=_SRC,
        exports=(ExportTarget(kind="jsonl", output_path="   "),),
    )

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)

    assert any("output_path" in p for p in exc_info.value.problems)


def test_validate_spec_rejects_empty_metadata_key() -> None:
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=_SRC,
        exports=_EXP,
        metadata={"": "value"},
    )

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)

    assert any("metadata keys" in p for p in exc_info.value.problems)
