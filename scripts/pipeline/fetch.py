"""Fetch records from public data APIs with retry/backoff and checkpoint support."""

from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path
from typing import Any

from kpubdata import Client

logger = logging.getLogger("publish_to_hf.fetch")

# Default retry configuration
MAX_RETRIES = 5
BASE_DELAY = 2.0  # seconds
MAX_DELAY = 120.0  # seconds


class FetchError(Exception):
    """Raised when fetch fails after all retries."""


def fetch_records(
    config: dict[str, Any],
    *,
    checkpoint_dir: Path | None = None,
    resume: bool = False,
) -> list[dict[str, Any]]:
    """Fetch all records from the configured source.

    Args:
        config: Pipeline configuration dict.
        checkpoint_dir: Directory to store fetch checkpoints. If None, no checkpointing.
        resume: If True and checkpoint exists, resume from last successful fetch.

    Returns:
        List of raw record dicts.
    """
    source = config["source"]
    client = Client.from_env()
    ds = client.dataset(f"{source['provider']}.{source['dataset']}")

    all_records: list[dict[str, Any]] = []
    use_list_all = source.get("list_all", False)
    fetch_params_list: list[dict[str, Any]] = source["fetch_params"]
    total = len(fetch_params_list)

    # Determine resume point
    start_index = 0
    checkpoint_file = _checkpoint_path(checkpoint_dir) if checkpoint_dir else None

    if resume and checkpoint_file and checkpoint_file.exists():
        checkpoint_data = json.loads(checkpoint_file.read_text(encoding="utf-8"))
        start_index = checkpoint_data.get("next_index", 0)
        all_records = checkpoint_data.get("records", [])
        logger.info(
            "Resuming from checkpoint: index %d/%d (%d records cached)",
            start_index,
            total,
            len(all_records),
        )

    for i in range(start_index, total):
        params = fetch_params_list[i]
        logger.info("Fetching param set %d/%d: %s", i + 1, total, params)

        records = _fetch_with_retry(ds, params, use_list_all=use_list_all)
        all_records.extend(records)

        # Save checkpoint after each successful fetch
        if checkpoint_file:
            _save_checkpoint(checkpoint_file, next_index=i + 1, records=all_records)

    logger.info("Fetched %d total records", len(all_records))
    return all_records


def _fetch_with_retry(
    ds: Any,
    params: dict[str, Any],
    *,
    use_list_all: bool = False,
    max_retries: int = MAX_RETRIES,
    base_delay: float = BASE_DELAY,
) -> list[dict[str, Any]]:
    """Fetch a single param set with exponential backoff on failure."""
    records: list[dict[str, Any]] = []

    for attempt in range(max_retries + 1):
        try:
            if use_list_all:
                for batch in ds.list_all(**params):
                    records.extend(batch.items)
            else:
                result = ds.list(**params)
                records.extend(result.items)
            return records
        except Exception as exc:
            error_msg = str(exc).lower()
            is_retryable = any(
                token in error_msg
                for token in (
                    "429",
                    "too many requests",
                    "500",
                    "502",
                    "503",
                    "504",
                    "timeout",
                    "connection",
                )
            )

            if not is_retryable or attempt == max_retries:
                if attempt == max_retries:
                    raise FetchError(
                        f"Failed after {max_retries} retries for params {params}: {exc}"
                    ) from exc
                raise

            delay = min(base_delay * (2**attempt) + random.uniform(0, 1), MAX_DELAY)
            logger.warning(
                "Retryable error (attempt %d/%d), waiting %.1fs: %s",
                attempt + 1,
                max_retries,
                delay,
                exc,
            )
            records.clear()
            time.sleep(delay)

    return records  # unreachable, but satisfies type checker


def _checkpoint_path(checkpoint_dir: Path) -> Path:
    """Get the checkpoint file path."""
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    return checkpoint_dir / "fetch_checkpoint.json"


def _save_checkpoint(path: Path, *, next_index: int, records: list[dict[str, Any]]) -> None:
    """Save fetch progress to a checkpoint file."""
    data = {"next_index": next_index, "records": records}
    # Write atomically via temp file
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.rename(path)
