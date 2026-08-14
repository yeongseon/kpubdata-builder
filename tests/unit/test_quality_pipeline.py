"""Quality WARN/FAIL gate — orchestrator/preview 통합 테스트 (#486).

WARN이 Build를 계속 진행시키는지, FAIL이 Gold 진입을 막으면서도 quality_results를
manifest에 보존하는지, multi-source에서 source별 결과가 분리되는지, Preview와
Build가 동일 데이터/규칙에 동일 판정을 내리는지를 검증한다.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import cast

from kpubdata_builder.pipeline import run_build
from kpubdata_builder.pipeline.preview import preview_build
from kpubdata_builder.spec import BuildSpec, ExportTarget, JsonValue, SourceRef
from kpubdata_builder.spec.models import (
    CompareColumnsRule,
    QualityPolicy,
    RangeRule,
    SchemaContract,
)


class _FakeResult:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    @property
    def items(self) -> Iterable[dict[str, JsonValue]]:
        return self._items


class _FakeDataset:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    def list(self, **_params: JsonValue) -> _FakeResult:
        return _FakeResult(self._items)


class _FakeClient:
    def __init__(self, data: dict[str, list[dict[str, JsonValue]]]) -> None:
        self._data = data

    def dataset(self, source_key: str) -> _FakeDataset:
        if source_key not in self._data:
            raise KeyError(f"unknown source: {source_key}")
        return _FakeDataset(self._data[source_key])


def _spec(
    *sources: SourceRef, dataset_id: str = "apt_trade", quality: QualityPolicy | None = None
) -> BuildSpec:
    return BuildSpec(
        dataset_id=dataset_id,
        title="Apartment Trades",
        description="seoul apartment trades",
        sources=tuple(sources),
        exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
        quality=quality,
    )


def _manifest(tmp_path: Path, run_id: str) -> dict[str, object]:
    return cast(
        dict[str, object],
        json.loads((tmp_path / run_id / "manifest.json").read_text(encoding="utf-8")),
    )


class TestWarnAllowsProgress:
    def test_warn_violation_lets_gold_run(self, tmp_path: Path) -> None:
        spec = _spec(
            SourceRef(provider="datago", dataset="apt_trade"),
            quality=QualityPolicy(min_rows=100),  # warn 기본, row_count=1 < 100
        )
        client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

        result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

        assert result.status == "ok"
        assert result.outcomes[0].stages_completed == ("bronze", "silver", "gold")
        assert (tmp_path / "run1" / "gold" / "datago.apt_trade").is_dir()

        manifest = _manifest(tmp_path, "run1")
        quality_results = cast(dict[str, object], manifest["quality_results"])
        source_results = cast(list[dict[str, object]], quality_results["datago.apt_trade"])
        min_rows = next(r for r in source_results if r["rule"] == "min_rows")
        assert min_rows["status"] == "warn"


class TestFailBlocksGold:
    def test_fail_violation_blocks_gold_and_fails_source(self, tmp_path: Path) -> None:
        spec = _spec(
            SourceRef(provider="datago", dataset="apt_trade"),
            quality=QualityPolicy(min_rows=100, min_rows_severity="fail"),
        )
        client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

        result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

        assert result.status == "failed"
        outcome = result.outcomes[0]
        assert outcome.status == "failed"
        assert "gold" not in outcome.stages_completed
        assert not (tmp_path / "run1" / "gold" / "datago.apt_trade").exists()
        # Bronze/Silver는 성공적으로 끝났다 — Quality FAIL만이 이유다.
        assert "silver" not in outcome.stages_completed  # persist는 gate 이후이므로 아직 안 됨
        assert (tmp_path / "run1" / "bronze" / "datago.apt_trade").is_dir()

    def test_fail_preserves_quality_results_in_manifest(self, tmp_path: Path) -> None:
        spec = _spec(
            SourceRef(provider="datago", dataset="apt_trade"),
            quality=QualityPolicy(min_rows=100, min_rows_severity="fail"),
        )
        client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

        result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

        assert result.status == "failed"
        manifest = _manifest(tmp_path, "run1")
        quality_results = cast(dict[str, object], manifest["quality_results"])
        assert "datago.apt_trade" in quality_results
        source_results = cast(list[dict[str, object]], quality_results["datago.apt_trade"])
        min_rows = next(r for r in source_results if r["rule"] == "min_rows")
        assert min_rows["status"] == "fail"
        assert cast(dict[str, int], manifest["row_counts"]) == {"datago.apt_trade": 1}

    def test_fetch_failure_does_not_invent_zero_row_count(self, tmp_path: Path) -> None:
        spec = _spec(
            SourceRef(provider="datago", dataset="missing"),
            quality=QualityPolicy(min_rows=1, min_rows_severity="fail"),
        )

        result = run_build(spec, client=_FakeClient({}), output_root=tmp_path, run_id="run1")

        assert result.status == "failed"
        assert cast(dict[str, int], _manifest(tmp_path, "run1")["row_counts"]) == {}


class TestEvaluationErrorGate:
    def test_range_warn_continues_and_fail_blocks_gold(self, tmp_path: Path) -> None:
        client = _FakeClient({"datago.apt_trade": [{"price": "not-a-number"}]})
        warn_spec = _spec(
            SourceRef(provider="datago", dataset="apt_trade"),
            quality=QualityPolicy(
                range=(RangeRule(column="price", min=0, max=100, severity="warn"),)
            ),
        )
        fail_spec = _spec(
            SourceRef(provider="datago", dataset="apt_trade"),
            quality=QualityPolicy(
                range=(RangeRule(column="price", min=0, max=100, severity="fail"),)
            ),
        )

        warn_result = run_build(warn_spec, client=client, output_root=tmp_path, run_id="warn")
        fail_result = run_build(fail_spec, client=client, output_root=tmp_path, run_id="fail")

        assert warn_result.status == "ok"
        assert (tmp_path / "warn" / "gold" / "datago.apt_trade").is_dir()
        assert fail_result.status == "failed"
        assert not (tmp_path / "fail" / "gold" / "datago.apt_trade").exists()
        fail_manifest = _manifest(tmp_path, "fail")
        fail_results = cast(
            list[dict[str, object]],
            cast(dict[str, object], fail_manifest["quality_results"])["datago.apt_trade"],
        )
        range_result = next(r for r in fail_results if r["rule"] == "range")
        assert range_result["status"] == "fail"
        assert range_result["threshold"] == {"min": 0, "max": 100}
        assert range_result["detail"] == (
            "column dtype String cannot be compared with numeric range"
        )

    def test_compare_warn_continues_and_fail_blocks_gold(self, tmp_path: Path) -> None:
        client = _FakeClient({"datago.apt_trade": [{"left": "x", "right": 1}]})

        def spec_for(severity: str) -> BuildSpec:
            return _spec(
                SourceRef(provider="datago", dataset="apt_trade"),
                quality=QualityPolicy(
                    compare_columns=(
                        CompareColumnsRule(
                            left="left", operator="gt", right="right", severity=severity
                        ),
                    )
                ),
            )

        warn_result = run_build(
            spec_for("warn"), client=client, output_root=tmp_path, run_id="warn"
        )
        fail_result = run_build(
            spec_for("fail"), client=client, output_root=tmp_path, run_id="fail"
        )

        assert warn_result.status == "ok"
        assert fail_result.status == "failed"
        assert (tmp_path / "warn" / "gold" / "datago.apt_trade").is_dir()
        assert not (tmp_path / "fail" / "gold" / "datago.apt_trade").exists()


class TestSchemaValidationFailurePreservesQualityResults:
    """기존 #189 legacy schema gate가 실패해도 quality_results는 manifest에 남는다 (#486)."""

    def test_missing_required_column_preserves_schema_check_results(self, tmp_path: Path) -> None:
        spec = BuildSpec(
            dataset_id="apt_trade",
            title="Apartment Trades",
            description="seoul apartment trades",
            sources=(
                SourceRef(
                    provider="datago",
                    dataset="apt_trade",
                    schema=SchemaContract(required=("missing_col",)),
                ),
            ),
            exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
        )
        client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

        result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

        assert result.status == "failed"
        manifest = _manifest(tmp_path, "run1")
        quality_results = cast(dict[str, object], manifest["quality_results"])
        source_results = cast(list[dict[str, object]], quality_results["datago.apt_trade"])
        required = next(r for r in source_results if r["rule"] == "required_column")
        assert required["status"] == "fail"
        assert required["column"] == "missing_col"


