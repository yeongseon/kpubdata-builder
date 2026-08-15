"""비동기 build job 서비스 동작 검증 (#482)."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
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
        self, spec_yaml: str, *, run_id: str | None = None, created_by: str | None = None
    ) -> ServiceResponse:
        try:
            return super().build(spec_yaml, run_id=run_id, created_by=created_by)
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
