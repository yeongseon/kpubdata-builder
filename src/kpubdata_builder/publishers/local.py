"""Local filesystem publisher — copies artifacts to a destination directory."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .base import BasePublisher


@dataclass(frozen=True)
class LocalPublisher(BasePublisher):
    """Copy artifact files to a local destination directory."""

    destination: Path

    @property
    def name(self) -> str:
        return "local"

    def publish(self, artifact_paths: tuple[Path, ...]) -> None:
        self.destination.mkdir(parents=True, exist_ok=True)
        for src in artifact_paths:
            dst = self.destination / src.name
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
