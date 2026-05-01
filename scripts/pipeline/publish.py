"""Upload packaged dataset to HuggingFace Hub."""

from __future__ import annotations

import logging
import shutil
import sys
from pathlib import Path

logger = logging.getLogger("publish_to_hf.publish")


def upload_to_hf(staging_dir: Path, hf_repo: str, *, dry_run: bool = False) -> None:
    """Upload staged dataset files to HuggingFace Hub.

    Args:
        staging_dir: Directory containing README.md and data/*.parquet.
        hf_repo: HuggingFace repo ID (e.g. 'kpubdata/seoul-apartment-trades').
        dry_run: If True, skip actual upload.
    """
    if dry_run:
        logger.info("[DRY RUN] Would upload %s to %s", staging_dir, hf_repo)
        return

    try:
        from huggingface_hub import HfApi
    except ImportError:
        logger.error(
            "huggingface_hub not installed. Install with: pip install 'kpubdata-builder[publish]'"
        )
        sys.exit(1)

    upload_dir = staging_dir / ".hf_upload"
    if upload_dir.exists():
        shutil.rmtree(upload_dir)
    upload_dir.mkdir(parents=True)

    readme = staging_dir / "README.md"
    if readme.exists():
        shutil.copy2(readme, upload_dir / "README.md")

    data_dir = staging_dir / "data"
    if data_dir.exists():
        dest_data = upload_dir / "data"
        dest_data.mkdir()
        for parquet_file in data_dir.glob("*.parquet"):
            shutil.copy2(parquet_file, dest_data / parquet_file.name)

    api = HfApi()
    api.create_repo(repo_id=hf_repo, repo_type="dataset", exist_ok=True)
    api.upload_folder(
        folder_path=str(upload_dir),
        repo_id=hf_repo,
        repo_type="dataset",
    )
    shutil.rmtree(upload_dir)
    logger.info("Uploaded to https://huggingface.co/datasets/%s", hf_repo)
