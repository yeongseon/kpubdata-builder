#!/usr/bin/env python3
"""Publish kpubdata dataset to HuggingFace Hub and/or Kaggle.

This is the CLI entrypoint. All logic lives in the pipeline/ package.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

import yaml
from pipeline.fetch import fetch_records
from pipeline.package import generate_dataset_card, write_parquet
from pipeline.publish import upload_to_hf, upload_to_kaggle
from pipeline.transform import transform_records, validate_schema

logger = logging.getLogger("publish_to_hf")


def load_config(config_path: str) -> dict[str, Any]:
    """Load and validate YAML config file."""
    with open(config_path, encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f)
        if not isinstance(data, dict):
            logger.error("Config file is empty or not a YAML mapping: %s", config_path)
            sys.exit(1)
        return data


def main(argv: list[str] | None = None) -> None:
    """Main entrypoint for the publish pipeline."""
    parser = argparse.ArgumentParser(
        description="Publish kpubdata dataset to HuggingFace and/or Kaggle"
    )
    parser.add_argument("config", help="Path to YAML config file")
    parser.add_argument(
        "--target",
        choices=["hf", "kaggle", "all"],
        default="all",
        help="Upload target: hf (HuggingFace), kaggle, or all (default: all)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Skip upload, generate locally")
    parser.add_argument("--local-only", action="store_true", help="Only generate local files")
    parser.add_argument("--resume", action="store_true", help="Resume from last checkpoint")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    config = load_config(args.config)
    output_cfg = config["output"]
    staging_dir = Path(output_cfg["staging_dir"])

    # Checkpoint directory lives alongside staging
    checkpoint_dir = staging_dir / ".checkpoints"

    records = fetch_records(config, checkpoint_dir=checkpoint_dir, resume=args.resume)
    if not records:
        logger.error("No records fetched. Check API key and fetch_params.")
        sys.exit(1)

    df = transform_records(records, config)
    if df.is_empty():
        logger.error("DataFrame empty after transform. Check filters.")
        sys.exit(1)

    validate_schema(df, config)

    parquet_path = staging_dir / output_cfg["parquet_filename"]
    write_parquet(df, parquet_path)

    card_path = staging_dir / "README.md"
    generate_dataset_card(df, config, card_path)

    if args.local_only:
        logger.info("Local-only mode. Files at: %s", staging_dir)
        return

    target = args.target
    if target in ("hf", "all"):
        upload_to_hf(staging_dir, output_cfg["hf_repo"], dry_run=args.dry_run)
    if target in ("kaggle", "all") and output_cfg.get("kaggle_slug"):
        upload_to_kaggle(staging_dir, config, dry_run=args.dry_run)

    # Clean up checkpoint on successful completion
    if checkpoint_dir.exists():
        import shutil

        shutil.rmtree(checkpoint_dir)
        logger.info("Cleaned up checkpoints after successful publish")


if __name__ == "__main__":
    main()
