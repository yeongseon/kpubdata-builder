"""Hugging Face Hub publisher — uploads artifacts to a HF dataset repo."""

from __future__ import annotations

import os
from pathlib import Path

from ..errors import PublishError
from .base import BasePublisher, PublishResult


def _repo_path_for(path: Path, common_root: Path | None) -> str:
    """파일 artifact를 레이아웃을 보존하는 repo 상대 경로로 매핑한다.

    공통 상위 디렉터리(common_root) 기준 상대 경로를 사용해 중첩 구조를 유지하고,
    근거가 없으면 basename으로 폴백한다.
    """
    if common_root is not None:
        try:
            return path.relative_to(common_root).as_posix()
        except ValueError:
            pass
    return path.name


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

        # 파일 artifact의 공통 상위 디렉터리를 기준으로 repo 내 경로를 정한다.
        # bare filename으로 평탄화하면 중첩 디렉터리 레이아웃이 사라지고, 서로 다른
        # 디렉터리의 동명 파일이 무경고로 덮어쓰기된다 (#170).
        file_parents = [str(p.parent) for p in artifact_paths if not p.is_dir()]
        common_root: Path | None
        try:
            common_root = Path(os.path.commonpath(file_parents)) if file_parents else None
        except ValueError:
            # 절대/상대 경로가 섞이는 등 공통 경로를 구할 수 없으면 basename으로 폴백.
            common_root = None
        # 공통 루트가 파일시스템 루트("/")이면 의미 있는 공통 상위가 없다는 뜻이다.
        # 그대로 쓰면 무관한 절대경로가 tmp/..., var/... 같은 호스트 경로로 누출되므로
        # "공통 루트 없음"으로 취급해 basename으로 폴백한다 (#205).
        # is_absolute() 가드가 없으면 상대 경로의 commonpath인 Path(".")도 parent==self
        # 조건에 걸려 basename으로 평탄화되는 회귀가 생긴다(상대 경로 레이아웃은 보존해야 함).
        if (
            common_root is not None
            and common_root.is_absolute()
            and common_root.parent == common_root
        ):
            common_root = None

        seen_repo_paths: dict[str, Path] = {}
        for path in artifact_paths:
            if path.is_dir():
                api.upload_folder(
                    folder_path=str(path),
                    repo_id=destination,
                    repo_type="dataset",
                    commit_message="Update dataset via kpubdata-builder",
                )
            else:
                repo_path = _repo_path_for(path, common_root)
                # 두 artifact가 같은 repo 경로로 매핑되면 한쪽이 묻히므로 명시적으로 실패.
                prior = seen_repo_paths.get(repo_path)
                if prior is not None and prior != path:
                    raise PublishError(
                        f"duplicate artifact target path {repo_path!r}: "
                        f"{prior} and {path} would overwrite each other in {destination}"
                    )
                seen_repo_paths[repo_path] = path
                api.upload_file(
                    path_or_fileobj=str(path),
                    path_in_repo=repo_path,
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
