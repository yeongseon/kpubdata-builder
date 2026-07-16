"""Tests for scripts/pipeline/fetch.py — checkpoint/resume logic.

These tests exercise pure logic only; no network calls are made.
kpubdata.Client is fully mocked before module import.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: mock kpubdata before loading fetch.py, which does
#   ``from kpubdata import Client`` at module level.
# ---------------------------------------------------------------------------
_kpubdata_mock = MagicMock()
_kpubdata_mock.Client = MagicMock()
sys.modules.setdefault("kpubdata", _kpubdata_mock)

_FETCH_PATH = Path(__file__).parents[2] / "scripts" / "pipeline" / "fetch.py"


def _load_fetch() -> Any:
    spec = importlib.util.spec_from_file_location("_pipeline_fetch", _FETCH_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


fetch_mod = _load_fetch()
fetch_records = fetch_mod.fetch_records
FetchError = fetch_mod.FetchError
_checkpoint_path = fetch_mod._checkpoint_path
_save_checkpoint = fetch_mod._save_checkpoint
_fetch_with_retry = fetch_mod._fetch_with_retry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items


class _FakeDataset:
    """Returns successive batches on each call to list() / list_all()."""

    def __init__(self, batches: list[list[dict[str, Any]]]) -> None:
        self._batches = batches
        self._call_count = 0

    def list(self, **_params: Any) -> _FakeResult:
        result = self._batches[self._call_count]
        self._call_count += 1
        return _FakeResult(result)

    def list_all(self, **_params: Any) -> list[_FakeResult]:
        result = self._batches[self._call_count]
        self._call_count += 1
        return [_FakeResult(result)]


def _make_config(
    fetch_params: list[dict[str, Any]],
    *,
    list_all: bool = False,
) -> dict[str, Any]:
    return {
        "source": {
            "provider": "datago",
            "dataset": "air_quality",
            "list_all": list_all,
            "fetch_params": fetch_params,
        }
    }


def _mock_client(dataset: _FakeDataset) -> MagicMock:
    client = MagicMock()
    client.dataset.return_value = dataset
    return client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_fetch_records_returns_all_items() -> None:
    """fetch_records aggregates items from all fetch_params."""
    ds = _FakeDataset([[{"id": "1"}, {"id": "2"}], [{"id": "3"}]])
    config = _make_config([{"year": 2022}, {"year": 2023}])

    with patch.object(fetch_mod, "Client") as MockClient:
        MockClient.from_env.return_value = _mock_client(ds)
        records = fetch_records(config)

    assert len(records) == 3
    assert records[0]["id"] == "1"
    assert records[2]["id"] == "3"


def test_fetch_records_list_all_mode() -> None:
    """list_all=True uses ds.list_all() instead of ds.list()."""
    ds = _FakeDataset([[{"id": "a"}, {"id": "b"}]])
    config = _make_config([{"year": 2022}], list_all=True)

    with patch.object(fetch_mod, "Client") as MockClient:
        MockClient.from_env.return_value = _mock_client(ds)
        records = fetch_records(config)

    assert [r["id"] for r in records] == ["a", "b"]


def test_fetch_records_saves_checkpoint(tmp_path: Path) -> None:
    """A checkpoint file is written after each successful fetch batch."""
    ds = _FakeDataset([[{"id": "1"}], [{"id": "2"}]])
    config = _make_config([{"year": 2022}, {"year": 2023}])

    with patch.object(fetch_mod, "Client") as MockClient:
        MockClient.from_env.return_value = _mock_client(ds)
        fetch_records(config, checkpoint_dir=tmp_path)

    cp = tmp_path / "fetch_checkpoint.json"
    assert cp.exists()
    data = json.loads(cp.read_text(encoding="utf-8"))
    assert data["next_index"] == 2
    assert len(data["records"]) == 2


def test_fetch_records_resumes_from_checkpoint(tmp_path: Path) -> None:
    """resume=True skips already-fetched batches using the checkpoint."""
    # Pre-write a checkpoint: index 1 already done, 1 record cached.
    cp = tmp_path / "fetch_checkpoint.json"
    cp.write_text(
        json.dumps({"next_index": 1, "records": [{"id": "cached"}]}),
        encoding="utf-8",
    )

    # Only the second batch needs to be fetched.
    ds = _FakeDataset([[{"id": "new"}]])
    config = _make_config([{"year": 2022}, {"year": 2023}])

    with patch.object(fetch_mod, "Client") as MockClient:
        MockClient.from_env.return_value = _mock_client(ds)
        records = fetch_records(config, checkpoint_dir=tmp_path, resume=True)

    assert len(records) == 2
    assert records[0]["id"] == "cached"
    assert records[1]["id"] == "new"


def test_fetch_records_no_checkpoint_when_dir_none() -> None:
    """When checkpoint_dir is None, no checkpoint file is written."""
    ds = _FakeDataset([[{"id": "x"}]])
    config = _make_config([{"year": 2022}])

    with patch.object(fetch_mod, "Client") as MockClient:
        MockClient.from_env.return_value = _mock_client(ds)
        records = fetch_records(config, checkpoint_dir=None)

    assert records == [{"id": "x"}]


def test_checkpoint_path_creates_directory(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b"
    path = _checkpoint_path(nested)
    assert nested.is_dir()
    assert path.name == "fetch_checkpoint.json"


def test_save_checkpoint_writes_atomically(tmp_path: Path) -> None:
    cp = tmp_path / "fetch_checkpoint.json"
    _save_checkpoint(cp, next_index=3, records=[{"id": "r"}])
    data = json.loads(cp.read_text(encoding="utf-8"))
    assert data == {"next_index": 3, "records": [{"id": "r"}]}
    # No .tmp file left behind
    assert not (tmp_path / "fetch_checkpoint.tmp").exists()


def test_fetch_with_retry_raises_fetch_error_after_exhaustion() -> None:
    """Retryable errors raise FetchError after max_retries is exhausted."""
    failing_ds = MagicMock()
    failing_ds.list.side_effect = RuntimeError("503 service unavailable")

    with pytest.raises(FetchError, match="retries"):
        _fetch_with_retry(
            failing_ds,
            {"year": 2022},
            max_retries=1,
            base_delay=0.0,
        )


def test_fetch_with_retry_raises_immediately_for_non_retryable() -> None:
    """Non-retryable errors propagate without waiting for max_retries."""
    failing_ds = MagicMock()
    failing_ds.list.side_effect = ValueError("bad input — not retryable")

    with pytest.raises(ValueError, match="bad input"):
        _fetch_with_retry(failing_ds, {}, max_retries=3, base_delay=0.0)
