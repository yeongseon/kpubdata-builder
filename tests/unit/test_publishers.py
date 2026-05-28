from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kpubdata_builder.publishers import PUBLISHER_REGISTRY, LocalPublisher
from kpubdata_builder.publishers.huggingface import HuggingFacePublisher


def test_publisher_registry_contains_local_and_huggingface() -> None:
    assert "local" in PUBLISHER_REGISTRY
    assert "huggingface" in PUBLISHER_REGISTRY


def test_local_publisher_copies_files(tmp_path: Path) -> None:
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    (src_dir / "data.parquet").write_text("fake parquet")
    (src_dir / "README.md").write_text("# Dataset")

    dest = tmp_path / "dest"
    publisher = LocalPublisher(destination=dest)

    assert publisher.name == "local"
    publisher.publish((src_dir / "data.parquet", src_dir / "README.md"))

    assert (dest / "data.parquet").read_text() == "fake parquet"
    assert (dest / "README.md").read_text() == "# Dataset"


def test_local_publisher_copies_directories(tmp_path: Path) -> None:
    src_dir = tmp_path / "src" / "data"
    src_dir.mkdir(parents=True)
    (src_dir / "train.parquet").write_text("train data")

    dest = tmp_path / "dest"
    publisher = LocalPublisher(destination=dest)
    publisher.publish((src_dir,))

    assert (dest / "data" / "train.parquet").read_text() == "train data"


def test_huggingface_publisher_name() -> None:
    publisher = HuggingFacePublisher(repo_id="kpubdata/test")
    assert publisher.name == "huggingface"


def test_huggingface_publisher_requires_token(tmp_path: Path) -> None:
    publisher = HuggingFacePublisher(repo_id="kpubdata/test", token_env="MISSING_TOKEN")
    src = tmp_path / "data.parquet"
    src.write_text("data")

    fake_hf = MagicMock()
    with (
        patch.dict("os.environ", {}, clear=True),
        patch.dict("sys.modules", {"huggingface_hub": fake_hf}),
        pytest.raises(RuntimeError, match="not set"),
    ):
        publisher.publish((src,))


def test_huggingface_publisher_requires_library(tmp_path: Path) -> None:
    publisher = HuggingFacePublisher(repo_id="kpubdata/test")
    src = tmp_path / "data.parquet"
    src.write_text("data")

    with (
        patch.dict("os.environ", {"HF_TOKEN": "fake-token"}),
        patch.dict("sys.modules", {"huggingface_hub": None}),
        pytest.raises(RuntimeError, match="huggingface_hub"),
    ):
        publisher.publish((src,))
