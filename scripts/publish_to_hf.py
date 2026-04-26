#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import re
import shutil
import sys
from pathlib import Path
from typing import Any

import polars as pl
import yaml
from kpubdata import Client

logger = logging.getLogger("publish_to_hf")

_FILTER_RE = re.compile(r"(\w+)\s*(>=|<=|!=|==|>|<)\s*(.+)")


def load_config(config_path: str) -> dict[str, Any]:
    with open(config_path, encoding="utf-8") as f:
        data: dict[str, Any] = yaml.safe_load(f)
        if not isinstance(data, dict):
            logger.error("Config file is empty or not a YAML mapping: %s", config_path)
            sys.exit(1)
        return data


def fetch_records(config: dict[str, Any]) -> list[dict[str, Any]]:
    source = config["source"]
    client = Client.from_env()
    ds = client.dataset(f"{source['provider']}.{source['dataset']}")

    all_records: list[dict[str, Any]] = []
    use_list_all = source.get("list_all", False)

    for i, params in enumerate(source["fetch_params"]):
        logger.info("Fetching param set %d/%d: %s", i + 1, len(source["fetch_params"]), params)

        if use_list_all:
            for batch in ds.list_all(**params):
                all_records.extend(batch.items)
        else:
            result = ds.list(**params)
            all_records.extend(result.items)

    logger.info("Fetched %d total records", len(all_records))
    return all_records


def transform_records(records: list[dict[str, Any]], config: dict[str, Any]) -> pl.DataFrame:
    transform = config["transform"]
    column_mapping: dict[str, str] = transform["column_mapping"]
    dtypes: dict[str, str] = transform.get("dtypes", {})

    mapped: list[dict[str, Any]] = []
    for record in records:
        row: dict[str, Any] = {}
        for raw_key, clean_key in column_mapping.items():
            val = record.get(raw_key)
            row[clean_key] = str(val) if val is not None else None
        mapped.append(row)

    # Force all columns to Utf8 to avoid mixed-type inference errors
    schema = {clean: pl.Utf8 for clean in column_mapping.values()}
    df = pl.DataFrame(mapped, schema=schema)
    logger.info("Raw DataFrame: %d rows x %d cols", df.height, df.width)

    for col, dtype_str in dtypes.items():
        if col not in df.columns:
            continue
        df = _cast_column(df, col, dtype_str)

    for derived in transform.get("derived", []):
        df = _add_derived_column(df, derived)

    pre_filter_count = df.height
    for filter_expr in transform.get("filters", []):
        df = _apply_filter(df, filter_expr)
    if df.height != pre_filter_count:
        logger.info("Filtered: %d -> %d rows", pre_filter_count, df.height)

    logger.info("Transformed DataFrame: %d rows x %d cols", df.height, df.width)
    return df


def _apply_filter(df: pl.DataFrame, expr_str: str) -> pl.DataFrame:
    """Apply a simple comparison filter: 'col >= value', 'col != value', etc."""
    match = _FILTER_RE.match(expr_str.strip())
    if not match:
        logger.warning("Cannot parse filter expression: %s", expr_str)
        return df

    col, op, val_str = match.group(1), match.group(2), match.group(3).strip()

    if col not in df.columns:
        logger.warning("Filter references unknown column '%s', skipping", col)
        return df

    try:
        val: int | float = int(val_str)
    except ValueError:
        val = float(val_str)

    ops = {
        ">": pl.col(col) > val,
        "<": pl.col(col) < val,
        ">=": pl.col(col) >= val,
        "<=": pl.col(col) <= val,
        "==": pl.col(col) == val,
        "!=": pl.col(col) != val,
    }
    return df.filter(ops[op])


_NULL_TOKENS = {"", "-", "N/A", "null", "None"}


def _nullify_tokens(col: str) -> pl.Expr:
    return (
        pl.when(pl.col(col).cast(pl.Utf8).str.strip_chars().is_in(list(_NULL_TOKENS)))
        .then(pl.lit(None, dtype=pl.Utf8))
        .otherwise(pl.col(col).cast(pl.Utf8).str.strip_chars())
        .alias(col)
    )


