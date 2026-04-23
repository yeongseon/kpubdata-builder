from __future__ import annotations

from kpubdata_builder import (
    AssemblyError,
    BuildError,
    ExecutionError,
    ExportError,
    ManifestError,
    SpecLoadError,
    ValidationError,
)


def test_all_builder_errors_inherit_from_build_error() -> None:
    assert issubclass(SpecLoadError, BuildError)
    assert issubclass(ValidationError, BuildError)
    assert issubclass(ExecutionError, BuildError)
    assert issubclass(AssemblyError, BuildError)
    assert issubclass(ExportError, BuildError)
    assert issubclass(ManifestError, BuildError)


def test_validation_error_keeps_problem_list() -> None:
    error = ValidationError(["problem one", "problem two"])

    assert error.problems == ["problem one", "problem two"]
    assert str(error) == "Validation failed: problem one; problem two"