class TestPartialMultiSource:
    def test_source_results_are_kept_separate(self, tmp_path: Path) -> None:
        spec = _spec(
            SourceRef(provider="datago", dataset="a"),
            SourceRef(provider="datago", dataset="b"),
            quality=QualityPolicy(min_rows=5, min_rows_severity="fail"),
        )
        client = _FakeClient(
            {
                "datago.a": [{"id": "1"}],  # 1 row < 5 -> FAIL
                "datago.b": [{"id": str(i)} for i in range(10)],  # 10 rows -> PASS
            }
        )

        result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

        assert result.status == "failed"
        a_outcome = next(o for o in result.outcomes if o.source_key == "datago.a")
        b_outcome = next(o for o in result.outcomes if o.source_key == "datago.b")
        assert a_outcome.status == "failed"
        assert b_outcome.status == "ok"

        manifest = _manifest(tmp_path, "run1")
        quality_results = cast(dict[str, object], manifest["quality_results"])
        a_results = cast(list[dict[str, object]], quality_results["datago.a"])
        b_results = cast(list[dict[str, object]], quality_results["datago.b"])
        assert next(r for r in a_results if r["rule"] == "min_rows")["status"] == "fail"
        assert next(r for r in b_results if r["rule"] == "min_rows")["status"] == "pass"
        assert cast(dict[str, int], manifest["row_counts"]) == {
            "datago.a": 1,
            "datago.b": 10,
        }
        # b는 성공했으니 gold까지 완주한다.
        assert (tmp_path / "run1" / "gold" / "datago.b").is_dir()
        assert not (tmp_path / "run1" / "gold" / "datago.a").exists()


