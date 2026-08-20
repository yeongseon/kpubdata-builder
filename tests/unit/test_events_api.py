"""``GET /builds/{run_id}/events`` HTTP API 테스트 (#496).

Event 방출 자체(어느 boundary에서 어떤 event가 나오는지)는
test_pipeline_events.py가 다룬다. 이 파일은 route adapter 계층 — 존재/
ownership/bounded query(limit/tail)/secret 비노출 — 을 실제
``BuilderService.build()``/``dispatch``를 통해 검증한다.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import cast

import pytest

import kpubdata_builder.service.app as app_module
from kpubdata_builder.pipeline import CancellationProbe
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
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


def _file_source_spec_yaml(upload_id: str) -> str:
    return (
        f"""
dataset_id: dataset.async-uploaded
title: Async Uploaded Fixture
description: file source build (#498 async owner propagation regression)
sources:
  - kind: file
    upload_id: {upload_id}
    format: csv
    encoding: utf-8
exports:
  - kind: jsonl
    output_path: out/data.jsonl
""".strip()
        + "\n"
    )


class _ObservedAsyncService(BuilderService):
    """``_run_build_job``(``build()`` 호출 + owner_id manifest 보정까지 전부)이
    끝난 뒤 ``completed``를 set한다 — async build가 실제로 완료된(manifest 보정
    까지 반영된) 시점을 폴링/sleep 없이 결정론적으로 기다리기 위한 헬퍼다."""

    def __init__(
        self,
        *,
        output_root: Path,
        client_factory: object,
        completed: threading.Event,
        async_max_workers: int = 1,
    ) -> None:
        super().__init__(
            output_root=output_root,
            client_factory=client_factory,  # type: ignore[arg-type]
            async_max_workers=async_max_workers,
        )
        self._completed = completed

    def _run_build_job(
        self,
        spec_yaml: str,
        run_id: str,
        created_by: str | None,
        cancellation: CancellationProbe,
    ) -> ServiceResponse:
        try:
            return super()._run_build_job(spec_yaml, run_id, created_by, cancellation)
        finally:
            self._completed.set()


class _BlockingAsyncService(BuilderService):
    """async worker를 ``release``까지 붙잡아둔다.

    ``_run_build_job``(worker pool의 실제 실행 진입점)이 ``entered``를 set한
    뒤 ``release``를 기다린다 — 그 사이 registry 상태는 이미 "running"이지만
    (``AsyncBuildExecutor._run``이 runner 호출 *전에* ``begin_run``으로 전이시킨다)
    run directory/manifest는 아직 만들어지지 않는다(``BuilderService.build()``
    가 아직 호출되지 않았으므로). ``release`` 이후에는 실제 ``build()``를
    그대로 호출해 정상적으로 완료시키고 ``completed``를 set한다 — active
    상태와 완료 상태 둘 다 이 클래스 하나로 결정론적으로 재현한다(#496
    follow-up).
    """

    def __init__(
        self,
        *,
        output_root: Path,
        client_factory: object,
        entered: threading.Event,
        release: threading.Event,
        completed: threading.Event,
        async_max_workers: int = 1,
        async_max_queue_size: int = 10,
    ) -> None:
        super().__init__(
            output_root=output_root,
            client_factory=client_factory,  # type: ignore[arg-type]
            async_max_workers=async_max_workers,
            async_max_queue_size=async_max_queue_size,
        )
        self._entered = entered
        self._release = release
        self._completed = completed

    def _run_build_job(
        self,
        spec_yaml: str,
        run_id: str,
        created_by: str | None,
        cancellation: CancellationProbe,
    ) -> ServiceResponse:
        self._entered.set()
        self._release.wait(timeout=5)
        try:
            return super()._run_build_job(spec_yaml, run_id, created_by, cancellation)
        finally:
            self._completed.set()


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


class TestActiveAsyncRunEvents:
    """``POST /builds``(비동기)로 제출된 run의 events를 실행 중에도 조회할 수
    있는지 검증한다 (#496 follow-up: BLOCKER).

    ``check_run_exists``/``check_ownership``은 run directory·manifest.json이
    이미 있다고 가정하지만, async run은 worker가 시작하기 전까지 run
    directory조차 없고 manifest는 run이 끝나야 생긴다. 그 구간(queued/running)
    에도 event store에는 이미 ``run_submitted``(및 이후 event)가 쌓여있으므로
    이 endpoint가 404/403을 내면 안 된다.
    """

    def _submit(
        self, service: BuilderService, run_id: str, spec_yaml: str = VALID_SPEC_YAML
    ) -> ServiceResponse:
        resp = dispatch(service, "POST", "/builds", {"spec": spec_yaml, "run_id": run_id})
        assert isinstance(resp, ServiceResponse)
        return resp

    def test_queued_run_returns_200_with_run_submitted(self, tmp_path: Path) -> None:
        """단일 worker가 다른 run으로 이미 바쁠 때 큐잉된 run도 조회 가능해야 한다."""
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingAsyncService(
            output_root=tmp_path,
            client_factory=lambda: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            entered=entered,
            release=release,
            completed=completed,
            async_max_workers=1,
        )
        first = self._submit(service, "run1")
        assert first.status_code == 202
        assert entered.wait(timeout=5)  # worker가 run1을 붙잡고 있다.

        second = self._submit(service, "run2")
        assert second.status_code == 202
        assert second.body["status"] == "queued"
        # run2는 아직 run directory조차 없다 — 기존 check_run_exists라면 404.
        assert not (tmp_path / "run2").exists()

        resp = dispatch(service, "GET", "/builds/run2/events", None)
        release.set()
        assert resp.status_code == 200
        events = cast(list[dict[str, object]], resp.body["events"])
        assert [e["event"] for e in events] == ["run_submitted"]
        assert completed.wait(timeout=5)

    def test_running_run_returns_200_before_manifest_exists(self, tmp_path: Path) -> None:
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingAsyncService(
            output_root=tmp_path,
            client_factory=lambda: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            entered=entered,
            release=release,
            completed=completed,
        )
        submitted = self._submit(service, "run1")
        assert submitted.status_code == 202
        assert entered.wait(timeout=5)
        assert not (tmp_path / "run1" / "manifest.json").exists()

        resp = dispatch(service, "GET", "/builds/run1/events", None)
        release.set()
        assert resp.status_code == 200
        events = cast(list[dict[str, object]], resp.body["events"])
        assert [e["event"] for e in events] == ["run_submitted"]
        assert completed.wait(timeout=5)

    def test_ownership_enforced_active_owner_can_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingAsyncService(
            output_root=tmp_path,
            client_factory=lambda: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            entered=entered,
            release=release,
            completed=completed,
        )
        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="a")
        )
        submitted = self._submit(service, "run1")
        assert submitted.status_code == 202
        assert entered.wait(timeout=5)

        resp = dispatch(service, "GET", "/builds/run1/events", None)
        release.set()
        assert resp.status_code == 200
        assert completed.wait(timeout=5)

    def test_ownership_enforced_other_principal_gets_403(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingAsyncService(
            output_root=tmp_path,
            client_factory=lambda: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            entered=entered,
            release=release,
            completed=completed,
        )
        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="a")
        )
        submitted = self._submit(service, "run1")
        assert submitted.status_code == 202
        assert entered.wait(timeout=5)

        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="b")
        )
        resp = dispatch(service, "GET", "/builds/run1/events", None)
        release.set()
        assert resp.status_code == 403
        assert completed.wait(timeout=5)

    def test_unknown_run_still_404_when_not_in_async_registry(self, tmp_path: Path) -> None:
        """async registry에도 persisted run에도 없으면 여전히 404다."""
        service = _service(tmp_path)
        resp = dispatch(service, "GET", "/builds/nope/events", None)
        assert resp.status_code == 404

    def test_completed_async_run_still_uses_manifest_ownership_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """완료된 run은 async registry에 terminal entry로 남아있어도, 기존
        completed persisted run 조회(#496 원래 API)가 계속 정상 동작해야 한다."""
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingAsyncService(
            output_root=tmp_path,
            client_factory=lambda: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            entered=entered,
            release=release,
            completed=completed,
        )
        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="a")
        )
        submitted = self._submit(service, "run1")
        assert submitted.status_code == 202
        assert entered.wait(timeout=5)
        release.set()
        assert completed.wait(timeout=5)

        assert (tmp_path / "run1" / "manifest.json").exists()

        resp = dispatch(service, "GET", "/builds/run1/events", None)
        assert resp.status_code == 200
        events = cast(list[dict[str, object]], resp.body["events"])
        event_names = [e["event"] for e in events]
        assert "run_submitted" in event_names
        assert "run_finished" in event_names

    def test_same_label_different_owner_id_active_run_returns_403(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """created_by/label(legacy)이 같아도 stable owner_id(#505)가 다르면
        거부해야 한다 — active run access가 owner_id를 우선 비교해야 하는
        핵심 사례."""
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingAsyncService(
            output_root=tmp_path,
            client_factory=lambda: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            entered=entered,
            release=release,
            completed=completed,
        )
        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="oidc", identifier="same", owner_id="oidc:owner-a"),
        )
        submitted = self._submit(service, "run1")
        assert submitted.status_code == 202
        assert entered.wait(timeout=5)

        # label("oidc:same")은 앞의 principal과 동일하지만 owner_id는 다르다.
        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="oidc", identifier="same", owner_id="oidc:owner-b"),
        )
        resp = dispatch(service, "GET", "/builds/run1/events", None)
        release.set()
        assert resp.status_code == 403
        assert completed.wait(timeout=5)

    def test_matching_owner_id_active_run_returns_200(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingAsyncService(
            output_root=tmp_path,
            client_factory=lambda: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            entered=entered,
            release=release,
            completed=completed,
        )
        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="oidc", identifier="a", owner_id="oidc:owner-a"),
        )
        submitted = self._submit(service, "run1")
        assert submitted.status_code == 202
        assert entered.wait(timeout=5)

        resp = dispatch(service, "GET", "/builds/run1/events", None)
        release.set()
        assert resp.status_code == 200
        assert completed.wait(timeout=5)

    def test_owner_id_never_leaks_into_build_status_or_events_wire(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingAsyncService(
            output_root=tmp_path,
            client_factory=lambda: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            entered=entered,
            release=release,
            completed=completed,
        )
        secret_owner_id = "oidc:super-secret-owner-hash"
        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="oidc", identifier="a", owner_id=secret_owner_id),
        )
        submitted = self._submit(service, "run1")
        assert submitted.status_code == 202
        assert "owner_id" not in submitted.body
        assert secret_owner_id not in json.dumps(submitted.body)
        assert entered.wait(timeout=5)

        status = dispatch(service, "GET", "/builds/run1", None)
        assert "owner_id" not in status.body
        assert secret_owner_id not in json.dumps(status.body)

        resp = dispatch(service, "GET", "/builds/run1/events", None)
        release.set()
        assert resp.status_code == 200
        assert "owner_id" not in json.dumps(resp.body)
        assert secret_owner_id not in json.dumps(resp.body)
        assert completed.wait(timeout=5)

    def test_enqueue_failed_terminal_without_manifest_only_owner_can_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """worker pool enqueue 자체가 실패해 manifest 없이 종결된 terminal job도
        (build()가 전혀 호출되지 않아 run directory조차 없다) 소유자만
        조회할 수 있어야 한다 — registry snapshot의 owner_id가 유일한
        판정 근거다."""
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = BuilderService(
            output_root=tmp_path,
            client_factory=lambda: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
        )

        def _broken_executor_submit(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("simulated worker pool rejection")

        monkeypatch.setattr(service._async_builds._executor, "submit", _broken_executor_submit)

        owner = Principal(kind="oidc", identifier="a", owner_id="oidc:owner-a")
        response = service.submit_build(
            VALID_SPEC_YAML, run_id="run1", created_by=owner.label, owner_id=owner.owner_id
        )
        assert response.status_code >= 500
        assert not (tmp_path / "run1").exists()

        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: owner)
        resp_owner = dispatch(service, "GET", "/builds/run1/events", None)
        assert resp_owner.status_code == 200
        events = cast(list[dict[str, object]], resp_owner.body["events"])
        assert [e["event"] for e in events] == ["run_submitted", "run_failed"]

        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="oidc", identifier="b", owner_id="oidc:owner-b"),
        )
        resp_other = dispatch(service, "GET", "/builds/run1/events", None)
        assert resp_other.status_code == 403

        monkeypatch.setattr(
            app_module, "authenticate", lambda **_kwargs: Principal(kind="oidc", identifier="b")
        )
        resp2 = dispatch(service, "GET", "/builds/run1/events", None)
        assert resp2.status_code == 403