def _cast_column(df: pl.DataFrame, col: str, dtype_str: str) -> pl.DataFrame:
    if dtype_str == "int_comma":
        return df.with_columns(
            pl.col(col)
            .cast(pl.Utf8)
            .str.replace_all(",", "")
            .str.strip_chars()
            .cast(pl.Int64, strict=False)
            .alias(col)
        )
    if dtype_str == "int":
        df = df.with_columns(_nullify_tokens(col))
        return df.with_columns(pl.col(col).cast(pl.Int64, strict=False).alias(col))
    if dtype_str == "float":
        df = df.with_columns(_nullify_tokens(col))
        return df.with_columns(pl.col(col).cast(pl.Float64, strict=False).alias(col))
    if dtype_str == "str":
        return df.with_columns(pl.col(col).cast(pl.Utf8).alias(col))
    logger.warning("Unknown dtype '%s' for column '%s', skipping cast", dtype_str, col)
    return df


def _add_derived_column(df: pl.DataFrame, derived: dict[str, Any]) -> pl.DataFrame:
    name = derived["name"]
    expr = derived["expr"]
    dtype_str = derived.get("dtype", "str")

    if expr.startswith("concat_date("):
        inner = expr[len("concat_date(") : -1]
        col_refs = [p.strip() for p in inner.split(",")]
        if len(col_refs) == 3:
            year_col, month_col, day_col = col_refs
            polars_expr = (
                pl.col(year_col).cast(pl.Utf8)
                + pl.lit("-")
                + pl.col(month_col).cast(pl.Utf8).str.zfill(2)
                + pl.lit("-")
                + pl.col(day_col).cast(pl.Utf8).str.zfill(2)
            ).alias(name)
            df = df.with_columns(polars_expr)
        else:
            logger.warning("concat_date requires exactly 3 columns, got %d", len(col_refs))
            return df
    elif expr.startswith("format("):
        inner = expr[len("format(") : -1]
        parts = [p.strip() for p in inner.split(",")]
        fmt_str = parts[0].strip("'\"")
        col_refs = [p.strip() for p in parts[1:]]
        polars_expr = pl.format(fmt_str, *[pl.col(c) for c in col_refs]).alias(name)
        df = df.with_columns(polars_expr)
    else:
        logger.warning("Unsupported derived expression: %s", expr)
        return df

    if dtype_str != "str":
        df = _cast_column(df, name, dtype_str)
    return df


