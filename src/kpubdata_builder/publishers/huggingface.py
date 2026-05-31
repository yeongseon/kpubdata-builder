"""Hugging Face Hub publisher — uploads artifacts to a HF dataset repo."""

from __future__ import annotations

import os
from pathlib import Path

from .base import BasePublisher, PublishResult


class HuggingFacePublisher(BasePublisher):
    """Upload artifact files to a Hugging Face Hub dataset repository."""

    @property
    def name(self) -> str:
        return "huggingface"

    def publish(self, artifact_paths: tuple[Path, ...], *, destination: str) -> PublishResult:
        """Publish artifacts to a HuggingFace dataset repo.

        Args:
            artifact_paths: Files/directories to upload.
            destination: HF repo ID (e.g. "kpubdata/air-quality").
        """
        try:
            from huggingface_hub import HfApi  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "huggingface_hub is required for HuggingFace publishing. "
                "Install it with: pip install huggingface_hub"
            ) from exc

        token = os.environ.get("HF_TOKEN")
        if not token:
            raise RuntimeError(
                "Environment variable HF_TOKEN is not set. "
                "A Hugging Face API token is required for publishing."
            )

        api = HfApi(token=token)
        count = 0

        for path in artifact_paths:
            if path.is_dir():
                api.upload_folder(
                    folder_path=str(path),
                    repo_id=destination,
                    repo_type="dataset",
                    commit_message="Update dataset via kpubdata-builder",
                )
            else:
                api.upload_file(
                    path_or_fileobj=str(path),
                    path_in_repo=path.name,
                    repo_id=destination,
                    repo_type="dataset",
                    commit_message="Update dataset via kpubdata-builder",
                )
            count += 1

        return PublishResult(
            publisher=self.name,
            reference=f"https://huggingface.co/datasets/{destination}",
            artifact_count=count,
        )
