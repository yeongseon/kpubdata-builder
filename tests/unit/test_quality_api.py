"""Quality History/Detail API 테스트 (#486).

``GET /datasets/{dataset_id}/quality/history``와 ``GET /builds/{run_id}/quality``가
#488 dataset→run 조회 helper/ownership semantics를 재사용해 올바르게 집계·노출하는지
검증한다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest
import yaml

import kpubdata_builder.service.app as app_module
from kpubdata_builder.service import BuilderService, dispatch
from kpubdata_builder.service.app import _OWNERSHIP_ENV
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.spec import JsonValue, parse_spec
from kpubdata_builder.spec.serializer import write_buildspec_snapshot

_SPEC_YAML = (
    "dataset_id: {dataset_id}\n"
    "title: Fixture\n"
    "description: fixture\n"
    "sources:\n"
    "  - provider: datago\n"
    "    dataset: air_quality\n"
    "    alias: air\n"
    "exports:\n"
    "  - kind: jsonl\n"
    "    output_path: out/data.jsonl\n"
)


def _write_fixture_run(
    tmp_path: Path,
    run_id: str,
    *,
    dataset_id: str,
    quality_results: dict[str, list[dict[str, object]]] | None = None,
    row_counts: dict[str, int] | None = None,
    errors: tuple[str, ...] = (),
    started_at: str = "2025-01-01T00:00:00+00:00",
    finished_at: str = "2025-01-01T00:05:00+00:00",
    created_by: str | None = None,
    include_quality_key: bool = True,
    inputs: tuple[str, ...] | None = None,
) -> None:
    """실제 파이프라인 없이 canonical snapshot + manifest만 기록하는 결정적 fixture.

    test_dataset_api.py의 동일 패턴(_write_fixture_run)을 재사용한다 — #488의
    dataset→run 조회가 파일시스템 정본(snapshot+manifest)만으로 동작함을
    전제하므로, quality 집계 로직도 같은 최소 fixture로 검증할 수 있다.
    """
    spec_yaml = _SPEC_YAML.format(dataset_id=dataset_id)
    spec = parse_spec(cast(dict[str, object], yaml.safe_load(spec_yaml)))
    write_buildspec_snapshot(spec, output_root=tmp_path, run_id=run_id)

    run_dir = tmp_path / run_id
    manifest: dict[str, object] = {
        "started_at": started_at,
        "finished_at": finished_at,
        "errors": list(errors),
        "row_counts": row_counts or {},
        "created_by": created_by,
    }
    if inputs is not None:
        manifest["inputs"] = list(inputs)
    if include_quality_key:
        manifest["quality_results"] = quality_results or {}
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


class _FakeResult:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    @property
    def items(self) -> list[dict[str, JsonValue]]:
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


def _service(tmp_path: Path) -> BuilderService:
    client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]})
    return BuilderService(output_root=tmp_path, client_factory=lambda: client)


_PASS = {
    "source_key": "air",
    "category": "row_count",
    "rule": "min_rows",
    "column": None,
    "status": "pass",
    "actual": 10,
    "threshold": 1,
    "affected_rows": None,
    "evaluated_rows": None,
    "detail": None,
}


def _result(status: str, **overrides: object) -> dict[str, object]:
    return {**_PASS, "status": status, **overrides}


class TestDatasetQualityHistoryAggregation:
    def test_pass_warn_fail_counts(self, tmp_path: Path) -> None:
        _write_fixture_run(
            tmp_path,
            "r1",
            dataset_id="d.a",
            quality_results={
                "air": [
                    _result("pass"),
                    _result("warn", rule="max_null_ratio"),
                    _result("fail", rule="max_duplicate_rate"),
                ]
            },
        )
        resp = dispatch(_service(tmp_path), "GET", "/datasets/d.a/quality/history", None)

        assert resp.status_code == 200
        (entry,) = cast(list[dict[str, object]], resp.body["runs"])
        assert entry["pass_count"] == 1
        assert entry["warn_count"] == 1
        assert entry["fail_count"] == 1
        assert entry["evaluated_checks"] == 3
        assert entry["rule_pass_rate"] == pytest.approx(1 / 3)

    def test_denominator_zero_yields_null_pass_rate(self, tmp_path: Path) -> None:
        _write_fixture_run(tmp_path, "r1", dataset_id="d.a", quality_results={})

        resp = dispatch(_service(tmp_path), "GET", "/datasets/d.a/quality/history", None)

        (entry,) = cast(list[dict[str, object]], resp.body["runs"])
        assert entry["evaluated_checks"] == 0
        assert entry["rule_pass_rate"] is None

    def test_legacy_run_without_quality_results_field(self, tmp_path: Path) -> None:
        """manifest.quality_results 필드 자체가 없는 legacy run은 0건/None으로 표현된다."""
        _write_fixture_run(tmp_path, "r1", dataset_id="d.a", include_quality_key=False)

        resp = dispatch(_service(tmp_path), "GET", "/datasets/d.a/quality/history", None)

        (entry,) = cast(list[dict[str, object]], resp.body["runs"])
        assert entry["evaluated_checks"] == 0
        assert entry["rule_pass_rate"] is None
        assert entry["pass_count"] == 0

    def test_legacy_run_never_interpreted_as_all_pass(self, tmp_path: Path) -> None:
        _write_fixture_run(tmp_path, "r1", dataset_id="d.a", include_quality_key=False)

        resp = dispatch(_service(tmp_path), "GET", "/datasets/d.a/quality/history", None)

        (entry,) = cast(list[dict[str, object]], resp.body["runs"])
        assert entry["rule_pass_rate"] != 1.0

    def test_partial_failed_run_included_with_structured_results(self, tmp_path: Path) -> None:
        _write_fixture_run(
            tmp_path,
            "r1",
            dataset_id="d.a",
            errors=["air: quality check failed"],
            quality_results={"air": [_result("fail")]},
        )

        resp = dispatch(_service(tmp_path), "GET", "/datasets/d.a/quality/history", None)

        (entry,) = cast(list[dict[str, object]], resp.body["runs"])
        assert entry["status"] == "failed"
        assert entry["fail_count"] == 1

    def test_multi_source_aggregates_across_sources(self, tmp_path: Path) -> None:
        _write_fixture_run(
            tmp_path,
            "r1",
            dataset_id="d.a",
            quality_results={
                "air": [_result("pass")],
                "fcst": [_result("pass"), _result("fail")],
            },
        )

        resp = dispatch(_service(tmp_path), "GET", "/datasets/d.a/quality/history", None)

        (entry,) = cast(list[dict[str, object]], resp.body["runs"])
        assert entry["evaluated_checks"] == 3
        assert entry["pass_count"] == 2
        assert entry["fail_count"] == 1

    def test_validated_rows_uses_row_counts_total_not_evaluated_rows_sum(
        self, tmp_path: Path
    ) -> None:
        """validated_rows는 row_counts 합계를 쓰며, evaluated_rows를 rule 수만큼
        중복 합산하지 않는다(#486 #16)."""
        _write_fixture_run(
            tmp_path,
            "r1",
            dataset_id="d.a",
            row_counts={"air": 100, "fcst": 50},
            quality_results={
                "air": [
                    _result("pass", evaluated_rows=100),
                    _result("pass", rule="max_null_ratio", evaluated_rows=100),
                ],
                "fcst": [_result("pass", evaluated_rows=50)],
            },
        )

        resp = dispatch(_service(tmp_path), "GET", "/datasets/d.a/quality/history", None)

        (entry,) = cast(list[dict[str, object]], resp.body["runs"])
        # naive sum of evaluated_rows would be 250; correct validated_rows is 150.
        assert entry["validated_rows"] == 150

    def test_validated_rows_ignores_boolean_row_count_values(self, tmp_path: Path) -> None:
        """손상된 manifest의 bool row_count 값은 1/0으로 합산되지 않는다(#486)."""
        _write_fixture_run(
            tmp_path,
            "r1",
            dataset_id="d.a",
            row_counts={"air": 100},
        )
        run_dir = tmp_path / "r1"
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
        manifest["row_counts"]["fcst"] = True
        (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        resp = dispatch(_service(tmp_path), "GET", "/datasets/d.a/quality/history", None)

        (entry,) = cast(list[dict[str, object]], resp.body["runs"])
        # bool True must not add 1 to the total.
        assert entry["validated_rows"] == 100

    def test_quality_fail_build_preserves_validated_rows(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        spec_yaml = (
            _SPEC_YAML.format(dataset_id="d.failed")
            + "quality:\n"
            + "  min_rows: 10\n"
            + "  min_rows_severity: fail\n"
        )

        build = dispatch(service, "POST", "/build", {"spec": spec_yaml, "run_id": "failed"})
        history = dispatch(service, "GET", "/datasets/d.failed/quality/history", None)

        assert build.status_code == 502
        assert history.status_code == 200
        (entry,) = cast(list[dict[str, object]], history.body["runs"])
        assert entry["status"] == "failed"
        assert entry["fail_count"] == 1
        assert entry["validated_rows"] == 1

    def test_history_newest_first(self, tmp_path: Path) -> None:
        _write_fixture_run(
            tmp_path, "r1", dataset_id="d.a", finished_at="2025-01-01T00:00:00+00:00"
        )
        _write_fixture_run(
            tmp_path, "r2", dataset_id="d.a", finished_at="2025-06-01T00:00:00+00:00"
        )

        resp = dispatch(_service(tmp_path), "GET", "/datasets/d.a/quality/history", None)

        runs = cast(list[dict[str, object]], resp.body["runs"])
        assert [r["run_id"] for r in runs] == ["r2", "r1"]


class TestDatasetQualityHistoryRequestHandling:
    def test_not_found_for_unknown_dataset(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/datasets/nope/quality/history", None)
        assert resp.status_code == 404

    def test_rejects_invalid_limit(self, tmp_path: Path) -> None:
        _write_fixture_run(tmp_path, "r1", dataset_id="d.a")
        resp = dispatch(
            _service(tmp_path), "GET", "/datasets/d.a/quality/history", None, query="limit=0"
        )
        assert resp.status_code == 400

    def test_respects_limit(self, tmp_path: Path) -> None:
        for i in range(3):
            _write_fixture_run(
                tmp_path,
                f"r{i}",
                dataset_id="d.a",
                finished_at=f"2025-01-0{i + 1}T00:00:00+00:00",
            )
        resp = dispatch(
            _service(tmp_path), "GET", "/datasets/d.a/quality/history", None, query="limit=2"
        )
        assert resp.status_code == 200
        assert len(cast(list[object], resp.body["runs"])) == 2

    def test_default_limit_is_30(self, tmp_path: Path) -> None:
        for i in range(35):
            _write_fixture_run(
                tmp_path,
                f"r{i:03d}",
                dataset_id="d.a",
                finished_at=f"2025-01-01T00:{i:02d}:00+00:00",
            )
        resp = dispatch(_service(tmp_path), "GET", "/datasets/d.a/quality/history", None)
        assert resp.status_code == 200
        assert len(cast(list[object], resp.body["runs"])) == 30


class TestDatasetQualityHistoryOwnership:
    def _build_as(
        self, service: BuilderService, dataset_id: str, run_id: str, identifier: str
    ) -> None:
        original = app_module.authenticate
        app_module.authenticate = lambda **_kwargs: Principal(kind="oidc", identifier=identifier)  # type: ignore[assignment]
        try:
            spec_yaml = _SPEC_YAML.format(dataset_id=dataset_id)
            resp = dispatch(service, "POST", "/build", {"spec": spec_yaml, "run_id": run_id})
            assert resp.status_code == 200
        finally:
            app_module.authenticate = original

    def test_other_user_run_excluded_from_history(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        self._build_as(service, "dataset.shared", "r-a", "userA")

        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="userB")
        )
        resp = dispatch(service, "GET", "/datasets/dataset.shared/quality/history", None)
        assert resp.status_code == 404

    def test_same_dataset_id_different_users_isolated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        self._build_as(service, "dataset.shared2", "r-a", "userA")
        self._build_as(service, "dataset.shared2", "r-b", "userB")

        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="userA")
        )
        resp = dispatch(service, "GET", "/datasets/dataset.shared2/quality/history", None)
        assert resp.status_code == 200
        runs = cast(list[dict[str, object]], resp.body["runs"])
        assert [r["run_id"] for r in runs] == ["r-a"]

    def test_dev_principal_sees_all_runs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        self._build_as(service, "dataset.shared3", "r-a", "userA")
        self._build_as(service, "dataset.shared3", "r-b", "userB")

        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: Principal(kind="dev"))
        resp = dispatch(service, "GET", "/datasets/dataset.shared3/quality/history", None)
        assert resp.status_code == 200
        assert len(cast(list[object], resp.body["runs"])) == 2


