from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kpubdata_builder.spec import JsonValue
from kpubdata_builder.stages.bronze import (
    BronzeArtifact,
    ProvenanceEvent,
    build_bronze_artifact,
    compute_data_checksum,
    persist_bronze_artifact,
)
from kpubdata_builder.stages.bronze.build import DatasetResult, SourceDataset


@dataclass(frozen=True)
class FakeResult:
    items: list[dict[str, JsonValue]]


class FakeDataset:
    def __init__(self, records: list[dict[str, JsonValue]]) -> None:
        self.records = records
        self.seen_params: dict[str, JsonValue] | None = None

    def list(self, **params: JsonValue) -> DatasetResult:
        self.seen_params = dict(params)
        return FakeResult(items=self.records)


class FakeClient:
    def __init__(self, dataset: FakeDataset) -> None:
        self.dataset_instance = dataset
        self.seen_source_key = ""

    def dataset(self, source_key: str) -> SourceDataset:
        self.seen_source_key = source_key
        return self.dataset_instance


def test_bronze_models_preserve_record_count_and_timezone() -> None:
    fetched_at = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    provenance = ProvenanceEvent(
        source_key="datago.apt_trade",
        fetch_params={"page": 1},
        fetched_at=fetched_at,
    )
    artifact = BronzeArtifact(
        source_key="datago.apt_trade",
        raw_records=({"id": "1"}, {"id": "2"}),
        fetch_params={"page": 1},
        fetched_at=fetched_at,
        provenance=provenance,
    )

    assert artifact.record_count == 2
    assert artifact.fetched_at.tzinfo is not None
    assert artifact.provenance == provenance


def test_bronze_models_reject_naive_fetched_at() -> None:
    naive = datetime(2026, 5, 8, 12, 0)

    with pytest.raises(ValueError, match="timezone-aware"):
        ProvenanceEvent(source_key="datago.apt_trade", fetched_at=naive)

    with pytest.raises(ValueError, match="timezone-aware"):
        BronzeArtifact(source_key="datago.apt_trade", raw_records=(), fetched_at=naive)


def test_build_bronze_artifact_fetches_raw_records_without_transforming() -> None:
    fetched_at = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    records: list[dict[str, JsonValue]] = [
        {"id": "1", "name": "강남구", "amount": 100, "nested": {"b": 2, "a": 1}},
        {"id": "2", "name": "서초구", "amount": None, "tags": ["raw", "bronze"]},
    ]
    dataset = FakeDataset(records)
    client = FakeClient(dataset)

    artifact = build_bronze_artifact(
        client,
        source_key="datago.apt_trade",
        fetch_params={"lawd_cd": "11680", "deal_ymd": "202501"},
        fetched_at=fetched_at,
    )

    assert client.seen_source_key == "datago.apt_trade"
    assert dataset.seen_params == {"lawd_cd": "11680", "deal_ymd": "202501"}
    assert artifact.source_key == "datago.apt_trade"
    assert artifact.fetch_params == {"lawd_cd": "11680", "deal_ymd": "202501"}
    assert artifact.fetched_at == fetched_at
    assert artifact.raw_records == tuple(records)
    assert artifact.raw_records[0] is records[0]
    assert artifact.record_count == 2
    assert artifact.provenance is not None
    assert artifact.provenance.source_key == "datago.apt_trade"
    assert artifact.provenance.fetch_params == {"lawd_cd": "11680", "deal_ymd": "202501"}
    assert artifact.provenance.fetched_at == fetched_at
    assert artifact.provenance.record_count == 2
    assert artifact.provenance.data_checksum != ""


