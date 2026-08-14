"""드리프트 감지 단위 테스트 (#445, DRIFT-1; dataset/source 범위 한정은 #486)."""

from __future__ import annotations

import json
from pathlib import Path

from kpubdata_builder.spec import BuildSpec, ExportTarget, SourceRef
from kpubdata_builder.spec.serializer import write_buildspec_snapshot
from kpubdata_builder.stages.silver.drift import detect_drift, find_previous_silver
from kpubdata_builder.tabular import SchemaInfo, TableStatistics
from kpubdata_builder.tabular.types import ColumnInfo

_SCHEMA_A = SchemaInfo(
    columns=(
        ColumnInfo(name="a", dtype="Int64", nullable=False, unique_count=3),
        ColumnInfo(name="b", dtype="Utf8", nullable=True, unique_count=2),
    )
)
_STATS_A = TableStatistics(row_count=100, null_counts={"a": 0, "b": 5}, duplicate_rate=0.1)


class TestDetectDriftSchema:
    def test_column_added(self) -> None:
        schema_b = SchemaInfo(
            columns=_SCHEMA_A.columns
            + (ColumnInfo(name="c", dtype="Float64", nullable=True, unique_count=1),)
        )
        findings = detect_drift(schema_b, _STATS_A, _SCHEMA_A, _STATS_A)
        assert any(f.kind == "column_added" and f.column == "c" for f in findings)

    def test_column_removed(self) -> None:
        schema_b = SchemaInfo(columns=(_SCHEMA_A.columns[0],))
        findings = detect_drift(schema_b, _STATS_A, _SCHEMA_A, _STATS_A)
        assert any(f.kind == "column_removed" and f.column == "b" for f in findings)

    def test_dtype_changed(self) -> None:
        schema_b = SchemaInfo(
            columns=(
                ColumnInfo(name="a", dtype="Float64", nullable=False, unique_count=3),
                ColumnInfo(name="b", dtype="Utf8", nullable=True, unique_count=2),
            )
        )
        findings = detect_drift(schema_b, _STATS_A, _SCHEMA_A, _STATS_A)
        assert any(f.kind == "dtype_changed" and f.column == "a" for f in findings)

    def test_no_schema_drift(self) -> None:
        findings = detect_drift(_SCHEMA_A, _STATS_A, _SCHEMA_A, _STATS_A)
        assert findings == []


class TestDetectDriftStats:
    def test_row_count_jump(self) -> None:
        stats_b = TableStatistics(row_count=500, null_counts={"a": 0, "b": 5}, duplicate_rate=0.1)
        findings = detect_drift(_SCHEMA_A, stats_b, _SCHEMA_A, _STATS_A)
        assert any(f.kind == "row_count_jump" for f in findings)

    def test_small_row_count_change_no_drift(self) -> None:
        stats_b = TableStatistics(row_count=110, null_counts={"a": 0, "b": 5}, duplicate_rate=0.1)
        findings = detect_drift(_SCHEMA_A, stats_b, _SCHEMA_A, _STATS_A)
        assert findings == []

    def test_previous_zero_rows_no_jump(self) -> None:
        stats_prev = TableStatistics(row_count=0, null_counts={}, duplicate_rate=0.0)
        stats_curr = TableStatistics(row_count=100, null_counts={"a": 0}, duplicate_rate=0.0)
        findings = detect_drift(_SCHEMA_A, stats_curr, _SCHEMA_A, stats_prev)
        assert not any(f.kind == "row_count_jump" for f in findings)


