"""빌드 매니페스트 모델과 파일 기록 동작을 검증한다."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kpubdata_builder import ManifestError
from kpubdata_builder.manifest import (
    BuildManifest,
    SchemaSummary,
    build_schema_summary,
    build_source_provenance,
    compute_data_checksum,
    manifest_writer,
    write_manifest,
)


def test_build_manifest_instantiation() -> None:
    """필수 필드만으로 BuildManifest를 생성할 수 있어야 한다."""
    started_at = datetime.now(tz=timezone.utc)
    finished_at = datetime.now(tz=timezone.utc)

    manifest = BuildManifest(build_id="build-1", started_at=started_at, finished_at=finished_at)

    assert manifest.build_id == "build-1"


def test_manifest_writer_creates_parent_directories(tmp_path: Path) -> None:
    manifest = BuildManifest(
        build_id="build-1",
        started_at=datetime.now(tz=timezone.utc),
        finished_at=datetime.now(tz=timezone.utc),
    )
    output_path = tmp_path / "nested" / "build" / "manifest.json"

    manifest_writer(manifest, output_path)

    assert output_path.exists()


def test_manifest_writer_wraps_io_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = BuildManifest(
        build_id="build-1",
        started_at=datetime.now(tz=timezone.utc),
        finished_at=datetime.now(tz=timezone.utc),
    )
    output_path = tmp_path / "manifest.json"

    # 매니페스트 쓰기는 이제 temp 파일 + os.replace로 원자적이다 (#204). atomic 교체
    # 단계의 OSError가 ManifestError로 감싸지는지 검증한다.
    def raise_io_error(src: object, dst: object) -> None:
        del src, dst
        raise OSError("disk full")

    monkeypatch.setattr("os.replace", raise_io_error)

    with pytest.raises(ManifestError):
        manifest_writer(manifest, output_path)

    # 실패 시 임시 파일을 남기지 않는다.
    assert list(tmp_path.glob(".manifest_*.tmp")) == []


# --- schema summary tests (#11) ---


def test_build_schema_summary_sets_total_and_preserves_order() -> None:
    summary = build_schema_summary([("id", "String", False), ("amount", "Int64", True)])

    assert summary.total_fields == 2
    assert [(f.name, f.type, f.nullable) for f in summary.fields] == [
        ("id", "String", False),
        ("amount", "Int64", True),
    ]


def test_build_schema_summary_empty() -> None:
    summary = build_schema_summary([])

    assert summary == SchemaSummary(fields=(), total_fields=0)


def test_manifest_writer_serializes_schema_summaries(tmp_path: Path) -> None:
    manifest = BuildManifest(
        build_id="build-1",
        started_at=datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc),
        schema_summaries={
            "datago.apt_trade": build_schema_summary(
                [("id", "String", False), ("amount", "Int64", True)]
            )
        },
    )
    output_path = tmp_path / "manifest.json"

    manifest_writer(manifest, output_path)

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["schema_summaries"] == {
        "datago.apt_trade": {
            "total_fields": 2,
            "fields": [
                {"name": "id", "type": "String", "nullable": False},
                {"name": "amount", "type": "Int64", "nullable": True},
            ],
        }
    }


def test_manifest_writer_omits_schema_summaries_when_empty(tmp_path: Path) -> None:
    manifest = BuildManifest(
        build_id="build-1",
        started_at=datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc),
    )
    output_path = tmp_path / "manifest.json"

    manifest_writer(manifest, output_path)

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["schema_summaries"] == {}


# --- provenance tests (#12) ---


def test_compute_data_checksum_is_reproducible_and_order_independent() -> None:
    a = compute_data_checksum([{"id": "1", "amount": 1000}])
    b = compute_data_checksum([{"amount": 1000, "id": "1"}])

    assert a == b
    assert a.startswith("sha256:")
    assert a != compute_data_checksum([{"id": "2", "amount": 1000}])


def test_build_source_provenance_fills_checksum_count_and_utc_time() -> None:
    kst = timezone(timedelta(hours=9))
    records = [{"id": "1"}, {"id": "2"}]

    prov = build_source_provenance(
        provider="datago",
        dataset="apt_trade",
        fetched_at=datetime(2026, 5, 26, 10, 0, 0, tzinfo=kst),
        records=records,
        params={"page": 1},
    )

    assert prov.provider == "datago"
    assert prov.dataset == "apt_trade"
    assert prov.fetched_at == "2026-05-26T01:00:00+00:00"
    assert prov.record_count == 2
    assert prov.data_checksum == compute_data_checksum(records)
    assert prov.api_version == "unknown"
    assert prov.params == {"page": 1}


def test_manifest_writer_serializes_provenance(tmp_path: Path) -> None:
    prov = build_source_provenance(
        provider="datago",
        dataset="apt_trade",
        fetched_at=datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc),
        records=[{"id": "1"}],
        params={"page": 1},
    )
    manifest = BuildManifest(
        build_id="build-1",
        started_at=datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc),
        provenance=(prov,),
    )
    output_path = tmp_path / "manifest.json"

    manifest_writer(manifest, output_path)

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["provenance"] == [
        {
            "provider": "datago",
            "dataset": "apt_trade",
            "fetched_at": "2026-05-26T01:00:00+00:00",
            "record_count": 1,
            "data_checksum": prov.data_checksum,
            "api_version": "unknown",
            "params": {"page": 1},
        }
    ]


def test_manifest_writer_provenance_defaults_to_empty_list(tmp_path: Path) -> None:
    manifest = BuildManifest(
        build_id="build-1",
        started_at=datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc),
    )
    output_path = tmp_path / "manifest.json"

    manifest_writer(manifest, output_path)

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["provenance"] == []


# --- serialization contract tests (#7) ---


def test_manifest_writer_emits_valid_json_with_all_fields(tmp_path: Path) -> None:
    started_at = datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc)
    finished_at = datetime(2026, 5, 26, 1, 0, 5, tzinfo=timezone.utc)
    manifest = BuildManifest(
        build_id="build-1",
        started_at=started_at,
        finished_at=finished_at,
        inputs=("datago.air_quality",),
        outputs=("out/data.jsonl", "out/README.md"),
        warnings=("schema drift",),
        errors=(),
        row_counts={"silver": 2, "gold": 2},
    )
    output_path = tmp_path / "manifest.json"

    manifest_writer(manifest, output_path)

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["build_id"] == "build-1"
    assert payload["started_at"] == "2026-05-26T01:00:00+00:00"
    assert payload["finished_at"] == "2026-05-26T01:00:05+00:00"
    assert payload["inputs"] == ["datago.air_quality"]
    assert payload["outputs"] == ["out/data.jsonl", "out/README.md"]
    assert payload["warnings"] == ["schema drift"]
    assert payload["errors"] == []
    assert payload["row_counts"] == {"gold": 2, "silver": 2}


def test_manifest_writer_normalizes_timestamps_to_utc(tmp_path: Path) -> None:
    kst = timezone(timedelta(hours=9))
    manifest = BuildManifest(
        build_id="build-1",
        started_at=datetime(2026, 5, 26, 10, 0, 0, tzinfo=kst),
        finished_at=datetime(2026, 5, 26, 10, 0, 0, tzinfo=kst),
    )
    output_path = tmp_path / "manifest.json"

    manifest_writer(manifest, output_path)

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["started_at"] == "2026-05-26T01:00:00+00:00"
    assert payload["finished_at"] == "2026-05-26T01:00:00+00:00"


def test_manifest_writer_output_is_deterministic(tmp_path: Path) -> None:
    manifest = BuildManifest(
        build_id="build-1",
        started_at=datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc),
        row_counts={"silver": 1, "gold": 1, "bronze": 1},
    )
    output_path = tmp_path / "manifest.json"

    manifest_writer(manifest, output_path)

    raw = output_path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert not raw.endswith("\n\n")
    top_level_keys = list(json.loads(raw).keys())
    assert top_level_keys == sorted(top_level_keys)


def test_write_manifest_alias_matches_manifest_writer(tmp_path: Path) -> None:
    manifest = BuildManifest(
        build_id="build-1",
        started_at=datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc),
        outputs=("out/data.jsonl",),
    )
    via_writer = tmp_path / "writer.json"
    via_alias = tmp_path / "alias.json"

    manifest_writer(manifest, via_writer)
    write_manifest(manifest, via_alias)

    assert via_writer.read_bytes() == via_alias.read_bytes()


def test_manifest_writer_is_byte_identical_on_repeat(tmp_path: Path) -> None:
    manifest = BuildManifest(
        build_id="build-1",
        started_at=datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 5, 26, 1, 0, 5, tzinfo=timezone.utc),
        inputs=("b_src", "a_src"),
        outputs=("out/b.jsonl", "out/a.jsonl"),
        row_counts={"b_src": 2, "a_src": 1},
    )
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    manifest_writer(manifest, first)
    manifest_writer(manifest, second)

    assert first.read_bytes() == second.read_bytes()


def test_manifest_writer_preserves_non_ascii(tmp_path: Path) -> None:
    manifest = BuildManifest(
        build_id="build-1",
        started_at=datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc),
        inputs=("datago.대기오염정보",),
        warnings=("스키마 변동 감지",),
    )
    output_path = tmp_path / "manifest.json"

    manifest_writer(manifest, output_path)

    raw = output_path.read_text(encoding="utf-8")
    assert "대기오염정보" in raw
    assert "스키마 변동 감지" in raw
    assert "\\u" not in raw
