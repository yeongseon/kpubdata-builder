"""비동기 build job 서비스 동작 검증 (#482)."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

import kpubdata_builder.service.app as app_module
from kpubdata_builder.events import BuildEvent
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.ownership import _OWNERSHIP_ENV
from kpubdata_builder.spec import JsonValue

VALID_SPEC_YAML = (
    """
dataset_id: dataset.sample
title: Sample Dataset
description: Sample description
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: out/data.jsonl
""".strip()
    + "\n"
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

    def list(self, **params: JsonValue) -> _FakeResult:
        return _FakeResult(self._items)


class _FakeClient:
    def __init__(self, data: dict[str, list[dict[str, JsonValue]]]) -> None:
        self._data = data

    def dataset(self, source_key: str) -> _FakeDataset:
        if source_key not in self._data:
            raise KeyError(f"unknown source: {source_key}")
        return _FakeDataset(self._data[source_key])


ClientFactory = Callable[[], _FakeClient]


class _ObservedBuildService(BuilderService):
    def __init__(
        self,
        *,
        output_root: Path,
        client_factory: ClientFactory,
        completed: threading.Event,
        async_max_workers: int = 1,
    ) -> None:
        super().__init__(
            output_root=output_root,
            client_factory=client_factory,
            async_max_workers=async_max_workers,
        )
        self._completed = completed

    def build(
        self,
        spec_yaml: str,
        *,
        run_id: str | None = None,
        created_by: str | None = None,
        owner_id: str | None = None,
        manifest_owner_id: str | None = None,
        principal: Principal | None = None,
    ) -> ServiceResponse:
        try:
            return super().build(
                spec_yaml,
                run_id=run_id,
                created_by=created_by,
                owner_id=owner_id,
                manifest_owner_id=manifest_owner_id,
                principal=principal,
            )
        finally:
            self._completed.set()


class _BlockingBuildService(BuilderService):
    def __init__(
        self,
        *,
        output_root: Path,
        entered: threading.Event,
        release: threading.Event,
        async_max_queue_size: int = 10,
    ) -> None:
        super().__init__(
            output_root=output_root,
            client_factory=lambda: _FakeClient({}),
            async_max_workers=1,
            async_max_queue_size=async_max_queue_size,
        )
        self._entered = entered
        self._release = release

    def _run_build_job(
        self, spec_yaml: str, run_id: str, created_by: str | None
    ) -> ServiceResponse:
        self._entered.set()
        self._release.wait(timeout=5)
        return ServiceResponse(200, {"status": "ok", "run_id": run_id})


def _service(tmp_path: Path, completed: threading.Event) -> _ObservedBuildService:
    client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]})
    return _ObservedBuildService(
        output_root=tmp_path,
        client_factory=lambda: client,
        completed=completed,
        async_max_workers=1,
    )


class TestAsyncBuildJobs:
    def test_second_job_stays_queued_when_single_worker_is_busy(self, tmp_path: Path) -> None:
        entered = threading.Event()
        release = threading.Event()
        service = _BlockingBuildService(output_root=tmp_path, entered=entered, release=release)

        first = service.submit_build(VALID_SPEC_YAML, run_id="run1", created_by="tester")
        assert first.status_code == 202
        assert entered.wait(timeout=5)
        second = service.submit_build(VALID_SPEC_YAML, run_id="run2", created_by="tester")

        first_status = service.build_status("run1")
        second_status = service.build_status("run2")
        release.set()
        assert first_status.body["status"] == "running"
        assert second.status_code == 202
        assert second_status.body["status"] == "queued"

    def test_successful_async_build_writes_manifest_and_index(self, tmp_path: Path) -> None:
        completed = threading.Event()
        service = _service(tmp_path, completed)

        response = service.submit_build(VALID_SPEC_YAML, run_id="run1", created_by="tester")
        assert response.status_code == 202
        assert completed.wait(timeout=5)

        status = service.build_status("run1")
        entry = service._build_index.get("run1")
        assert status.body["status"] == "succeeded"
        assert (tmp_path / "run1" / "manifest.json").exists()
        assert entry is not None
        assert entry.status == "ok"

    def test_failed_async_build_records_failed_terminal_status(self, tmp_path: Path) -> None:
        completed = threading.Event()
        service = _ObservedBuildService(
            output_root=tmp_path,
            client_factory=lambda: _FakeClient({}),
            completed=completed,
            async_max_workers=1,
        )

        response = service.submit_build(VALID_SPEC_YAML, run_id="run1", created_by="tester")
        assert response.status_code == 202
        assert completed.wait(timeout=5)

        status = service.build_status("run1")
        entry = service._build_index.get("run1")
        assert status.body["status"] == "failed"
        assert entry is not None
        assert entry.status == "failed"

    def test_active_registry_is_empty_after_service_restart(self, tmp_path: Path) -> None:
        entered = threading.Event()
        release = threading.Event()
        service = _BlockingBuildService(output_root=tmp_path, entered=entered, release=release)
        service.submit_build(VALID_SPEC_YAML, run_id="run1", created_by="tester")
        assert entered.wait(timeout=5)

        restarted = BuilderService(
            output_root=tmp_path,
            client_factory=lambda: _FakeClient({"datago.air_quality": []}),
            async_max_workers=1,
        )
        release.set()

        status = restarted.build_status("run1")
        assert status.status_code == 404

    def test_duplicate_active_run_id_returns_existing_job(self, tmp_path: Path) -> None:
        entered = threading.Event()
        release = threading.Event()
        service = _BlockingBuildService(output_root=tmp_path, entered=entered, release=release)
        first = service.submit_build(VALID_SPEC_YAML, run_id="run1", created_by="tester")
        assert entered.wait(timeout=5)

        second = service.submit_build(VALID_SPEC_YAML, run_id="run1", created_by="tester")
        release.set()

        assert first.status_code == 202
        assert second.status_code == 200
        assert second.body["run_id"] == "run1"
        assert second.body["status"] == "running"

    def test_duplicate_terminal_run_id_returns_conflict(self, tmp_path: Path) -> None:
        completed = threading.Event()
        service = _service(tmp_path, completed)
        response = service.submit_build(VALID_SPEC_YAML, run_id="run1", created_by="tester")
        assert response.status_code == 202
        assert completed.wait(timeout=5)

        duplicate = service.submit_build(VALID_SPEC_YAML, run_id="run1", created_by="tester")

        assert duplicate.status_code == 409
        assert duplicate.body["run_id"] == "run1"

    def test_post_builds_generates_run_id_when_omitted(self, tmp_path: Path) -> None:
        completed = threading.Event()
        service = _service(tmp_path, completed)

        response = dispatch(service, "POST", "/builds", {"spec": VALID_SPEC_YAML})
        assert isinstance(response, ServiceResponse)
        assert response.status_code == 202
        run_id = response.body["run_id"]
        assert isinstance(run_id, str)
        assert run_id
        assert completed.wait(timeout=5)

    def test_queue_full_returns_429(self, tmp_path: Path) -> None:
        entered = threading.Event()
        release = threading.Event()
        service = _BlockingBuildService(
            output_root=tmp_path,
            entered=entered,
            release=release,
            async_max_queue_size=1,
        )
        service.submit_build(VALID_SPEC_YAML, run_id="run1", created_by="tester")
        assert entered.wait(timeout=5)
        queued = service.submit_build(VALID_SPEC_YAML, run_id="run2", created_by="tester")

        saturated = service.submit_build(VALID_SPEC_YAML, run_id="run3", created_by="tester")
        release.set()

        assert queued.status_code == 202
        assert saturated.status_code == 429

    def test_unsafe_run_id_is_rejected_before_job_creation(self, tmp_path: Path) -> None:
        completed = threading.Event()
        service = _service(tmp_path, completed)

        response = dispatch(
            service,
            "POST",
            "/builds",
            {"spec": VALID_SPEC_YAML, "run_id": "../bad"},
        )

        assert isinstance(response, ServiceResponse)
        assert response.status_code == 400
        assert service.build_status("bad").status_code == 404


class TestRunSubmittedEventFailure:
    """``run_submitted`` event append 실패는 job을 아예 큐잉하지 않는다 (#496).

    job이 executor에 이미 큐잉된 *뒤에* event를 append하면, "event는 유실됐는데
    job은 이미 실행 중"이라는 모순이 생긴다(사용자 요구사항 C). 이를 피하기
    위해 ``AsyncBuildExecutor.submit()``의 ``on_accept`` hook이 event append를
    job 큐잉보다 먼저 실행한다 — 여기서 실패하면 job은 registry에도 worker
    pool에도 전혀 등록되지 않는다.
    """

    def test_append_failure_prevents_job_from_being_queued(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]})
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)
        # lazy event store를 강제로 초기화한 뒤 append만 고장낸다.
        event_store = service._event_store

        def _broken_append(event: BuildEvent) -> BuildEvent:
            raise RuntimeError("simulated event store outage")

        monkeypatch.setattr(event_store, "append", _broken_append)

        response = service.submit_build(VALID_SPEC_YAML, run_id="run1", created_by="tester")

        # HTTP는 실패를 정직하게 보고한다 — 202(accepted)가 아니다.
        assert response.status_code != 202
        assert response.status_code >= 500
        # job이 registry/worker pool 어디에도 등록되지 않았다 — "이미 실행
        # 중"이라는 모순이 없다.
        assert service.build_status("run1").status_code == 404
        # 실제 파이프라인이 전혀 실행되지 않았다 — run 디렉터리조차 생기지 않는다.
        assert not (tmp_path / "run1").exists()

    def test_unrelated_run_id_is_unaffected_by_a_prior_failure(self, tmp_path: Path) -> None:
        """이 run_id 하나만 겪은 실패가 다른 run_id의 정상 submission을 막지 않는다."""
        completed = threading.Event()
        service = _ObservedBuildService(
            output_root=tmp_path,
            client_factory=lambda: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            completed=completed,
            async_max_workers=1,
        )

        response = service.submit_build(VALID_SPEC_YAML, run_id="run-ok", created_by="tester")

        assert response.status_code == 202
        assert completed.wait(timeout=5)
        assert service.build_status("run-ok").body["status"] == "succeeded"


class TestExecutorEnqueueFailure:
    """``on_accept``(event append)는 성공했는데 실제 worker pool 큐잉 자체가
    실패하는 반대 방향 실패를 다룬다 (#496 self-review).

    ``AsyncBuildExecutor.submit()``은 ``registry.create()``로 job을 "queued"로
    등록한 *뒤에* ``self._executor.submit()``으로 실제 worker pool에 큐잉한다.
    후자가 실패하면 event(``run_submitted``)는 이미 기록됐고 registry 항목도
    이미 만들어진 상태라, 아무 조치가 없으면 "queued"가 영원히 남는 phantom
    job이 된다 — 아무도 실행하지 않는데 정상 진행 중처럼 보인다. event는
    append-only라 지우지 않고(#496 원칙), 대신 job 실행 실패에 이미 쓰이는
    ``registry.mark_failed()``로 정리한다. #496 lifecycle 계약상 timeline 자체도
    이 실패를 표현해야 하므로, 기존 ``run_failed`` vocabulary로 같은 run_id에
    종결 event를 하나 더 남긴다(새 event type/state/API field 없음).
    """

    def test_enqueue_failure_leaves_registry_failed_not_phantom_queued(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]})
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)

        def _broken_executor_submit(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("simulated worker pool rejection")

        monkeypatch.setattr(service._async_builds._executor, "submit", _broken_executor_submit)

        response = service.submit_build(VALID_SPEC_YAML, run_id="run1", created_by="tester")

        # HTTP는 실패를 정직하게 보고한다 — 202(accepted)가 아니다.
        assert response.status_code != 202
        assert response.status_code >= 500

        # registry는 "queued" phantom으로 남지 않는다 — 실제 job 실행 실패에
        # 쓰이는 것과 동일한 terminal("failed") 상태로 정리된다. HTTP(500)와
        # 상태 조회(failed) 둘 다 "성공하지 않았다"로 일치한다 — 모순이 없다.
        status = service.build_status("run1")
        assert status.status_code == 200
        assert status.body["status"] == "failed"

        # timeline 자체도 실패를 표현한다: run_submitted(append-only라 지워지지
        # 않는다) 뒤에 기존 run_failed vocabulary로 종결 event가 남아, event만
        # 보고도 "정상 진행 중"으로 오해할 수 없다. chronological order도
        # run_submitted -> run_failed 그대로다.
        events = service._event_store.list_for_run("run1", limit=100, tail=False)
        assert [e.event for e in events] == ["run_submitted", "run_failed"]
        assert events[-1].status == "fail"
        # raw exception/stack trace가 event message에 섞이지 않는다 — bounded,
        # 고정된 안전한 message만 쓴다.
        assert events[-1].message == "build could not be queued for execution"
        assert "RuntimeError" not in (events[-1].message or "")
        assert "simulated worker pool rejection" not in (events[-1].message or "")

        # 실제 파이프라인은 전혀 실행되지 않았다 — run 디렉터리조차 생기지 않는다.
        assert not (tmp_path / "run1").exists()

    def test_resubmission_after_enqueue_failure_reports_existing_failed_job(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """실패로 정리된 job에 재제출해도 새로 accepted(202)되는 phantom이 아니라
        기존 failed 상태를 그대로 반환한다(기존 "existing" 재제출 semantics 재사용).
        """
        client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]})
        service = BuilderService(output_root=tmp_path, client_factory=lambda: client)

        def _broken_executor_submit(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("simulated worker pool rejection")

        monkeypatch.setattr(service._async_builds._executor, "submit", _broken_executor_submit)
        first = service.submit_build(VALID_SPEC_YAML, run_id="run1", created_by="tester")
        assert first.status_code >= 500

        second = service.submit_build(VALID_SPEC_YAML, run_id="run1", created_by="tester")

        assert second.status_code == 200
        assert second.body["run_id"] == "run1"
        assert second.body["status"] == "failed"


class TestBuildJobStatusOwnership:
    """GET /builds/{run_id} 잡 상태 polling의 ownership 게이트 (#480).

    잡 상태 응답은 성공 잡의 최종 build 출력(``response``) 전체를 포함하므로,
    events와 동일하게 active async job(completed run 포함)에 대한 cross-owner
    접근이 상태 조회로 출력을 가져가지 못하게 차단한다.
    """

    def test_cross_owner_cannot_poll_active_job_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        entered = threading.Event()
        release = threading.Event()
        service = _BlockingBuildService(output_root=tmp_path, entered=entered, release=release)
        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="oidc", identifier="a", owner_id="oidc:owner-a"),
        )
        submitted = dispatch(
            service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "run1"}
        )
        assert submitted.status_code == 202
        assert entered.wait(timeout=5)

        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="oidc", identifier="b", owner_id="oidc:owner-b"),
        )
        resp = dispatch(service, "GET", "/builds/run1", None)
        assert resp.status_code == 403

        release.set()

    def test_owner_and_admin_can_poll_active_job_status(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        entered = threading.Event()
        release = threading.Event()
        service = _BlockingBuildService(output_root=tmp_path, entered=entered, release=release)
        owner = Principal(kind="oidc", identifier="a", owner_id="oidc:owner-a")
        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: owner)
        assert (
            dispatch(
                service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "run1"}
            ).status_code
            == 202
        )
        assert entered.wait(timeout=5)

        resp = dispatch(service, "GET", "/builds/run1", None)
        assert resp.status_code == 200
        assert resp.body["status"] == "running"

        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="dev", owner_id="dev:local"),
        )
        admin_resp = dispatch(service, "GET", "/builds/run1", None)
        assert admin_resp.status_code == 200

        release.set()

    def test_unknown_run_status_still_404_after_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        service = _service(tmp_path, threading.Event())
        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="oidc", identifier="a", owner_id="oidc:owner-a"),
        )

        resp = dispatch(service, "GET", "/builds/never-submitted", None)

        assert resp.status_code == 404

    def test_unsafe_run_id_rejected_in_status_route(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = _service(tmp_path, threading.Event())

        resp = dispatch(service, "GET", "/builds/..%2Fescape", None)

        assert resp.status_code == 400
