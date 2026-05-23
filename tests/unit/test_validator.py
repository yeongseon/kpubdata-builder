"""BuildSpec 최소 검증 규칙과 오류 수집 동작을 검증한다."""

from __future__ import annotations

import pytest

from kpubdata_builder import ValidationError
from kpubdata_builder.spec import BuildSpec, ExportTarget, SourceRef
from kpubdata_builder.validator import validate_spec


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


@pytest.mark.parametrize(
    ("dataset_id", "sources", "exports", "expected_problems"),
    [
        (
            "   ",
            (SourceRef(provider="datago", dataset="air_quality"),),
            (ExportTarget(kind="markdown", output_path="README.md"),),
            ["dataset_id must be a non-empty string"],
        ),
        (
            "dataset.sample",
            (),
            (ExportTarget(kind="markdown", output_path="README.md"),),
            ["at least one source is required"],
        ),
        (
            "dataset.sample",
            (SourceRef(provider="datago", dataset="air_quality"),),
            (),
            ["at least one export target is required"],
        ),
    ],
)
def test_validate_spec_rejects_invalid_spec(
    dataset_id: str,
    sources: tuple[SourceRef, ...],
    exports: tuple[ExportTarget, ...],
    expected_problems: list[str],
) -> None:
    # 잘못된 입력 조합마다 기대한 problems 목록이 수집되는지 검증한다.
    spec = BuildSpec(
        dataset_id=dataset_id,
        title="Sample Dataset",
        description="Sample description",
        sources=sources,
        exports=exports,
    )

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(spec)

    assert exc_info.value.problems == expected_problems
