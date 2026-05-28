"""빌더 파이프라인 실패를 위한 오류 계층.

이 모듈은 BuildSpec 로드, 검증, 실행, 조립, 내보내기, 매니페스트 작성처럼
파이프라인의 주요 단계별 실패를 구분하기 위한 예외 타입을 정의한다.

주요 클래스:
    - BuildError: 모든 빌더 예외의 공통 기반
    - ValidationError: 여러 검증 문제를 함께 담는 구조화 예외
"""

from __future__ import annotations


class BuildError(Exception):
    """모든 빌더 오류의 기반 예외.

    예외:
        하위 예외들이 이 클래스를 상속하므로 호출자는 BuildError 하나만
        잡아도 빌더 계층 오류를 일괄 처리할 수 있다.
    """


class SpecLoadError(BuildError):
    """빌드 스펙을 로드하거나 파싱하지 못했음을 나타낸다."""


class ValidationError(BuildError):
    """빌드 스펙 검증에 실패했음을 나타낸다.

    속성:
        problems: 검증 단계에서 수집된 개별 오류 메시지 목록.
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__(f"Validation failed: {'; '.join(problems)}")


class ExecutionError(BuildError):
    """소스 실행 또는 실행 준비 과정이 실패했음을 나타낸다."""


class AssemblyError(BuildError):
    """조립 단계에서 필요한 소스 결합을 완료하지 못했음을 나타낸다."""


class ExportError(BuildError):
    """파일 내보내기나 출력 디렉터리 준비가 실패했음을 나타낸다."""


class ManifestError(BuildError):
    """매니페스트 직렬화 또는 디스크 기록이 실패했음을 나타낸다."""


class PublishError(BuildError):
    """산출물 게시(복사/업로드/등록)가 실패했음을 나타낸다."""


__all__ = [
    "AssemblyError",
    "BuildError",
    "ExecutionError",
    "ExportError",
    "ManifestError",
    "PublishError",
    "SpecLoadError",
    "ValidationError",
]
