"""Tests for scripts/pipeline/package.py — parquet output and dataset card.

Pure logic tests; no network calls.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

import polars as pl
import pytest

_PACKAGE_PATH = Path(__file__).parents[2] / "scripts" / "pipeline" / "package.py"


def _load_package() -> Any:
    spec = importlib.util.spec_from_file_location("scripts.pipeline.package", _PACKAGE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("scripts.pipeline.package", mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


package_mod = _load_package()
write_parquet = package_mod.write_parquet
generate_dataset_card = package_mod.generate_dataset_card
_size_category = package_mod._size_category


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_config(hf_repo: str = "kpubdata/test-dataset") -> dict[str, Any]:
    return {
        "source": {"source_url": "https://www.data.go.kr"},
        "output": {"hf_repo": hf_repo},
        "card": {
            "title": "Test Dataset",
            "description": "A test dataset.",
            "license": "cc-by-4.0",
            "language": ["ko"],
            "tags": ["korea", "test"],
            "features": [{"name": "id", "description": "Record identifier"}],
        },
    }


# ---------------------------------------------------------------------------
# write_parquet
# ---------------------------------------------------------------------------


def test_write_parquet_creates_file(tmp_path: Path) -> None:
    df = pl.DataFrame({"id": ["1", "2"], "value": [10, 20]})
    out = tmp_path / "data" / "train.parquet"

    result = write_parquet(df, out)

    assert result == out
    assert out.exists()
    assert pl.read_parquet(out).equals(df)


def test_write_parquet_creates_parent_dirs(tmp_path: Path) -> None:
    df = pl.DataFrame({"x": [1]})
    nested = tmp_path / "a" / "b" / "c" / "out.parquet"

    write_parquet(df, nested)

    assert nested.exists()


def test_write_parquet_roundtrip_preserves_schema(tmp_path: Path) -> None:
    df = pl.DataFrame({"name": ["Seoul"], "count": [42]})
    out = tmp_path / "out.parquet"
    write_parquet(df, out)

    loaded = pl.read_parquet(out)
    assert loaded.columns == df.columns
    assert loaded["count"][0] == 42


# ---------------------------------------------------------------------------
# generate_dataset_card
# ---------------------------------------------------------------------------


def test_generate_dataset_card_creates_readme(tmp_path: Path) -> None:
    df = pl.DataFrame({"id": ["1", "2"]})
    config = _minimal_config()
    out = tmp_path / "README.md"

    result = generate_dataset_card(df, config, out)

    assert result == out
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "# Test Dataset" in content
    assert "kpubdata/test-dataset" in content


def test_generate_dataset_card_contains_frontmatter(tmp_path: Path) -> None:
    df = pl.DataFrame({"id": ["1"]})
    config = _minimal_config()
    out = tmp_path / "README.md"

    generate_dataset_card(df, config, out)

    content = out.read_text(encoding="utf-8")
    assert content.startswith("---")
    assert "license: cc-by-4.0" in content
    assert "- korea" in content


def test_generate_dataset_card_with_variants(tmp_path: Path) -> None:
    df = pl.DataFrame({"id": ["1"]})
    config = _minimal_config()
    out = tmp_path / "README.md"

    generate_dataset_card(df, config, out, variant_names=["ko", "en"])

    content = out.read_text(encoding="utf-8")
    assert "config_name: ko" in content
    assert "config_name: en" in content
    assert "default_config_name: en" in content


def test_generate_dataset_card_sample_table(tmp_path: Path) -> None:
    df = pl.DataFrame({"id": ["r1", "r2", "r3", "r4", "r5", "r6"]})
    config = _minimal_config()
    out = tmp_path / "README.md"

    generate_dataset_card(df, config, out)

    content = out.read_text(encoding="utf-8")
    # Sample table capped at 5 rows — r6 should not appear
    assert "r5" in content
    assert "r6" not in content


def test_generate_dataset_card_stats_for_numeric(tmp_path: Path) -> None:
    df = pl.DataFrame({"id": ["1", "2"], "count": [10, 20]})
    config = _minimal_config()
    out = tmp_path / "README.md"

    generate_dataset_card(df, config, out)

    content = out.read_text(encoding="utf-8")
    assert "## Statistics" in content
    assert "`count`" in content


# ---------------------------------------------------------------------------
# _size_category
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (0, "n<1K"),
        (999, "n<1K"),
        (1_000, "1K<n<10K"),
        (9_999, "1K<n<10K"),
        (10_000, "10K<n<100K"),
        (99_999, "10K<n<100K"),
        (100_000, "100K<n<1M"),
        (999_999, "100K<n<1M"),
        (1_000_000, "1M<n<10M"),
        (9_999_999, "1M<n<10M"),
        (10_000_000, "n>10M"),
    ],
)
def test_size_category(n: int, expected: str) -> None:
    assert _size_category(n) == expected
