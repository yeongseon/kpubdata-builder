"""Hugging Face Hub publisher — uploads artifacts to a HF dataset repo."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .base import BasePublisher


@dataclass(frozen=True)
class HuggingFacePublisher(BasePublisher):
    """Upload artifact files to a Hugging Face Hub dataset repository."""

    repo_id: str
    token_env: str = "HF_TOKEN"
    commit_message: str = "Update dataset via kpubdata-builder"
    revision: str = "main"
    extras: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return "huggingface"

    def publish(self, artifact_paths: tuple[Path, ...]) -> None:
        try:
            from huggingface_hub import HfApi  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "huggingface_hub is required for HuggingFace publishing. "
                "Install it with: pip install huggingface_hub"
            ) from exc

        token = os.environ.get(self.token_env)
        if not token:
            raise RuntimeError(
                f"Environment variable {self.token_env} is not set. "
                "A Hugging Face API token is required for publishing."
            )

        api = HfApi(token=token)

        for path in artifact_paths:
            if path.is_dir():
                api.upload_folder(
                    folder_path=str(path),
                    repo_id=self.repo_id,
                    repo_type="dataset",
                    revision=self.revision,
                    commit_message=self.commit_message,
                )
            else:
                api.upload_file(
                    path_or_fileobj=str(path),
                    path_in_repo=path.name,
                    repo_id=self.repo_id,
                    repo_type="dataset",
                    revision=self.revision,
                    commit_message=self.commit_message,
                )