def _write_run(
    output_root: Path,
    run_id: str,
    *,
    dataset_id: str,
    source_key: str = "datago.apt_trade",
    row_count: int = 10,
    finished_at: str = "2025-01-01T00:05:00+00:00",
    errors: tuple[str, ...] = (),
) -> None:
    """buildspec.yaml snapshot + manifest.json + silver/{source_key}/schema+stats.json을 기록한다.

    find_previous_silver가 읽는 것과 동일한 파일 배치를 최소한으로 재현하는
    fixture다 — 실제 파이프라인을 돌리지 않고 스코핑 로직만 결정적으로 검증한다.
    """
    spec = BuildSpec(
        dataset_id=dataset_id,
        title="Fixture",
        description="fixture",
        sources=(SourceRef(provider="datago", dataset="apt_trade"),),
        exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
    )
    write_buildspec_snapshot(spec, output_root=output_root, run_id=run_id)
    run_dir = output_root / run_id
    manifest = {
        "started_at": "2025-01-01T00:00:00+00:00",
        "finished_at": finished_at,
        "errors": list(errors),
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    silver_dir = run_dir / "silver" / source_key.replace("/", "_")
    silver_dir.mkdir(parents=True, exist_ok=True)
    schema_payload = {
        "columns": [{"name": "id", "dtype": "Int64", "nullable": False, "unique_count": row_count}]
    }
    stats_payload = {"row_count": row_count, "null_counts": {"id": 0}, "duplicate_rate": 0.0}
    (silver_dir / "schema.json").write_text(json.dumps(schema_payload), encoding="utf-8")
    (silver_dir / "stats.json").write_text(json.dumps(stats_payload), encoding="utf-8")


_APT_TRADE = "datago.apt_trade"


class TestFindPreviousSilverScoping:
    """직전 아무 run이 아니라 동일 dataset_id·source_key의 직전 "성공" run만 찾는다 (#486)."""

    def test_returns_none_when_no_candidates(self, tmp_path: Path) -> None:
        assert find_previous_silver(tmp_path, "run1", dataset_id="d.a", source_key="s") is None

    def test_finds_matching_previous_run(self, tmp_path: Path) -> None:
        _write_run(tmp_path, "run0", dataset_id="d.a", row_count=5)

        found = find_previous_silver(tmp_path, "run1", dataset_id="d.a", source_key=_APT_TRADE)

        assert found is not None
        _schema, stats = found
        assert stats.row_count == 5

    def test_does_not_compare_across_datasets(self, tmp_path: Path) -> None:
        """dataset A run, dataset B run, dataset A new run 순서에서 A new가 B와 비교되지 않는다."""
        _write_run(tmp_path, "a-run1", dataset_id="dataset.a", row_count=10)
        _write_run(tmp_path, "b-run1", dataset_id="dataset.b", row_count=999)

        found = find_previous_silver(
            tmp_path, "a-run2", dataset_id="dataset.a", source_key=_APT_TRADE
        )

        assert found is not None
        _schema, stats = found
        assert stats.row_count == 10  # dataset.b(999)가 아니라 dataset.a의 이전 run.

    def test_does_not_compare_across_sources(self, tmp_path: Path) -> None:
        _write_run(tmp_path, "run0", dataset_id="d.a", source_key="datago.other", row_count=10)

        found = find_previous_silver(tmp_path, "run1", dataset_id="d.a", source_key=_APT_TRADE)

        assert found is None

    def test_excludes_failed_runs(self, tmp_path: Path) -> None:
        _write_run(tmp_path, "run0", dataset_id="d.a", row_count=10, errors=("boom",))

        found = find_previous_silver(tmp_path, "run1", dataset_id="d.a", source_key=_APT_TRADE)

        assert found is None

    def test_excludes_current_run(self, tmp_path: Path) -> None:
        _write_run(tmp_path, "run1", dataset_id="d.a", row_count=10)

        found = find_previous_silver(tmp_path, "run1", dataset_id="d.a", source_key=_APT_TRADE)

        assert found is None

    def test_picks_most_recent_by_finished_at(self, tmp_path: Path) -> None:
        _write_run(
            tmp_path,
            "run-old",
            dataset_id="d.a",
            row_count=1,
            finished_at="2025-01-01T00:00:00+00:00",
        )
        _write_run(
            tmp_path,
            "run-new",
            dataset_id="d.a",
            row_count=2,
            finished_at="2025-06-01T00:00:00+00:00",
        )

        found = find_previous_silver(
            tmp_path, "run-latest", dataset_id="d.a", source_key=_APT_TRADE
        )

        assert found is not None
        _schema, stats = found
        assert stats.row_count == 2

    def test_missing_snapshot_or_stats_are_skipped(self, tmp_path: Path) -> None:
        # snapshot 없는 legacy run.
        legacy_dir = tmp_path / "legacy"
        legacy_dir.mkdir()
        (legacy_dir / "manifest.json").write_text(
            json.dumps({"finished_at": "2025-01-01T00:00:00+00:00", "errors": []}),
            encoding="utf-8",
        )

        found = find_previous_silver(tmp_path, "run1", dataset_id="d.a", source_key=_APT_TRADE)

        assert found is None