def test_persist_bronze_artifact_writes_jsonl_and_metadata(tmp_path: Path) -> None:
    fetched_at = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    artifact = BronzeArtifact(
        source_key="datago.apt_trade",
        raw_records=(
            {"id": "1", "name": "강남구", "nested": {"b": 2, "a": 1}},
            {"id": "2", "name": "서초구", "amount": None},
        ),
        fetch_params={"lawd_cd": "11680"},
        fetched_at=fetched_at,
        provenance=ProvenanceEvent(
            source_key="datago.apt_trade",
            fetch_params={"lawd_cd": "11680"},
            fetched_at=fetched_at,
            record_count=2,
            data_checksum=compute_data_checksum(
                (
                    {"id": "1", "name": "강남구", "nested": {"b": 2, "a": 1}},
                    {"id": "2", "name": "서초구", "amount": None},
                )
            ),
        ),
    )

    result = persist_bronze_artifact(artifact, output_root=tmp_path / "build", run_id="run-1")

    assert "run-1" in str(result.bronze_dir)
    assert "bronze" in str(result.bronze_dir)
    assert "datago.apt_trade" in str(result.bronze_dir)
    assert result.records_path == result.bronze_dir / "raw_records.jsonl"
    assert result.metadata_path == result.bronze_dir / "metadata.json"

    jsonl_records = [
        json.loads(line) for line in result.records_path.read_text(encoding="utf-8").splitlines()
    ]
    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))

    assert jsonl_records == list(artifact.raw_records)
    assert metadata["source_key"] == "datago.apt_trade"
    assert metadata["fetch_params"] == {"lawd_cd": "11680"}
    assert metadata["fetched_at"] == "2026-05-08T12:00:00+00:00"
    assert metadata["record_count"] == 2
    assert metadata["artifact_paths"] == {
        "records": "raw_records.jsonl",
        "metadata": "metadata.json",
    }
    prov = metadata["provenance"]
    assert prov["operation"] == "fetch"
    assert prov["source_key"] == "datago.apt_trade"
    assert prov["fetch_params"] == {"lawd_cd": "11680"}
    assert prov["fetched_at"] == "2026-05-08T12:00:00+00:00"
    assert prov["record_count"] == 2
    assert prov["data_checksum"] != ""


def test_persist_bronze_artifact_separates_different_params(tmp_path: Path) -> None:
    fetched_at = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    artifact_a = BronzeArtifact(
        source_key="datago.apt_trade",
        raw_records=({"id": "1"},),
        fetch_params={"lawd_cd": "11680"},
        fetched_at=fetched_at,
    )
    artifact_b = BronzeArtifact(
        source_key="datago.apt_trade",
        raw_records=({"id": "2"},),
        fetch_params={"lawd_cd": "11650"},
        fetched_at=fetched_at,
    )

    result_a = persist_bronze_artifact(artifact_a, output_root=tmp_path, run_id="run-1")
    result_b = persist_bronze_artifact(artifact_b, output_root=tmp_path, run_id="run-1")

    assert result_a.bronze_dir != result_b.bronze_dir
    assert result_a.records_path.read_text(encoding="utf-8").strip() == '{"id": "1"}'
    assert result_b.records_path.read_text(encoding="utf-8").strip() == '{"id": "2"}'


def test_persist_bronze_artifact_rejects_unsafe_run_id(tmp_path: Path) -> None:
    fetched_at = datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc)
    artifact = BronzeArtifact(
        source_key="datago.apt_trade",
        raw_records=(),
        fetched_at=fetched_at,
    )

    with pytest.raises(ValueError, match="unsafe characters"):
        persist_bronze_artifact(artifact, output_root=tmp_path, run_id="../escape")

    with pytest.raises(ValueError, match="unsafe characters"):
        persist_bronze_artifact(artifact, output_root=tmp_path, run_id="/absolute")

    with pytest.raises(ValueError, match="must not be empty"):
        persist_bronze_artifact(artifact, output_root=tmp_path, run_id="")


# --- compute_data_checksum ---


def test_compute_data_checksum_deterministic() -> None:
    records: tuple[dict[str, JsonValue], ...] = ({"a": 1, "b": 2}, {"c": 3})
    assert compute_data_checksum(records) == compute_data_checksum(records)


def test_compute_data_checksum_differs_for_different_data() -> None:
    r1: tuple[dict[str, JsonValue], ...] = ({"a": 1},)
    r2: tuple[dict[str, JsonValue], ...] = ({"a": 2},)
    assert compute_data_checksum(r1) != compute_data_checksum(r2)


def test_compute_data_checksum_empty() -> None:
    checksum = compute_data_checksum(())
    assert isinstance(checksum, str)
    assert len(checksum) == 64