class TestAsyncManifestOwnerIdPropagation:
    """async run 완료 후 persisted manifest.owner_id가 제출 principal의 stable
    owner_id를 담아야 한다 (#496 follow-up: security BLOCKER).

    수정 전에는 ``_run_build_job``이 ``build()``에 owner_id를 전혀 넘기지
    않아 async run의 manifest.owner_id가 항상 None이었다 — manifest가 써지는
    순간 ``check_active_run_access``가 manifest 경로로 전환되면서 stable
    owner_id 비교(#505) 대신 legacy created_by/label 비교로 되돌아갔다. 같은
    label(OIDC sub 앞 8자 truncation 충돌 등)을 쓰는 서로 다른 owner_id의
    principal 두 명이 있으면, "완료된" run에서만 그 fallback이 cross-owner
    접근을 허용해버릴 수 있었다 — 아래 A/B 시나리오가 그 재현이다.
    """

    def _submit(
        self, service: BuilderService, run_id: str, spec_yaml: str = VALID_SPEC_YAML
    ) -> ServiceResponse:
        resp = dispatch(service, "POST", "/builds", {"spec": spec_yaml, "run_id": run_id})
        assert isinstance(resp, ServiceResponse)
        return resp

    def test_completed_manifest_records_submitting_principals_stable_owner_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A(``oidc:same``/``oidc:owner-A``)가 async build → 완료 → manifest 생성.

        요구사항 시나리오 1: persisted manifest 내부 owner_id == oidc:owner-A.
        """
        completed = threading.Event()
        service = _ObservedAsyncService(
            output_root=tmp_path,
            client_factory=lambda: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            completed=completed,
        )
        principal_a = Principal(kind="oidc", identifier="same", owner_id="oidc:owner-A")
        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: principal_a)

        submitted = self._submit(service, "run1")
        assert submitted.status_code == 202
        assert completed.wait(timeout=5)

        manifest_data = json.loads(
            (tmp_path / "run1" / "manifest.json").read_text(encoding="utf-8")
        )
        assert manifest_data["owner_id"] == "oidc:owner-A"

    def test_completed_run_owner_gets_200_other_same_label_principal_gets_403(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """요구사항 시나리오 2/3: A는 200, 같은 label을 쓰는 B(다른 owner_id)는 403."""
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        completed = threading.Event()
        service = _ObservedAsyncService(
            output_root=tmp_path,
            client_factory=lambda: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            completed=completed,
        )
        principal_a = Principal(kind="oidc", identifier="same", owner_id="oidc:owner-A")
        principal_b = Principal(kind="oidc", identifier="same", owner_id="oidc:owner-B")
        assert principal_a.label == principal_b.label  # 회귀의 전제조건: legacy label이 같다.

        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: principal_a)
        submitted = self._submit(service, "run1")
        assert submitted.status_code == 202
        assert completed.wait(timeout=5)
        assert (tmp_path / "run1" / "manifest.json").exists()

        resp_owner = dispatch(service, "GET", "/builds/run1/events", None)
        assert resp_owner.status_code == 200

        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: principal_b)
        resp_other = dispatch(service, "GET", "/builds/run1/events", None)
        assert resp_other.status_code == 403

    def test_active_same_label_different_owner_still_returns_403(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """회귀 방지: manifest가 아직 없는 active 구간(#496 이전 라운드 fix)도
        여전히 동작해야 한다 — 이번 수정이 그 경로를 깨지 않았는지 확인."""
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingAsyncService(
            output_root=tmp_path,
            client_factory=lambda: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            entered=entered,
            release=release,
            completed=completed,
        )
        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="oidc", identifier="same", owner_id="oidc:owner-A"),
        )
        submitted = self._submit(service, "run1")
        assert submitted.status_code == 202
        assert entered.wait(timeout=5)
        assert not (tmp_path / "run1" / "manifest.json").exists()

        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="oidc", identifier="same", owner_id="oidc:owner-B"),
        )
        resp = dispatch(service, "GET", "/builds/run1/events", None)
        release.set()
        assert resp.status_code == 403
        assert completed.wait(timeout=5)

    def test_owner_id_not_exposed_via_wire_after_completion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """manifest 보정 이후에도 owner_id는 build_status/events/manifest 응답
        어디에도 노출되지 않는다 — public API/OpenAPI 계약은 바뀌지 않는다."""
        completed = threading.Event()
        service = _ObservedAsyncService(
            output_root=tmp_path,
            client_factory=lambda: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            completed=completed,
        )
        secret_owner_id = "oidc:super-secret-owner-hash"
        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="oidc", identifier="a", owner_id=secret_owner_id),
        )
        submitted = self._submit(service, "run1")
        assert submitted.status_code == 202
        assert completed.wait(timeout=5)

        status = dispatch(service, "GET", "/builds/run1", None)
        assert "owner_id" not in status.body
        assert secret_owner_id not in json.dumps(status.body)

        events_resp = dispatch(service, "GET", "/builds/run1/events", None)
        assert events_resp.status_code == 200
        assert "owner_id" not in json.dumps(events_resp.body)
        assert secret_owner_id not in json.dumps(events_resp.body)

        manifest_resp = dispatch(service, "GET", "/builds/run1/manifest", None)
        assert manifest_resp.status_code == 200
        assert "owner_id" not in manifest_resp.body
        assert secret_owner_id not in json.dumps(manifest_resp.body)

    def test_async_file_source_resolver_still_does_not_receive_owner_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#498 known limitation 유지 확인: async 경로로 제출된 ``kind=file``
        build는, manifest owner_id가 이제 올바르게 채워지더라도, 여전히 file
        source resolver에는 owner_id를 넘기지 않는다 — 업로드 소유자 본인이
        제출해도 async 경로에서는 그 업로드를 찾지 못해 build가 실패해야
        한다(동기 ``/build``와 달리)."""
        completed = threading.Event()
        service = _ObservedAsyncService(
            output_root=tmp_path,
            client_factory=lambda: _FakeClient({}),
            completed=completed,
        )
        owner = Principal(kind="oidc", identifier="a", owner_id="oidc:owner-a")

        created = service.create_upload(
            b"id,amount\n1,1000\n",
            format="csv",
            encoding="utf-8",
            original_filename="trades.csv",
            principal=owner,
        )
        assert created.status_code == 200
        upload_id = created.body["upload_id"]
        assert isinstance(upload_id, str)

        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: owner)
        submitted = self._submit(service, "run1", _file_source_spec_yaml(upload_id))
        assert submitted.status_code == 202
        assert completed.wait(timeout=5)

        # manifest ownership은 이번 수정으로 정확하다 — 그러나 file resolver는
        # 여전히 owner_id를 받지 않으므로 업로드를 찾지 못해 build 자체는 실패한다
        # (#498 async limitation, 그대로 유지).
        manifest_data = json.loads(
            (tmp_path / "run1" / "manifest.json").read_text(encoding="utf-8")
        )
        assert manifest_data["owner_id"] == "oidc:owner-a"
        assert manifest_data["errors"], "file resolver가 owner_id 없이 업로드를 찾지 못해야 한다"

        status = dispatch(service, "GET", "/builds/run1", None)
        assert status.body["status"] == "failed"

    def test_completed_run_build_index_records_stable_owner_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """manifest뿐 아니라 BuildIndex(#505 SSOT, ``GET /builds`` 목록이 우선
        조회하는 경로)도 같은 stable owner_id를 가져야 한다 — 두 저장소가
        서로 다른 값을 갖는 SSOT 불일치를 만들지 않는다."""
        completed = threading.Event()
        service = _ObservedAsyncService(
            output_root=tmp_path,
            client_factory=lambda: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            completed=completed,
        )
        principal_a = Principal(kind="oidc", identifier="same", owner_id="oidc:owner-A")
        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: principal_a)

        submitted = self._submit(service, "run1")
        assert submitted.status_code == 202
        assert completed.wait(timeout=5)

        manifest_data = json.loads(
            (tmp_path / "run1" / "manifest.json").read_text(encoding="utf-8")
        )
        index_entry = service._build_index.get("run1")
        assert index_entry is not None
        assert index_entry.owner_id == "oidc:owner-A"
        assert index_entry.owner_id == manifest_data["owner_id"]

    def test_build_list_hides_completed_run_from_different_owner_with_same_label(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BuildIndex를 사용하는 persisted ownership 경로(``GET /builds`` 목록)도
        같은 label의 다른 owner_id principal을 통과시키지 않는다."""
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        completed = threading.Event()
        service = _ObservedAsyncService(
            output_root=tmp_path,
            client_factory=lambda: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            completed=completed,
        )
        principal_a = Principal(kind="oidc", identifier="same", owner_id="oidc:owner-A")
        principal_b = Principal(kind="oidc", identifier="same", owner_id="oidc:owner-B")
        assert principal_a.label == principal_b.label

        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: principal_a)
        submitted = self._submit(service, "run1")
        assert submitted.status_code == 202
        assert completed.wait(timeout=5)

        list_as_owner = dispatch(service, "GET", "/builds", None)
        assert list_as_owner.status_code == 200
        owner_run_ids = [
            b["run_id"] for b in cast(list[dict[str, object]], list_as_owner.body["builds"])
        ]
        assert "run1" in owner_run_ids

        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: principal_b)
        list_as_other = dispatch(service, "GET", "/builds", None)
        assert list_as_other.status_code == 200
        other_run_ids = [
            b["run_id"] for b in cast(list[dict[str, object]], list_as_other.body["builds"])
        ]
        assert "run1" not in other_run_ids
        assert "owner_id" not in json.dumps(list_as_other.body)