def write_parquet(df: pl.DataFrame, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(output_path)
    size_mb = output_path.stat().st_size / (1024 * 1024)
    logger.info("Wrote parquet: %s (%.2f MB, %d rows)", output_path, size_mb, df.height)
    return output_path


def generate_dataset_card(df: pl.DataFrame, config: dict[str, Any], output_path: Path) -> Path:
    card = config["card"]
    output_cfg = config["output"]

    features_table = _build_features_table(card.get("features", []))
    sample_table = _build_sample_table(df, max_rows=5)
    stats_section = _build_stats_section(df)

    tags_yaml = "\n".join(f"- {t}" for t in card.get("tags", []))
    languages_yaml = "\n".join(f"- {lang}" for lang in card.get("language", ["ko"]))
    task_categories = card.get("task_categories", [])
    task_categories_yaml = "\n".join(f"- {t}" for t in task_categories)
    task_categories_block = f"task_categories:\n{task_categories_yaml}\n" if task_categories else ""
    hf_repo = output_cfg["hf_repo"]
    source_url = config["source"].get("source_url", "https://www.data.go.kr")
    attribution = card.get("attribution", "")
    attribution_block = f"\n{attribution}\n" if attribution else ""

    content = f"""---
license: {card.get("license", "cc-by-4.0")}
language:
{languages_yaml}
tags:
{tags_yaml}
size_categories:
- {_size_category(df.height)}
{task_categories_block}---

# {card["title"]}

{card["description"]}

## Dataset Summary

- **Records**: {df.height:,}
- **Features**: {df.width}
- **Source**: [data.go.kr]({source_url})
- **HuggingFace Repo**: [{hf_repo}](https://huggingface.co/datasets/{hf_repo})

{stats_section}

## Features

{features_table}

## Sample Data

{sample_table}

## Usage

```python
from datasets import load_dataset

ds = load_dataset("{hf_repo}")
df = ds["train"].to_pandas()
print(df.head())
```

## Source

This dataset was generated using [kpubdata](https://github.com/yeongseon/kpubdata)
from public data APIs on data.go.kr.
{attribution_block}"""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    logger.info("Wrote dataset card: %s", output_path)
    return output_path


def _build_features_table(features: list[dict[str, str]]) -> str:
    if not features:
        return ""
    lines = ["| Feature | Description |", "| :--- | :--- |"]
    for f in features:
        escaped_desc = f["description"].replace("|", "\\|")
        lines.append(f"| `{f['name']}` | {escaped_desc} |")
    return "\n".join(lines)


def _build_sample_table(df: pl.DataFrame, max_rows: int = 5) -> str:
    sample = df.head(max_rows)
    header = "| " + " | ".join(sample.columns) + " |"
    separator = "| " + " | ".join("---" for _ in sample.columns) + " |"
    rows: list[str] = []
    for row in sample.iter_rows(named=True):
        cells = " | ".join(str(v).replace("|", "\\|") for v in row.values())
        rows.append(f"| {cells} |")
    return "\n".join([header, separator, *rows])


def _build_stats_section(df: pl.DataFrame) -> str:
    numeric_cols = [
        col for col, dtype in zip(df.columns, df.dtypes, strict=True) if dtype.is_numeric()
    ]
    if not numeric_cols:
        return ""

    lines = [
        "## Statistics",
        "",
        "| Feature | Mean | Std | Min | Max |",
        "| :--- | ---: | ---: | ---: | ---: |",
    ]
    for col in numeric_cols:
        series = df[col].drop_nulls()
        if series.is_empty():
            continue
        mean_val = series.mean()
        std_val = series.std()
        mean = float(str(mean_val)) if mean_val is not None else 0.0
        std = float(str(std_val)) if std_val is not None else 0.0
        min_val = str(series.min())
        max_val = str(series.max())
        lines.append(f"| `{col}` | {mean:,.2f} | {std:,.2f} | {min_val} | {max_val} |")
    return "\n".join(lines)


def _size_category(n: int) -> str:
    if n < 1_000:
        return "n<1K"
    if n < 10_000:
        return "1K<n<10K"
    if n < 100_000:
        return "10K<n<100K"
    if n < 1_000_000:
        return "100K<n<1M"
    if n < 10_000_000:
        return "1M<n<10M"
    return "n>10M"


def upload_to_hf(staging_dir: Path, hf_repo: str, *, dry_run: bool = False) -> None:
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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Publish kpubdata dataset to HuggingFace Hub")
    parser.add_argument("config", help="Path to YAML config file")
    parser.add_argument("--dry-run", action="store_true", help="Skip HF upload, generate locally")
    parser.add_argument("--local-only", action="store_true", help="Only generate local files")
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    config = load_config(args.config)
    output_cfg = config["output"]
    staging_dir = Path(output_cfg["staging_dir"])

    records = fetch_records(config)
    if not records:
        logger.error("No records fetched. Check API key and fetch_params.")
        sys.exit(1)

    df = transform_records(records, config)
    if df.is_empty():
        logger.error("DataFrame empty after transform. Check filters.")
        sys.exit(1)

    parquet_path = staging_dir / output_cfg["parquet_filename"]
    write_parquet(df, parquet_path)

    card_path = staging_dir / "README.md"
    generate_dataset_card(df, config, card_path)

    if args.local_only:
        logger.info("Local-only mode. Files at: %s", staging_dir)
        return

    upload_to_hf(staging_dir, output_cfg["hf_repo"], dry_run=args.dry_run)


if __name__ == "__main__":
    main()
