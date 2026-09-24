"""Upload packaged dataset to HuggingFace Hub and/or Kaggle.

DEPRECATED — legacy publish path (#208). The canonical execution path is the
medallion orchestrator ``kpubdata_builder.pipeline.run_build``. See
``docs/ARCHITECTURE.md``.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Any

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
        shutil.copytree(data_dir, upload_dir / "data", dirs_exist_ok=True)

    api = HfApi()
    # private 를 명시한다. 생략하면 huggingface_hub 의 기본값에 맡기게 되는데,
    # 그 기본값은 버전에 따라 달라질 수 있는 값이고 공개 여부는 추측할 일이 아니다.
    api.create_repo(repo_id=hf_repo, repo_type="dataset", exist_ok=True, private=False)
    api.upload_folder(
        folder_path=str(upload_dir),
        repo_id=hf_repo,
        repo_type="dataset",
        # 이전 리비전에만 있던 파일을 지운다. 없으면 이름이 바뀐 옛 파일이
        # 영원히 남는다 — REPUBLISH_GUIDE 의 수동 삭제 절차가 그 증거였다.
        delete_patterns=["data/*", "README.md"],
    )
    shutil.rmtree(upload_dir)
    logger.info("Uploaded to https://huggingface.co/datasets/%s", hf_repo)


def upload_to_kaggle(
    staging_dir: Path,
    config: dict[str, Any],
    *,
    dry_run: bool = False,
    public: bool = False,
) -> None:
    """Upload staged dataset files to Kaggle.

    Args:
        staging_dir: Directory containing data/*.parquet.
        config: Full pipeline config (needs output.kaggle_slug and card info).
        dry_run: If True, skip actual upload.
    """
    output_cfg = config["output"]
    kaggle_slug = output_cfg.get("kaggle_slug")
    if not kaggle_slug:
        logger.error("No kaggle_slug in output config. Skipping Kaggle upload.")
        return

    card = config["card"]

    try:
        from kaggle.api.kaggle_api_extended import KaggleApi  # type: ignore[import-untyped]
    except ImportError:
        logger.error("kaggle not installed. Install with: pip install 'kpubdata-builder[publish]'")
        sys.exit(1)

    upload_dir = staging_dir / ".kaggle_upload"
    if upload_dir.exists():
        shutil.rmtree(upload_dir)
    upload_dir.mkdir(parents=True)

    data_dir = staging_dir / "data"
    if data_dir.exists():
        shutil.copytree(data_dir, upload_dir / "data", dirs_exist_ok=True)

    description = card.get("description", "").strip()
    attribution = card.get("attribution", "").strip()
    if attribution:
        description = f"{description}\n\n{attribution}"

    license_name = _map_kaggle_license(card.get("license", "cc-by-4.0"))

    metadata: dict[str, Any] = {
        "title": card["title"],
        "id": kaggle_slug,
        "licenses": [{"name": license_name}],
    }

    subtitle = card.get("subtitle", "")
    if not subtitle:
        subtitle = description.split("\n")[0][:80].strip()
    if 20 <= len(subtitle) <= 80:
        metadata["subtitle"] = subtitle

    if description:
        metadata["description"] = description

    tags = card.get("tags", [])
    if tags:
        metadata["keywords"] = tags

    metadata_path = upload_dir / "dataset-metadata.json"
    metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Wrote Kaggle metadata: %s", metadata_path)

    if dry_run:
        logger.info("[DRY RUN] Would upload %s to Kaggle as %s", upload_dir, kaggle_slug)
        shutil.rmtree(upload_dir)
        return

    api = KaggleApi()
    api.authenticate()

    try:
        results = api.dataset_list(mine=True, search=kaggle_slug.split("/")[-1])
        dataset_exists = any(str(d) == kaggle_slug for d in results)
    except Exception as exc:
        # 조회가 실패했다는 것은 "없다" 가 아니라 "모른다" 다. False 로 떨어뜨리면
        # 이미 있는 데이터셋에 create_new 를 시도해 실패하거나, 최악의 경우 의도
        # 밖의 새 데이터셋을 만든다. 모르는 채로 쓰지 않는다.
        logger.error("Kaggle dataset lookup failed for %s: %s", kaggle_slug, exc)
        shutil.rmtree(upload_dir)
        raise

    if dataset_exists:
        api.dataset_create_version(
            folder=str(upload_dir),
            version_notes="Updated dataset",
            quiet=False,
            convert_to_csv=False,
            delete_old_versions=False,
            dir_mode="skip",
        )
        logger.info("Updated Kaggle dataset: https://www.kaggle.com/datasets/%s", kaggle_slug)
    else:
        api.dataset_create_new(
            folder=str(upload_dir),
            # CLI publish 는 --public opt-in 인데 이 경로만 무조건 공개였다.
            # 두 경로가 같은 데이터셋을 다른 공개 정책으로 올리고 있었다.
            public=public,
            quiet=False,
            convert_to_csv=False,
            dir_mode="skip",
        )
        logger.info("Created Kaggle dataset: https://www.kaggle.com/datasets/%s", kaggle_slug)

    shutil.rmtree(upload_dir)


def _map_kaggle_license(hf_license: str) -> str:
    """Map HuggingFace license string to Kaggle license name."""
    mapping = {
        "cc-by-4.0": "CC-BY-4.0",
        "cc0-1.0": "CC0-1.0",
        "cc-by-sa-4.0": "CC-BY-SA-4.0",
        "cc-by-nc-4.0": "CC-BY-NC-4.0",
        "cc-by-nc-sa-4.0": "CC-BY-NC-SA-4.0",
        "apache-2.0": "apache-2.0",
        "mit": "other",
        "odc-by": "ODC-BY-1.0",
        "odbl": "ODbL-1.0",
    }
    return mapping.get(hf_license.lower(), "other")