class TestPreviewBuildParity:
    def test_preview_and_build_agree_on_same_data_and_rules(self, tmp_path: Path) -> None:
        quality = QualityPolicy(min_rows=5, min_rows_severity="fail", max_duplicate_rate=0.1)
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"), quality=quality)
        records = [{"id": "1"}, {"id": "1"}, {"id": "2"}]
        client = _FakeClient({"datago.apt_trade": records})

        preview_result = preview_build(spec, client=client, limit=5)
        run_build(spec, client=client, output_root=tmp_path, run_id="run1")

        preview_statuses = {r.rule: r.status for r in preview_result.previews[0].quality_results}
        manifest = _manifest(tmp_path, "run1")
        build_statuses = {
            r["rule"]: r["status"]
            for r in cast(
                list[dict[str, object]],
                cast(dict[str, object], manifest["quality_results"])["datago.apt_trade"],
            )
        }
        assert preview_statuses == build_statuses

    def test_preview_persists_no_files(self, tmp_path: Path) -> None:
        quality = QualityPolicy(min_rows=100, min_rows_severity="fail")
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"), quality=quality)
        client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

        preview_build(spec, client=client, limit=5)

        assert not tmp_path.exists() or not any(tmp_path.iterdir())


class TestDriftIntegration:
    """orchestrator가 실제로 dataset/source 범위 한정 drift를 manifest에 기록하는지 (#486)."""

    def test_schema_drift_recorded_in_manifest_for_same_dataset(self, tmp_path: Path) -> None:
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        client_v1 = _FakeClient({"datago.apt_trade": [{"id": "1", "amount": 1000}]})
        client_v2 = _FakeClient({"datago.apt_trade": [{"id": "1", "amount": "1000"}]})  # dtype 변경

        first = run_build(spec, client=client_v1, output_root=tmp_path, run_id="run1")
        assert first.status == "ok"
        second = run_build(spec, client=client_v2, output_root=tmp_path, run_id="run2")
        assert second.status == "ok"

        manifest = _manifest(tmp_path, "run2")
        schema_drift = cast(dict[str, object], manifest["schema_drift"])
        assert "datago.apt_trade" in schema_drift
        findings = cast(list[dict[str, object]], schema_drift["datago.apt_trade"])
        assert any(f["kind"] == "dtype_changed" and f["column"] == "amount" for f in findings)

    def test_no_drift_comparison_across_different_datasets(self, tmp_path: Path) -> None:
        """dataset A run, dataset B run(다른 스키마), dataset A new run 순서에서
        A new run이 B와 비교되어 가짜 drift가 나면 안 된다."""
        spec_a = _spec(SourceRef(provider="datago", dataset="apt_trade"), dataset_id="dataset.a")
        spec_b = _spec(SourceRef(provider="datago", dataset="apt_trade"), dataset_id="dataset.b")
        client_a = _FakeClient({"datago.apt_trade": [{"id": "1", "amount": 1000}]})
        client_b = _FakeClient({"datago.apt_trade": [{"id": "1", "totally_different_column": "x"}]})

        run_a1 = run_build(spec_a, client=client_a, output_root=tmp_path, run_id="a-run1")
        assert run_a1.status == "ok"
        run_b1 = run_build(spec_b, client=client_b, output_root=tmp_path, run_id="b-run1")
        assert run_b1.status == "ok"
        run_a2 = run_build(spec_a, client=client_a, output_root=tmp_path, run_id="a-run2")
        assert run_a2.status == "ok"

        manifest = _manifest(tmp_path, "a-run2")
        schema_drift = cast(dict[str, object], manifest.get("schema_drift", {}))
        # a-run1과 a-run2는 동일 스키마이므로 drift가 없어야 한다(dataset.b와 비교되면
        # totally_different_column 관련 가짜 drift가 생긴다).
        assert schema_drift.get("datago.apt_trade", []) == []
