"""Kaggle dataset publisher — uploads artifacts via the Kaggle API."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .base import BasePublisher


@dataclass(frozen=True)
class KagglePublisher(BasePublisher):
    """Upload a Kaggle-formatted dataset directory via the Kaggle API."""

    dataset_id: str
    message: str = "Update dataset via kpubdata-builder"

    @property
    def name(self) -> str:
        return "kaggle"

    def publish(self, artifact_paths: tuple[Path, ...]) -> None:
        try:
            from kaggle.api.kaggle_api_extended import KaggleApi  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "kaggle is required for Kaggle publishing. Install it with: pip install kaggle"
            ) from exc

        api = KaggleApi()
        api.authenticate()

        for path in artifact_paths:
            if path.is_dir():
                api.dataset_create_version(
                    str(path),
                    version_notes=self.message,
                    dir_mode="zip",
                )
            else:
                raise RuntimeError(
                    f"KagglePublisher expects a directory with dataset-metadata.json, "
                    f"got file: {path}"
                )
