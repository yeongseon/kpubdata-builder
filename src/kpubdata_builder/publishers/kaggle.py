"""Kaggle API를 통해 데이터셋을 퍼블리시하는 publisher."""

from __future__ import annotations

from pathlib import Path

from ..errors import PublishError
from .base import BasePublisher, PublishResult


class KagglePublisher(BasePublisher):
    """Kaggle API로 dataset-metadata.json이 있는 디렉터리를 업로드한다."""

    @property
    def name(self) -> str:
        return "kaggle"

    def publish(self, artifact_paths: tuple[Path, ...], *, destination: str) -> PublishResult:
        """Kaggle 데이터셋을 새 버전으로 업로드한다.

        Args:
            artifact_paths: dataset-metadata.json을 포함하는 디렉터리 경로.
            destination: Kaggle dataset ID (e.g. "username/dataset-name").
        """
        try:
            from kaggle.api.kaggle_api_extended import KaggleApi  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "kaggle is required for Kaggle publishing. Install it with: pip install kaggle"
            ) from exc

        api = KaggleApi()
        api.authenticate()
        count = 0

        for path in artifact_paths:
            if not path.is_dir():
                raise PublishError(
                    f"KagglePublisher expects a directory with dataset-metadata.json, "
                    f"got file: {path}"
                )

            try:
                results = api.dataset_list(mine=True, search=destination.split("/")[-1])
                dataset_exists = any(str(d) == destination for d in results)
            except Exception:
                dataset_exists = False

            try:
                if dataset_exists:
                    api.dataset_create_version(
                        str(path),
                        version_notes="Update via kpubdata-builder",
                        dir_mode="zip",
                    )
                else:
                    api.dataset_create_new(folder=str(path), dir_mode="zip", public=True)
            except Exception as exc:
                raise PublishError(
                    f"Failed to publish Kaggle dataset to {destination}: {exc}"
                ) from exc
            count += 1

        return PublishResult(
            publisher=self.name,
            reference=f"https://www.kaggle.com/datasets/{destination}",
            artifact_count=count,
        )
