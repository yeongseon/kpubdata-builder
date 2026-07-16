"""예외 계층과 ValidationError의 보조 데이터 보존을 검증한다."""

from __future__ import annotations

from kpubdata_builder import (
    BuildError,
    ExportError,
    ManifestError,
    SpecLoadError,
    ValidationError,
)


def test_all_builder_errors_inherit_from_build_error() -> None:
    # 모든 공개 예외가 BuildError 기반 계층에 속하는지 확인한다.
    assert issubclass(SpecLoadError, BuildError)
    assert issubclass(ValidationError, BuildError)
    assert issubclass(ExportError, BuildError)
    assert issubclass(ManifestError, BuildError)


def test_validation_error_keeps_problem_list() -> None:
    # ValidationError가 문제 목록과 문자열 표현을 함께 보존하는지 검증한다.
    error = ValidationError(["problem one", "problem two"])

    assert error.problems == ["problem one", "problem two"]
    assert str(error) == "Validation failed: problem one; problem two"
