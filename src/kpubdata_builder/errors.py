"""빌더 파이프라인 실패를 위한 오류 계층."""

from __future__ import annotations


class BuildError(Exception):
    """모든 빌더 오류의 기반 예외."""


class SpecLoadError(BuildError):
    """빌드 스펙을 로드하거나 파싱하지 못했다."""


class ValidationError(BuildError):
    """빌드 스펙 검증에 실패했다."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(f"Validation failed: {'; '.join(problems)}")


class ExecutionError(BuildError):
    """소스 실행에 실패했다."""


class AssemblyError(BuildError):
    """산출물 조립에 실패했다."""


class ExportError(BuildError):
    """내보내기 작업에 실패했다."""


class ManifestError(BuildError):
    """매니페스트 쓰기에 실패했다."""


__all__ = [
    "AssemblyError",
    "BuildError",
    "ExecutionError",
    "ExportError",
    "ManifestError",
    "SpecLoadError",
    "ValidationError",
]
