"""``GET /builds/{run_id}/events`` HTTP API 테스트 (#496).

Event 방출 자체(어느 boundary에서 어떤 event가 나오는지)는
test_pipeline_events.py가 다룬다. 이 파일은 route adapter 계층 — 존재/
ownership/bounded query(limit/tail)/secret 비노출 — 을 실제
``BuilderService.build()``/``dispatch``를 통해 검증한다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

import kpubdata_builder.service.app as app_module
from kpubdata_builder.service import BuilderService, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.ownership import _OWNERSHIP_ENV
from kpubdata_builder.spec import JsonValue

VALID_SPEC_YAML = (
    "dataset_id: dataset.events\n"
    "title: Events Fixture\n"
    "description: fixture\n"
    "sources:\n"
    "  - provider: datago\n"
    "    dataset: air_quality\n"
    "    alias: air\n"
    "    params:\n"
    "      api_key: SUPER-SECRET-API-KEY\n"
    "exports:\n"
    "  - kind: jsonl\n"
    "    output_path: out/data.jsonl\n"
    "    options:\n"
    "      kaggle_key: SUPER-SECRET-EXPORT-KEY\n"
)

# fetch 자체가 실패하는 spec (FakeClient가 모르는 dataset).
FETCH_FAILURE_SPEC_YAML = VALID_SPEC_YAML.replace("dataset: air_quality", "dataset: missing")


class _FakeResult:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    @property
    def items(self) -> list[dict[str, JsonValue]]:
        return self._items


class _FakeDataset:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    def list(self, **params: JsonValue) -> _FakeResult:
        return _FakeResult(self._items)


class _FakeClient:
    def __init__(self, data: dict[str, list[dict[str, JsonValue]]]) -> None:
        self._data = data

    def dataset(self, source_key: str) -> _FakeDataset:
        if source_key not in self._data:
            raise KeyError(f"unknown source: {source_key}")
        return _FakeDataset(self._data[source_key])


def _service(tmp_path: Path, *, rows: int = 2) -> BuilderService:
    records = [{"id": str(i), "v": i * 10} for i in range(1, rows + 1)]
    client = _FakeClient({"datago.air_quality": records})
    return BuilderService(output_root=tmp_path, client_factory=lambda: client)


def _build(service: BuilderService, run_id: str, spec_yaml: str = VALID_SPEC_YAML) -> int:
    resp = dispatch(service, "POST", "/build", {"spec": spec_yaml, "run_id": run_id})
    return resp.status_code


class TestGetBuildEventsRouting:
    def test_completed_run_returns_ordered_events(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1") == 200

        resp = dispatch(service, "GET", "/builds/r1/events", None)
        assert resp.status_code == 200
        assert resp.body["run_id"] == "r1"
        events = cast(list[dict[str, object]], resp.body["events"])
        assert events[0]["event"] == "run_started"
        assert events[-1]["event"] == "run_finished"
        seqs = [cast(int, e["seq"]) for e in events]
        assert seqs == sorted(seqs)

    def test_failed_run_still_returns_200_with_failure_events(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1", FETCH_FAILURE_SPEC_YAML) == 502

        resp = dispatch(service, "GET", "/builds/r1/events", None)
        assert resp.status_code == 200
        events = cast(list[dict[str, object]], resp.body["events"])
        assert events[-1]["event"] == "run_failed"
        assert events[-1]["status"] == "fail"

    def test_partial_run_shows_success_then_failure(self, tmp_path: Path) -> None:
        """실패했다고 이전 성공 event가 사라지지 않는다(append-only, #496)."""
        service = _service(tmp_path)
        spec_yaml = (
            "dataset_id: dataset.partial\n"
            "title: Partial Fixture\n"
            "description: fixture\n"
            "sources:\n"
            "  - provider: datago\n"
            "    dataset: air_quality\n"
            "    alias: good\n"
            "  - provider: datago\n"
            "    dataset: missing\n"
            "    alias: bad\n"
            "exports:\n"
            "  - kind: jsonl\n"
            "    output_path: out/data.jsonl\n"
        )
        assert _build(service, "r1", spec_yaml) == 502

        resp = dispatch(service, "GET", "/builds/r1/events", None)
        assert resp.status_code == 200
        events = cast(list[dict[str, object]], resp.body["events"])
        good_events = [e["event"] for e in events if e.get("source_key") == "good"]
        bad_events = [e["event"] for e in events if e.get("source_key") == "bad"]
        assert "stage_completed" in good_events
        assert "stage_failed" in bad_events

    def test_empty_timeline_for_run_with_no_events(self, tmp_path: Path) -> None:
        """BuilderService.build()를 거치지 않고 만들어진 run(존재하지만 event 없음)."""
        run_dir = tmp_path / "legacy"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text("{}", encoding="utf-8")

        resp = dispatch(_service(tmp_path), "GET", "/builds/legacy/events", None)
        assert resp.status_code == 200
        assert resp.body["events"] == []

    def test_unknown_run_returns_404(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/builds/nope/events", None)
        assert resp.status_code == 404

    def test_unsafe_run_id_returns_400(self, tmp_path: Path) -> None:
        resp = dispatch(_service(tmp_path), "GET", "/builds/../escape/events", None)
        assert resp.status_code == 400

    def test_does_not_shadow_other_builds_subroutes(self, tmp_path: Path) -> None:
        """다른 /builds/{run_id}/* route(예: manifest)가 여전히 정상 동작한다."""
        service = _service(tmp_path)
        assert _build(service, "r1") == 200
        resp = dispatch(service, "GET", "/builds/r1/manifest", None)
        assert resp.status_code == 200
        resp2 = dispatch(service, "GET", "/builds/r1/stages", None)
        assert resp2.status_code == 200


class TestLimitAndTail:
    def _build_many_sources(self, service: BuilderService, run_id: str, count: int) -> int:
        sources = "\n".join(
            f"  - provider: datago\n    dataset: air_quality\n    alias: s{i}\n"
            for i in range(count)
        )
        spec_yaml = (
            "dataset_id: dataset.many\n"
            "title: Many Fixture\n"
            "description: fixture\n"
            f"sources:\n{sources}"
            "exports:\n"
            "  - kind: jsonl\n"
            "    output_path: out/data.jsonl\n"
        )
        return _build(service, run_id, spec_yaml)

    def test_default_limit_returns_from_start(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1") == 200
        resp = dispatch(service, "GET", "/builds/r1/events", None, query="limit=1")
        assert resp.status_code == 200
        events = cast(list[dict[str, object]], resp.body["events"])
        assert len(events) == 1
        assert events[0]["event"] == "run_started"

    def test_tail_true_returns_most_recent_ascending(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1") == 200
        resp = dispatch(service, "GET", "/builds/r1/events", None, query="limit=1&tail=true")
        assert resp.status_code == 200
        events = cast(list[dict[str, object]], resp.body["events"])
        assert len(events) == 1
        assert events[0]["event"] == "run_finished"

    def test_invalid_limit_zero_returns_400(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1") == 200
        resp = dispatch(service, "GET", "/builds/r1/events", None, query="limit=0")
        assert resp.status_code == 400

    def test_invalid_limit_non_integer_returns_400(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1") == 200
        resp = dispatch(service, "GET", "/builds/r1/events", None, query="limit=abc")
        assert resp.status_code == 400

    def test_limit_above_max_returns_400(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1") == 200
        resp = dispatch(service, "GET", "/builds/r1/events", None, query="limit=1001")
        assert resp.status_code == 400

    def test_invalid_tail_value_returns_400(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1") == 200
        resp = dispatch(service, "GET", "/builds/r1/events", None, query="tail=yes")
        assert resp.status_code == 400

    def test_bool_as_int_limit_is_rejected_like_other_routes(self, tmp_path: Path) -> None:
        """query string은 항상 문자열이라 bool 우회는 애초에 불가능하지만,
        비정상 문자열이 그대로 400이 되는지 다른 route와 동일하게 확인한다."""
        service = _service(tmp_path)
        assert _build(service, "r1") == 200
        resp = dispatch(service, "GET", "/builds/r1/events", None, query="limit=true")
        assert resp.status_code == 400


class TestOwnership:
    def test_cross_owner_returns_403_before_query(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="a")
        )
        assert _build(service, "r1") == 200

        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="b")
        )
        resp = dispatch(service, "GET", "/builds/r1/events", None)
        assert resp.status_code == 403

        # ownership 거부 시 event store 조회 로직까지 도달하지 않는다.
        monkeypatch.setattr(
            service._event_store,
            "list_for_run",
            lambda *a, **kw: pytest.fail("event store leaked past 403"),
        )
        resp2 = dispatch(service, "GET", "/builds/r1/events", None)
        assert resp2.status_code == 403

    def test_owner_can_read_own_run_events(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="a")
        )
        assert _build(service, "r1") == 200

        resp = dispatch(service, "GET", "/builds/r1/events", None)
        assert resp.status_code == 200
        assert cast(list[object], resp.body["events"])

    def test_unknown_run_404_before_ownership_leak(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """존재하지 않는 run은 cross-owner와 구분 없이 404다(존재 여부 미노출)."""
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path)
        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="a")
        )
        resp = dispatch(service, "GET", "/builds/nope/events", None)
        assert resp.status_code == 404


class TestSecurityNoSecretLeak:
    def test_no_credential_or_path_in_events_response(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1") == 200

        resp = dispatch(service, "GET", "/builds/r1/events", None)
        body_text = json.dumps(resp.body)
        assert "SUPER-SECRET-API-KEY" not in body_text
        assert "SUPER-SECRET-EXPORT-KEY" not in body_text
        assert str(tmp_path) not in body_text
        assert "Authorization" not in body_text
        assert "Bearer" not in body_text

    def test_no_stack_trace_or_exception_repr_on_failure(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        assert _build(service, "r1", FETCH_FAILURE_SPEC_YAML) == 502

        resp = dispatch(service, "GET", "/builds/r1/events", None)
        body_text = json.dumps(resp.body)
        assert "Traceback" not in body_text
        assert "KeyError" not in body_text
        assert str(tmp_path) not in body_text

    def test_no_credential_leak_even_on_failed_source(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        # silver 검증 실패를 유발해 stage_failed event에 실린 message도 확인한다.
        silver_failure_spec = VALID_SPEC_YAML.replace(
            "    alias: air\n",
            "    alias: air\n    schema:\n      required: [does_not_exist]\n",
        )
        assert _build(service, "r1", silver_failure_spec) == 502

        resp = dispatch(service, "GET", "/builds/r1/events", None)
        body_text = json.dumps(resp.body)
        assert "SUPER-SECRET-API-KEY" not in body_text
        assert "SUPER-SECRET-EXPORT-KEY" not in body_text