class TestBuildQualityDetail:
    def test_returns_quality_results_and_schema_drift_from_manifest(self, tmp_path: Path) -> None:
        _write_fixture_run(
            tmp_path,
            "r1",
            dataset_id="d.a",
            quality_results={"air": [_result("pass")]},
        )
        run_dir = tmp_path / "r1"
        manifest = cast(dict[str, object], json.loads((run_dir / "manifest.json").read_text()))
        manifest["schema_drift"] = {
            "air": [{"kind": "column_added", "column": "new_col", "detail": "new column"}]
        }
        (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        resp = dispatch(_service(tmp_path), "GET", "/builds/r1/quality", None)

        assert resp.status_code == 200
        assert resp.body["run_id"] == "r1"
        assert resp.body["availability"] == "available"
        assert resp.body["evaluated_checks"] == 1
        quality_results = cast(dict[str, object], resp.body["quality_results"])
        assert quality_results["air"][0]["status"] == "pass"  # type: ignore[index]
        schema_drift = cast(dict[str, object], resp.body["schema_drift"])
        assert schema_drift["air"][0]["kind"] == "column_added"  # type: ignore[index]

    def test_exposes_structured_threshold_and_detail(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        spec_yaml = (
            _SPEC_YAML.format(dataset_id="d.range")
            + "quality:\n"
            + "  range:\n"
            + "    - column: v\n"
            + "      min: 0\n"
            + "      max: 100\n"
        )

        build = dispatch(service, "POST", "/build", {"spec": spec_yaml, "run_id": "range"})
        resp = dispatch(service, "GET", "/builds/range/quality", None)

        assert build.status_code == 200
        assert resp.status_code == 200
        quality_results = cast(dict[str, list[dict[str, object]]], resp.body["quality_results"])
        range_result = next(r for r in quality_results["air"] if r["rule"] == "range")
        assert range_result["threshold"] == {"min": 0, "max": 100}
        assert range_result["detail"] is None

    def test_legacy_manifest_without_quality_fields_returns_empty(self, tmp_path: Path) -> None:
        _write_fixture_run(tmp_path, "r1", dataset_id="d.a", include_quality_key=False)

        resp = dispatch(_service(tmp_path), "GET", "/builds/r1/quality", None)

        assert resp.status_code == 200
        assert resp.body["availability"] == "unavailable"
        assert resp.body["evaluated_checks"] == 0
        assert resp.body["quality_results"] == {}
        assert resp.body["schema_drift"] == {}

    def test_404_for_unknown_run(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/builds/nope/quality", None)
        assert resp.status_code == 404

    def test_403_for_non_owner(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="userA")
        )
        resp = dispatch(
            service,
            "POST",
            "/build",
            {"spec": _SPEC_YAML.format(dataset_id="d.owned"), "run_id": "r-owned"},
        )
        assert resp.status_code == 200

        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="userB")
        )
        resp = dispatch(service, "GET", "/builds/r-owned/quality", None)
        assert resp.status_code == 403


