"""산출물 출력 생성을 위한 기본 내보내기 도구 계약."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from ..artifact import ArtifactDataset
from ..errors import ExportError
from ..spec import ExportTarget


@dataclass(frozen=True)
class ExportResult:
    output_path: Path
    file_size: int
    format: str


def ensure_output_dir(output_dir: Path, relative_output_path: str) -> Path:
    destination = output_dir / relative_output_path
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExportError(f"Failed to prepare output directory for {destination}: {exc}") from exc
    return destination


class BaseExporter(ABC):
    """모든 산출물 내보내기 도구를 위한 추상 기반 클래스."""

    @property
    @abstractmethod
    def name(self) -> str:
        """레지스트리와 스펙에서 사용하는 내보내기 도구 식별자를 반환한다."""

    @abstractmethod
    def export(
        self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path
    ) -> ExportResult:
        pass


__all__ = ["BaseExporter", "ExportResult", "ensure_output_dir"]
