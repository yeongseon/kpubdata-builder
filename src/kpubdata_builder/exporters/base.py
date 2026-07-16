"""산출물 출력 생성을 위한 기본 내보내기 도구 계약.

이 모듈은 exporter 구현이 따라야 할 공통 인터페이스와 출력 디렉터리 준비
도우미를 정의한다.

주요 구성:
    - ExportResult: 생성된 파일 메타데이터
    - ensure_output_dir: 안전한 출력 파일 경로 준비
    - BaseExporter: exporter 추상 기반 클래스
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ..artifact import ArtifactDataset
from ..errors import ExportError
from ..spec import ExportTarget
from ..stages._path_safety import safe_output_path


@dataclass(frozen=True)
class ExportResult:
    """내보내기 결과 메타데이터를 담는다.

    속성:
        output_path: 생성된 파일 경로.
        file_size: 바이트 단위 파일 크기.
        format: exporter 식별자.
    """

    output_path: Path
    file_size: int
    format: str


def ensure_output_dir(output_dir: Path, relative_output_path: str) -> Path:
    """출력 파일의 부모 디렉터리를 보장하고 최종 경로를 반환한다.

    매개변수:
        output_dir: 빌드의 기준 출력 디렉터리.
        relative_output_path: exporter가 기록할 상대 경로.

    반환값:
        Path: 실제로 기록할 파일 경로.

    예외:
        ExportError: 디렉터리 생성 실패 시.
        PathTraversalError: relative_output_path가 output_dir를 벗어나는 경우 (#210).
    """
    destination = safe_output_path(output_dir, relative_output_path)
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExportError(f"Failed to prepare output directory for {destination}: {exc}") from exc
    return destination


class BaseExporter(ABC):
    """모든 산출물 내보내기 도구를 위한 추상 기반 클래스.

    구현체는 name 속성과 export 메서드를 제공해야 하며,
    BuildSpec의 ExportTarget.kind와 1:1로 대응된다.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """레지스트리와 스펙에서 사용하는 내보내기 도구 식별자를 반환한다."""

    @abstractmethod
    def export(
        self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path
    ) -> ExportResult:
        """산출물을 실제 파일 형식으로 내보낸다.

        매개변수:
            artifact: 내보낼 표준 데이터셋 산출물.
            target: kind, output_path, 옵션을 담은 출력 명세.
            output_dir: 모든 출력의 기준 디렉터리.

        반환값:
            ExportResult: 생성 파일 메타데이터.
        """
        pass


__all__ = ["BaseExporter", "ExportResult", "ensure_output_dir"]