class TestBuildQualityAvailability:
    """``availability``/``evaluated_checks``가 4가지 상태를 구분하는지 검증한다 (#514).

    빈 ``quality_results``만으로는 "0건 평가"와 "애초에 계산된 적 없음"을 구분할 수
    없었다 — 이 클래스는 그 구분(available/partial/unavailable, evaluated_checks
    0 vs >0)이 manifest.inputs(known source) 커버리지에 따라 올바르게 판정되는지
    검증한다.
    """

    def test_available_with_zero_evaluated_checks(self, tmp_path: Path) -> None:
        """모든 known source가 커버되지만, 평가된 check가 0건인 경우."""
        _write_fixture_run(
            tmp_path,
            "r1",
            dataset_id="d.a",
            quality_results={"air": []},
            inputs=("air",),
        )

        resp = dispatch(_service(tmp_path), "GET", "/builds/r1/quality", None)

        assert resp.status_code == 200
        assert resp.body["availability"] == "available"
        assert resp.body["evaluated_checks"] == 0

    def test_available_with_evaluated_checks(self, tmp_path: Path) -> None:
        """모든 known source가 커버되고, 평가된 check가 1건 이상인 경우."""
        _write_fixture_run(
            tmp_path,
            "r1",
            dataset_id="d.a",
            quality_results={"air": [_result("pass"), _result("warn")]},
            inputs=("air",),
        )

        resp = dispatch(_service(tmp_path), "GET", "/builds/r1/quality", None)

        assert resp.status_code == 200
        assert resp.body["availability"] == "available"
        assert resp.body["evaluated_checks"] == 2

    def test_partial_when_a_known_source_is_missing_from_quality_results(
        self, tmp_path: Path
    ) -> None:
        """multi-source run에서 한 source의 quality 결과가 아예 빠진 경우(partial)."""
        _write_fixture_run(
            tmp_path,
            "r1",
            dataset_id="d.a",
            quality_results={"air": [_result("pass")]},
            inputs=("air", "traffic"),
        )

        resp = dispatch(_service(tmp_path), "GET", "/builds/r1/quality", None)

        assert resp.status_code == 200
        assert resp.body["availability"] == "partial"
        assert resp.body["evaluated_checks"] == 1

    def test_unavailable_when_no_known_source_has_quality_results(self, tmp_path: Path) -> None:
        """새 manifest writer는 quality가 하나도 계산되지 않아도 quality_results={}를
        항상 기록한다 — 모든 source가 quality 단계 진입 전에 실패한 run은 "일부만
        커버"(partial)가 아니라 "결과가 전혀 없음"(unavailable)이어야 한다."""
        _write_fixture_run(
            tmp_path,
            "r1",
            dataset_id="d.a",
            quality_results={},
            inputs=("air", "traffic"),
        )

        resp = dispatch(_service(tmp_path), "GET", "/builds/r1/quality", None)

        assert resp.status_code == 200
        assert resp.body["availability"] == "unavailable"
        assert resp.body["evaluated_checks"] == 0

    def test_unavailable_for_legacy_manifest_without_quality_results_field(
        self, tmp_path: Path
    ) -> None:
        """quality_results 필드 자체가 없는 legacy run(#486 이전)."""
        _write_fixture_run(
            tmp_path, "r1", dataset_id="d.a", include_quality_key=False, inputs=("air",)
        )

        resp = dispatch(_service(tmp_path), "GET", "/builds/r1/quality", None)

        assert resp.status_code == 200
        assert resp.body["availability"] == "unavailable"
        assert resp.body["evaluated_checks"] == 0
