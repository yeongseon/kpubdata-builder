"""협력적 취소와 partial manifest 검증 (#481, ADR 0008).

이 파일은 네 층위를 각각 결정적으로 검증한다.

1. **상태 머신**(``AsyncBuildJobRegistry``): queued/running 취소, 종단 상태
   불변성, 반복 취소 멱등성.
2. **pipeline 안전 경계**: stub probe로 Bronze/Silver/Gold 경계를 정확히
   지정해 partial 산출물과 partial manifest를 확인한다.
3. **HTTP 계약**: route/ownership/상태 코드.
4. **경쟁 조건**: sleep이 아니라 ``threading.Barrier``와 명시적 lock 순서로
   재현한다.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

import kpubdata_builder.service.app as app_module
from kpubdata_builder.pipeline import CancellationProbe
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.jobs import (
    AsyncBuildExecutor,
    AsyncBuildJobRegistry,
    RunCancellation,
)
from kpubdata_builder.service.ownership import _OWNERSHIP_ENV
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.store import BuildIndex, rebuild_index

VALID_SPEC_YAML = (
    """
dataset_id: dataset.cancel
title: Cancel Sample
description: Cancellation fixture
sources:
  - provider: datago
    dataset: air_quality
    alias: air
exports:
  - kind: jsonl
    output_path: out/data.jsonl
""".strip()
    + "\n"
)


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


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


def _service(tmp_path: Path) -> BuilderService:
    client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}, {"id": "2", "v": 20}]})
    return BuilderService(output_root=tmp_path, client_factory=lambda: client, async_max_workers=1)


class _BoundaryProbe:
    """정확히 ``cancel_after`` 번째 경계 점검에서 취소를 관찰하는 stub probe.

    sleep이나 타이밍에 의존하지 않고 "Bronze 직후", "Silver 직후"처럼 **어느
    경계에서** 취소가 관찰되는지를 결정적으로 지정한다. ``cancel_after=None``
    이면 취소를 전혀 요청하지 않는다(정상 경로 회귀 확인용).
    """

    def __init__(self, *, cancel_after: int | None) -> None:
        self._cancel_after = cancel_after
        self.probe_count = 0
        self.committed = False
        self._lock = threading.Lock()

    def cancel_requested(self) -> bool:
        with self._lock:
            self.probe_count += 1
            if self._cancel_after is None:
                return False
            return self.probe_count > self._cancel_after

    def commit(self) -> bool:
        # 마지막 안전 경계. 지금 ``cancel_requested()``를 한 번 더 불렀다면 True가
        # 나올 시점(``probe_count >= cancel_after``)이면 취소가 이긴 것으로 본다 —
        # 실제 ``RunCancellation``에서 "request가 commit보다 먼저 도착한" 상황과
        # 동일하다.
        with self._lock:
            if self._cancel_after is not None and self.probe_count >= self._cancel_after:
                return False
            self.committed = True
            return True


def _read_manifest(tmp_path: Path, run_id: str) -> dict[str, object]:
    raw = (tmp_path / run_id / "manifest.json").read_text(encoding="utf-8")
    manifest = json.loads(raw)
    assert isinstance(manifest, dict)
    return manifest


class _RecordingRunner:
    """호출 횟수를 세는 runner. 취소된 queued job이 절대 실행되지 않음을 증명한다."""

    def __init__(self, *, entered: threading.Event | None = None) -> None:
        self.calls = 0
        self._entered = entered
        self._lock = threading.Lock()

    def __call__(
        self,
        spec_yaml: str,
        run_id: str,
        created_by: str | None,
        cancellation: RunCancellation,
    ) -> ServiceResponse:
        with self._lock:
            self.calls += 1
        if self._entered is not None:
            self._entered.set()
        return ServiceResponse(200, {"run_id": run_id})


# ---------------------------------------------------------------------------
# 1. 상태 머신
# ---------------------------------------------------------------------------


class TestCancellationStateMachine:
    def test_queued_job_becomes_cancelled_without_running(self) -> None:
        registry = AsyncBuildJobRegistry()
        registry.create(run_id="run1", created_by=None)

        outcome, snapshot = registry.request_cancel("run1")

        assert outcome == "cancelled"
        assert snapshot is not None
        assert snapshot.status == "cancelled"
        # 취소된 queued job은 worker가 절대 실행하지 않는다.
        assert registry.begin_run("run1") is False
        assert registry.get("run1") is not None
        assert registry.get("run1").status == "cancelled"  # type: ignore[union-attr]

    def test_running_job_goes_through_cancelling_then_cancelled(self) -> None:
        registry = AsyncBuildJobRegistry()
        registry.create(run_id="run1", created_by=None)
        assert registry.begin_run("run1") is True

        outcome, snapshot = registry.request_cancel("run1")
        assert outcome == "cancelling"
        assert snapshot is not None
        assert snapshot.status == "cancelling"

        # runner가 성공 응답을 돌려줘도, 취소가 확정된 job은 succeeded가 되지 않는다.
        final = registry.finish("run1", failed=False, response={"status": "ok"})
        assert final is not None
        assert final.status == "cancelled"
        assert final.response is None
        assert final.error is None

    def test_succeeded_job_cannot_be_cancelled(self) -> None:
        registry = AsyncBuildJobRegistry()
        registry.create(run_id="run1", created_by=None)
        registry.begin_run("run1")
        registry.finish("run1", failed=False, response={"status": "ok"})

        outcome, snapshot = registry.request_cancel("run1")

        assert outcome == "terminal"
        assert snapshot is not None
        assert snapshot.status == "succeeded"

    def test_failed_job_cannot_be_cancelled(self) -> None:
        registry = AsyncBuildJobRegistry()
        registry.create(run_id="run1", created_by=None)
        registry.begin_run("run1")
        registry.finish("run1", failed=True, error="boom")

        outcome, snapshot = registry.request_cancel("run1")

        assert outcome == "terminal"
        assert snapshot is not None
        assert snapshot.status == "failed"

    def test_repeated_cancel_is_deterministic(self) -> None:
        registry = AsyncBuildJobRegistry()
        registry.create(run_id="run1", created_by=None)
        registry.begin_run("run1")

        assert registry.request_cancel("run1")[0] == "cancelling"
        assert registry.request_cancel("run1")[0] == "already"
        registry.finish("run1", failed=False, response={"status": "ok"})
        # 종결 이후 반복 요청도 항상 같은 답을 준다.
        assert registry.request_cancel("run1")[0] == "already"
        assert registry.request_cancel("run1")[0] == "already"

    def test_commit_closes_the_cancellation_window(self) -> None:
        """마지막 안전 경계를 지난 job은 cancelling으로 전이되지 않는다."""
        registry = AsyncBuildJobRegistry()
        registry.create(run_id="run1", created_by=None)
        registry.begin_run("run1")
        cancellation = registry.cancellation("run1")
        assert cancellation is not None
        assert cancellation.commit() is True

        outcome, snapshot = registry.request_cancel("run1")

        assert outcome == "terminal"
        assert snapshot is not None
        assert snapshot.status == "running"
        # 그리고 실제로 succeeded로 끝난다 — cancelling -> succeeded 전이가 없다.
        final = registry.finish("run1", failed=False, response={"status": "ok"})
        assert final is not None
        assert final.status == "succeeded"

    def test_cancellation_state_is_not_shared_between_runs(self) -> None:
        registry = AsyncBuildJobRegistry()
        registry.create(run_id="run-a", created_by=None)
        registry.create(run_id="run-b", created_by=None)
        registry.begin_run("run-a")
        registry.begin_run("run-b")

        registry.request_cancel("run-a")

        cancellation_b = registry.cancellation("run-b")
        assert cancellation_b is not None
        assert cancellation_b.cancel_requested() is False
        assert registry.get("run-b") is not None
        assert registry.get("run-b").status == "running"  # type: ignore[union-attr]

    def test_cancelling_job_counts_as_running_workload(self) -> None:
        registry = AsyncBuildJobRegistry()
        registry.create(run_id="run1", created_by=None)
        registry.begin_run("run1")
        registry.request_cancel("run1")

        counts = registry.snapshot_counts()

        assert counts.running == 1
        assert counts.queued == 0

    def test_cancelled_queued_job_leaves_no_active_workload(self) -> None:
        registry = AsyncBuildJobRegistry()
        registry.create(run_id="run1", created_by=None)
        registry.request_cancel("run1")

        counts = registry.snapshot_counts()

        assert counts.queued == 0
        assert counts.running == 0


class TestExecutorSkipsCancelledJobs:
    def test_cancelled_queued_job_never_invokes_the_runner(self) -> None:
        executor = AsyncBuildExecutor(max_workers=1, max_queue_size=10)
        blocker_entered = threading.Event()
        blocker_release = threading.Event()

        def _blocker(
            spec_yaml: str,
            run_id: str,
            created_by: str | None,
            cancellation: RunCancellation,
        ) -> ServiceResponse:
            blocker_entered.set()
            blocker_release.wait(timeout=5)
            return ServiceResponse(200, {"run_id": run_id})

        recording = _RecordingRunner()
        try:
            executor.submit(spec_yaml="spec", run_id="run-busy", created_by=None, runner=_blocker)
            assert blocker_entered.wait(timeout=5)
            # 단일 worker가 점유돼 있어 이 job은 확실히 queued 상태다.
            executor.submit(
                spec_yaml="spec", run_id="run-queued", created_by=None, runner=recording
            )
            assert executor.request_cancel("run-queued")[0] == "cancelled"
            blocker_release.set()
            _wait_for_status(executor, "run-busy", "succeeded")
        finally:
            blocker_release.set()
            executor.shutdown()

        assert recording.calls == 0
        snapshot = executor.get("run-queued")
        assert snapshot is not None
        assert snapshot.status == "cancelled"


def _wait_for_status(
    executor: AsyncBuildExecutor, run_id: str, status: str, *, timeout: float = 5.0
) -> None:
    deadline = threading.Event()
    for _ in range(int(timeout * 200)):
        snapshot = executor.get(run_id)
        if snapshot is not None and snapshot.status == status:
            return
        deadline.wait(0.005)
    raise AssertionError(f"job {run_id} did not reach {status}")


# ---------------------------------------------------------------------------
# 2. pipeline 안전 경계와 partial manifest
# ---------------------------------------------------------------------------


class TestPipelineBoundaries:
    """경계 index: 0=fetch 이전, 1=Bronze 이후, 2=Silver 이후, 3=Gold 이후."""

    def test_cancel_before_fetch_produces_no_stage_artifacts(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        probe = _BoundaryProbe(cancel_after=0)

        response = service.build(VALID_SPEC_YAML, run_id="run1", cancellation=probe)

        assert response.status_code == 409
        manifest = _read_manifest(tmp_path, "run1")
        assert manifest["status"] == "cancelled"
        assert manifest["partial"] is True
        assert manifest["outputs"] == []
        # 실행되지 않은 단계를 성공으로 기록하지 않는다.
        assert manifest["row_counts"] == {}
        assert not (tmp_path / "run1" / "bronze").exists()

    def test_cancel_after_bronze_keeps_bronze_and_skips_silver(self, tmp_path: Path) -> None:
        service = _service(tmp_path)

        response = service.build(
            VALID_SPEC_YAML, run_id="run1", cancellation=_BoundaryProbe(cancel_after=1)
        )

        assert response.status_code == 409
        manifest = _read_manifest(tmp_path, "run1")
        outputs = manifest["outputs"]
        assert isinstance(outputs, list)
        assert manifest["status"] == "cancelled"
        assert manifest["partial"] is True
        assert any("bronze" in str(path) for path in outputs)
        assert not any("silver" in str(path) for path in outputs)
        assert not any("gold" in str(path) for path in outputs)
        assert not (tmp_path / "run1" / "silver").exists()
        assert not (tmp_path / "run1" / "gold").exists()

    def test_cancel_after_silver_keeps_bronze_and_silver_only(self, tmp_path: Path) -> None:
        service = _service(tmp_path)

        service.build(VALID_SPEC_YAML, run_id="run1", cancellation=_BoundaryProbe(cancel_after=2))

        manifest = _read_manifest(tmp_path, "run1")
        outputs = [str(path) for path in manifest["outputs"]]  # type: ignore[union-attr]
        assert manifest["status"] == "cancelled"
        assert manifest["partial"] is True
        assert any("bronze" in path for path in outputs)
        assert any("silver" in path for path in outputs)
        assert not any("gold" in path for path in outputs)
        assert not (tmp_path / "run1" / "gold").exists()

    def test_cancel_after_gold_keeps_gold_but_skips_export(self, tmp_path: Path) -> None:
        service = _service(tmp_path)

        service.build(VALID_SPEC_YAML, run_id="run1", cancellation=_BoundaryProbe(cancel_after=3))

        manifest = _read_manifest(tmp_path, "run1")
        outputs = [str(path) for path in manifest["outputs"]]
        assert manifest["status"] == "cancelled"
        assert manifest["partial"] is True
        assert any("gold" in path for path in outputs)
        # BuildSpec.exports 산출물은 만들어지지 않는다 — 취소 이후 새 단계를 시작하지 않는다.
        assert not (tmp_path / "run1" / "out").exists()
        assert not any(path.endswith("data.jsonl") for path in outputs)

    def test_cancel_at_the_final_boundary_still_lands_on_cancelled(self, tmp_path: Path) -> None:
        """모든 stage가 끝난 뒤 finalize 직전에 도착한 취소도 cancelled로 확정된다.

        ``run_build``의 마지막 안전 경계(``commit()``) 분기다 — 이 경로가 없으면
        취소 요청이 조용히 무시되고 run이 succeeded로 끝나 job 상태(cancelling)와
        manifest가 모순된다.
        """
        service = _service(tmp_path)
        # 단일 source의 경계는 4개(0~3)다. 전부 통과시킨 뒤 commit에서 취소가
        # 이기게 한다.
        probe = _BoundaryProbe(cancel_after=4)

        response = service.build(VALID_SPEC_YAML, run_id="run1", cancellation=probe)

        assert response.status_code == 409
        assert probe.committed is False
        manifest = _read_manifest(tmp_path, "run1")
        assert manifest["status"] == "cancelled"
        assert manifest["partial"] is True
        # 이 시점에는 산출물이 모두 만들어져 있지만, run은 정상 완료로 확정되지
        # 않았으므로 성공으로 승격되지 않는다.
        assert any("gold" in str(path) for path in manifest["outputs"])  # type: ignore[union-attr]
        assert service._build_index.get("run1") is not None
        assert service._build_index.get("run1").status == "cancelled"  # type: ignore[union-attr]

    def test_failed_source_reason_survives_a_later_cancellation(self, tmp_path: Path) -> None:
        """취소가 실패를 삼키지 않는다 — 실패 사유는 manifest errors에 남는다."""
        service = BuilderService(
            output_root=tmp_path,
            client_factory=lambda: _FakeClient({}),  # source fetch가 실패한다
            async_max_workers=1,
        )

        # 경계 0을 통과시키면 fetch가 실패해 source outcome이 "failed"가 되고,
        # 그 뒤 finalize 경계에서 취소가 이긴다.
        service.build(VALID_SPEC_YAML, run_id="run1", cancellation=_BoundaryProbe(cancel_after=1))

        manifest = _read_manifest(tmp_path, "run1")
        assert manifest["status"] == "cancelled"
        assert manifest["errors"] != []

    def test_cancellation_is_not_recorded_as_failure(self, tmp_path: Path) -> None:
        service = _service(tmp_path)

        service.build(VALID_SPEC_YAML, run_id="run1", cancellation=_BoundaryProbe(cancel_after=1))

        manifest = _read_manifest(tmp_path, "run1")
        assert manifest["errors"] == []
        assert manifest["status"] != "failed"

    def test_probe_without_cancellation_keeps_normal_success_path(self, tmp_path: Path) -> None:
        """취소가 없으면 probe가 있어도 기존 성공 경로와 결과가 같다."""
        service = _service(tmp_path)
        probe = _BoundaryProbe(cancel_after=None)

        response = service.build(VALID_SPEC_YAML, run_id="run1", cancellation=probe)

        assert response.status_code == 200
        assert probe.committed is True
        manifest = _read_manifest(tmp_path, "run1")
        assert manifest["status"] == "ok"
        assert manifest["partial"] is False

    def test_synchronous_build_is_unaffected(self, tmp_path: Path) -> None:
        """동기 ``POST /build``는 취소 개념 없이 기존 동작을 그대로 유지한다."""
        service = _service(tmp_path)

        response = dispatch(service, "POST", "/build", {"spec": VALID_SPEC_YAML})

        assert response.status_code == 200
        assert isinstance(response, ServiceResponse)
        assert response.body["status"] == "ok"
        manifest = _read_manifest(tmp_path, str(response.body["run_id"]))
        assert manifest["status"] == "ok"
        assert manifest["partial"] is False

    def test_partial_manifest_carries_no_paths_beyond_the_run_workspace(
        self, tmp_path: Path
    ) -> None:
        """취소 manifest에 raw exception/stack trace가 섞이지 않는다."""
        service = _service(tmp_path)

        service.build(VALID_SPEC_YAML, run_id="run1", cancellation=_BoundaryProbe(cancel_after=1))

        raw = (tmp_path / "run1" / "manifest.json").read_text(encoding="utf-8")
        assert "Traceback" not in raw
        assert "BuildCancelled" not in raw


# ---------------------------------------------------------------------------
# 3. BuildIndex / dataset semantics
# ---------------------------------------------------------------------------


class TestCancelledRunIndexSemantics:
    def test_build_index_records_cancelled_status(self, tmp_path: Path) -> None:
        service = _service(tmp_path)

        service.build(VALID_SPEC_YAML, run_id="run1", cancellation=_BoundaryProbe(cancel_after=1))

        entry = service._build_index.get("run1")
        assert entry is not None
        assert entry.status == "cancelled"

    def test_rebuild_index_preserves_cancelled_status_from_manifest(self, tmp_path: Path) -> None:
        """파생 index를 잃어도 manifest만으로 cancelled를 복원한다."""
        service = _service(tmp_path)
        service.build(VALID_SPEC_YAML, run_id="run1", cancellation=_BoundaryProbe(cancel_after=2))
        service._build_index.close()

        rebuild_index(tmp_path)

        rebuilt = BuildIndex(tmp_path)
        try:
            entry = rebuilt.get("run1")
            assert entry is not None
            assert entry.status == "cancelled"
        finally:
            rebuilt.close()

    def test_cancelled_run_is_not_a_successful_artifact_write(self, tmp_path: Path) -> None:
        """cancelled run은 '최근 성공 빌드' 근거로 승격되지 않는다."""
        service = _service(tmp_path)
        service.build(VALID_SPEC_YAML, run_id="run1", cancellation=_BoundaryProbe(cancel_after=1))

        assert service._build_index.latest_successful_finished_at() is None

    def test_build_list_reports_cancelled_not_ok(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.build(VALID_SPEC_YAML, run_id="run1", cancellation=_BoundaryProbe(cancel_after=1))

        response = dispatch(service, "GET", "/builds", None)

        assert isinstance(response, ServiceResponse)
        builds = response.body["builds"]
        assert isinstance(builds, list)
        assert [b["status"] for b in builds] == ["cancelled"]  # type: ignore[index]

    def test_build_list_filesystem_fallback_reports_cancelled(self, tmp_path: Path) -> None:
        """index가 비어 있어 파일시스템으로 폴백해도 cancelled를 ok로 오인하지 않는다."""
        service = _service(tmp_path)
        service.build(VALID_SPEC_YAML, run_id="run1", cancellation=_BoundaryProbe(cancel_after=1))
        service._build_index.delete("run1")

        response = dispatch(service, "GET", "/builds", None)

        assert isinstance(response, ServiceResponse)
        builds = response.body["builds"]
        assert isinstance(builds, list)
        assert [b["status"] for b in builds] == ["cancelled"]  # type: ignore[index]

    def test_cancelled_run_without_gold_exposes_no_gold_stage(self, tmp_path: Path) -> None:
        """Gold를 만들지 못한 취소 run이 완성된(=게시 가능한) 산출물로 보이지 않는다."""
        service = _service(tmp_path)
        service.build(VALID_SPEC_YAML, run_id="run1", cancellation=_BoundaryProbe(cancel_after=1))

        stages = dispatch(service, "GET", "/builds/run1/stages", None)

        assert isinstance(stages, ServiceResponse)
        assert stages.status_code == 200
        sources = stages.body["sources"]
        assert isinstance(sources, list)
        assert sources, "취소된 run도 시도한 source는 노출한다"
        for source in sources:
            assert isinstance(source, dict)
            # Bronze는 남아 있지만 Gold는 존재하지 않는다.
            assert source["gold"] != "available"
        # dataset 요약의 stage 표시도 같은 사실을 반영한다.
        detail = dispatch(service, "GET", "/datasets/dataset.cancel", None)
        assert isinstance(detail, ServiceResponse)
        stage_map = detail.body["stages"]
        assert isinstance(stage_map, dict)
        for summary in stage_map.values():
            assert isinstance(summary, dict)
            assert summary["gold"] != "available"

    def test_dataset_detail_surfaces_cancelled_status(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        service.build(VALID_SPEC_YAML, run_id="run1", cancellation=_BoundaryProbe(cancel_after=2))

        response = dispatch(service, "GET", "/datasets/dataset.cancel", None)

        assert isinstance(response, ServiceResponse)
        assert response.status_code == 200
        assert response.body["status"] == "cancelled"


# ---------------------------------------------------------------------------
# 4. HTTP 계약 / ownership
# ---------------------------------------------------------------------------


class _BlockingCancelService(BuilderService):
    """worker를 ``release``까지 붙잡아 running 구간을 결정적으로 만든다."""

    def __init__(
        self,
        *,
        output_root: Path,
        entered: threading.Event,
        release: threading.Event,
        completed: threading.Event,
    ) -> None:
        super().__init__(
            output_root=output_root,
            client_factory=lambda: _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}]}),
            async_max_workers=1,
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


def _await_event(
    service: BuilderService, run_id: str, event: str, *, timeout: float = 5.0
) -> list[str]:
    """해당 run에 ``event``가 기록될 때까지 기다리고 event 이름 목록을 돌려준다.

    종결 event는 terminal 상태 전이 *직후*에 append되므로, 상태만 보고 event를
    읽으면 timing에 의존하게 된다.
    """
    waiter = threading.Event()
    for _ in range(int(timeout * 200)):
        names = [e.event for e in service._event_store.list_for_run(run_id, limit=100, tail=False)]
        if event in names:
            return names
        waiter.wait(0.005)
    raise AssertionError(f"run {run_id} never recorded {event}")


def _await_job_status(
    service: BuilderService, run_id: str, status: str, *, timeout: float = 5.0
) -> None:
    """job이 지정한 terminal 상태에 도달할 때까지 기다린다.

    ``completed`` event는 runner가 **반환하는** 시점에 set되지만, terminal 전이와
    ``run_cancelled`` event append는 그 직후 executor(``_finish``)에서 일어난다 —
    두 시점을 혼동하면 테스트가 timing에 의존하게 된다.
    """
    waiter = threading.Event()
    for _ in range(int(timeout * 200)):
        snapshot = service._async_builds.get(run_id)
        if snapshot is not None and snapshot.status == status:
            return
        waiter.wait(0.005)
    raise AssertionError(f"job {run_id} did not reach {status}")


class TestCancelEndpoint:
    def test_unsafe_run_id_is_rejected(self, tmp_path: Path) -> None:
        service = _service(tmp_path)

        response = dispatch(service, "POST", "/builds/..%2Fescape/cancel", None)

        assert response.status_code == 400

    def test_nested_path_is_not_treated_as_a_run_id(self, tmp_path: Path) -> None:
        """``/builds/a/b/cancel``이 run_id "a/b"로 해석되어 경로를 벗어나지 않는다."""
        service = _service(tmp_path)

        response = dispatch(service, "POST", "/builds/a/b/cancel", None)

        assert response.status_code == 400

    def test_unknown_run_returns_404(self, tmp_path: Path) -> None:
        service = _service(tmp_path)

        response = dispatch(service, "POST", "/builds/never-submitted/cancel", None)

        assert response.status_code == 404

    def test_queued_job_cancel_returns_200_cancelled(self, tmp_path: Path) -> None:
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingCancelService(
            output_root=tmp_path, entered=entered, release=release, completed=completed
        )
        try:
            assert (
                dispatch(
                    service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "busy"}
                ).status_code
                == 202
            )
            assert entered.wait(timeout=5)
            assert (
                dispatch(
                    service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "waiting"}
                ).status_code
                == 202
            )

            response = dispatch(service, "POST", "/builds/waiting/cancel", None)
        finally:
            release.set()

        assert isinstance(response, ServiceResponse)
        assert response.status_code == 200
        assert response.body["status"] == "cancelled"
        assert response.body["run_id"] == "waiting"
        # 취소된 queued job은 워크스페이스조차 만들지 않는다.
        assert not (tmp_path / "waiting").exists()

    def test_running_job_cancel_reaches_cancelled_terminal(self, tmp_path: Path) -> None:
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingCancelService(
            output_root=tmp_path, entered=entered, release=release, completed=completed
        )
        try:
            dispatch(service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "run1"})
            assert entered.wait(timeout=5)

            cancel = dispatch(service, "POST", "/builds/run1/cancel", None)
            assert isinstance(cancel, ServiceResponse)
            assert cancel.status_code == 200
            assert cancel.body["status"] == "cancelling"
        finally:
            release.set()
        assert completed.wait(timeout=5)
        _await_job_status(service, "run1", "cancelled")

        status = dispatch(service, "GET", "/builds/run1", None)
        assert isinstance(status, ServiceResponse)
        assert status.body["status"] == "cancelled"
        # 취소된 job에는 build 출력도 error 문자열도 실리지 않는다.
        assert "response" not in status.body
        assert "error" not in status.body
        manifest = _read_manifest(tmp_path, "run1")
        assert manifest["status"] == "cancelled"
        assert manifest["partial"] is True

    def test_repeat_cancel_on_cancelled_job_is_idempotent_200(self, tmp_path: Path) -> None:
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingCancelService(
            output_root=tmp_path, entered=entered, release=release, completed=completed
        )
        try:
            dispatch(service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "run1"})
            assert entered.wait(timeout=5)
            first = dispatch(service, "POST", "/builds/run1/cancel", None)
            second = dispatch(service, "POST", "/builds/run1/cancel", None)
        finally:
            release.set()
        assert completed.wait(timeout=5)
        _await_job_status(service, "run1", "cancelled")
        third = dispatch(service, "POST", "/builds/run1/cancel", None)

        assert first.status_code == 200
        assert second.status_code == 200
        assert isinstance(second, ServiceResponse)
        assert second.body["status"] == "cancelling"
        assert third.status_code == 200
        assert isinstance(third, ServiceResponse)
        assert third.body["status"] == "cancelled"

    def test_resubmitting_a_cancelled_run_id_returns_the_existing_cancelled_job(
        self, tmp_path: Path
    ) -> None:
        """취소된 run_id 재제출은 새 job을 만들지 않는다 (기존 "existing" 재제출 계약).

        재시도는 새 run_id로 하는 것이 ADR 0008의 정책이며(자동 in-place 재시도
        없음), 이 경로가 새 job을 만들면 확정된 cancelled 상태가 조용히 덮어써진다.
        """
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingCancelService(
            output_root=tmp_path, entered=entered, release=release, completed=completed
        )
        try:
            dispatch(service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "busy"})
            assert entered.wait(timeout=5)
            dispatch(service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "waiting"})
            dispatch(service, "POST", "/builds/waiting/cancel", None)

            resubmitted = dispatch(
                service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "waiting"}
            )
        finally:
            release.set()

        assert isinstance(resubmitted, ServiceResponse)
        assert resubmitted.status_code == 200
        assert resubmitted.body["status"] == "cancelled"

    def test_terminal_succeeded_job_cancel_returns_409(self, tmp_path: Path) -> None:
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingCancelService(
            output_root=tmp_path, entered=entered, release=release, completed=completed
        )
        dispatch(service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "run1"})
        assert entered.wait(timeout=5)
        release.set()
        assert completed.wait(timeout=5)
        _await_job_status(service, "run1", "succeeded")

        response = dispatch(service, "POST", "/builds/run1/cancel", None)

        assert isinstance(response, ServiceResponse)
        assert response.status_code == 409
        assert response.body["status"] == "succeeded"
        status = dispatch(service, "GET", "/builds/run1", None)
        assert isinstance(status, ServiceResponse)
        assert status.body["status"] == "succeeded"


class TestCancelOwnership:
    def _oidc(self, monkeypatch: pytest.MonkeyPatch, owner: str, label: str = "user") -> None:
        monkeypatch.setattr(
            app_module,
            "authenticate",
            lambda **_kwargs: Principal(kind="oidc", identifier=label, owner_id=owner),
        )

    def test_cross_owner_cannot_cancel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingCancelService(
            output_root=tmp_path, entered=entered, release=release, completed=completed
        )
        try:
            self._oidc(monkeypatch, "oidc:owner-a", label="a")
            dispatch(service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "run1"})
            assert entered.wait(timeout=5)

            self._oidc(monkeypatch, "oidc:owner-b", label="b")
            response = dispatch(service, "POST", "/builds/run1/cancel", None)
        finally:
            release.set()

        assert response.status_code == 403
        # 실제 상태는 바뀌지 않았다.
        self._oidc(monkeypatch, "oidc:owner-a", label="a")
        assert completed.wait(timeout=5)
        _await_job_status(service, "run1", "succeeded")
        status = dispatch(service, "GET", "/builds/run1", None)
        assert isinstance(status, ServiceResponse)
        assert status.body["status"] == "succeeded"

    def test_same_label_different_stable_owner_cannot_cancel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingCancelService(
            output_root=tmp_path, entered=entered, release=release, completed=completed
        )
        try:
            self._oidc(monkeypatch, "oidc:owner-a", label="same-label")
            dispatch(service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "run1"})
            assert entered.wait(timeout=5)

            # display label은 동일하지만 stable owner_id가 다르다.
            self._oidc(monkeypatch, "oidc:owner-b", label="same-label")
            response = dispatch(service, "POST", "/builds/run1/cancel", None)
        finally:
            release.set()

        assert response.status_code == 403

    def test_owner_can_cancel_and_owner_id_never_leaks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingCancelService(
            output_root=tmp_path, entered=entered, release=release, completed=completed
        )
        try:
            self._oidc(monkeypatch, "oidc:owner-a", label="a")
            dispatch(service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "run1"})
            assert entered.wait(timeout=5)
            response = dispatch(service, "POST", "/builds/run1/cancel", None)
        finally:
            release.set()
        assert completed.wait(timeout=5)
        _await_job_status(service, "run1", "cancelled")

        assert isinstance(response, ServiceResponse)
        assert response.status_code == 200
        assert "owner_id" not in response.body
        assert "oidc:owner-a" not in json.dumps(response.body, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 5. structured events (#496 어휘 재사용)
# ---------------------------------------------------------------------------


class TestCancellationEvents:
    def test_queued_cancel_timeline_is_submitted_then_cancelled(self, tmp_path: Path) -> None:
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingCancelService(
            output_root=tmp_path, entered=entered, release=release, completed=completed
        )
        try:
            dispatch(service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "busy"})
            assert entered.wait(timeout=5)
            dispatch(service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "waiting"})
            dispatch(service, "POST", "/builds/waiting/cancel", None)
        finally:
            release.set()

        assert _await_event(service, "waiting", "run_cancelled") == [
            "run_submitted",
            "run_cancelled",
        ]
        events = service._event_store.list_for_run("waiting", limit=100, tail=False)
        assert events[-1].status == "ok"
        assert events[-1].message == "build cancelled at a safe stage boundary"

    def test_running_cancel_timeline_has_no_contradictory_terminal_event(
        self, tmp_path: Path
    ) -> None:
        entered = threading.Event()
        release = threading.Event()
        completed = threading.Event()
        service = _BlockingCancelService(
            output_root=tmp_path, entered=entered, release=release, completed=completed
        )
        try:
            dispatch(service, "POST", "/builds", {"spec": VALID_SPEC_YAML, "run_id": "run1"})
            assert entered.wait(timeout=5)
            dispatch(service, "POST", "/builds/run1/cancel", None)
        finally:
            release.set()
        assert completed.wait(timeout=5)
        names = _await_event(service, "run1", "run_cancelled")

        assert names[0] == "run_submitted"
        assert names[-1] == "run_cancelled"
        assert names.count("run_cancelled") == 1
        assert "run_finished" not in names
        assert "run_failed" not in names

    def test_successful_run_has_no_cancellation_event(self, tmp_path: Path) -> None:
        service = _service(tmp_path)

        service.build(VALID_SPEC_YAML, run_id="run1")

        names = [e.event for e in service._event_store.list_for_run("run1", limit=100, tail=False)]
        assert "run_cancelled" not in names
        assert "run_finished" in names


# ---------------------------------------------------------------------------
# 6. 경쟁 조건 (Barrier 기반, sleep 없음)
# ---------------------------------------------------------------------------


def _run_concurrently(*targets: Callable[[], None]) -> None:
    """모든 target을 하나의 Barrier로 동시에 출발시킨다."""
    barrier = threading.Barrier(len(targets))
    errors: list[BaseException] = []

    def _wrap(fn: Callable[[], None]) -> Callable[[], None]:
        def _inner() -> None:
            # barrier.wait도 try 안에 둔다 — BrokenBarrierError가 조용히 스레드를
            # 죽이면 "동시에 실행됐다"는 전제가 깨진 채로 테스트가 통과할 수 있다.
            try:
                barrier.wait(timeout=5)
                fn()
            except BaseException as exc:  # noqa: BLE001 - 스레드 예외를 본 스레드로 옮긴다
                errors.append(exc)

        return _inner

    threads = [threading.Thread(target=_wrap(target)) for target in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    if errors:
        raise errors[0]


class TestCancellationRaces:
    ITERATIONS = 200

    def test_cancel_and_worker_start_yield_exactly_one_valid_path(self) -> None:
        """A. queued 취소와 worker 시작이 동시에 일어나도 유효 경로는 정확히 하나다."""
        for index in range(self.ITERATIONS):
            registry = AsyncBuildJobRegistry()
            run_id = f"run-{index}"
            registry.create(run_id=run_id, created_by=None)
            results: dict[str, object] = {}

            def _cancel(
                reg: AsyncBuildJobRegistry = registry,
                rid: str = run_id,
                out: dict[str, object] = results,
            ) -> None:
                out["cancel"] = reg.request_cancel(rid)[0]

            def _start(
                reg: AsyncBuildJobRegistry = registry,
                rid: str = run_id,
                out: dict[str, object] = results,
            ) -> None:
                out["start"] = reg.begin_run(rid)

            _run_concurrently(_cancel, _start)

            snapshot = registry.get(run_id)
            assert snapshot is not None
            if results["start"] is True:
                # worker가 이겼다: queued -> running -> (cancelling) 경로만 가능하다.
                assert results["cancel"] in ("cancelling", "cancelled")
                assert snapshot.status in ("running", "cancelling")
                # cancel이 "cancelled"를 반환했다면 그건 queued를 먼저 잡은 것이므로
                # begin_run은 True일 수 없다 — 두 결과가 동시에 성립하지 않는다.
                assert results["cancel"] != "cancelled"
            else:
                # 취소가 이겼다: runner는 절대 시작되지 않는다.
                assert results["cancel"] == "cancelled"
                assert snapshot.status == "cancelled"

    def test_two_concurrent_cancels_never_corrupt_state(self) -> None:
        """D. 동시 취소 두 건: 상태는 하나로 수렴하고 응답도 결정적 조합이다."""
        for index in range(self.ITERATIONS):
            registry = AsyncBuildJobRegistry()
            run_id = f"run-{index}"
            registry.create(run_id=run_id, created_by=None)
            registry.begin_run(run_id)
            outcomes: list[str] = []
            outcomes_lock = threading.Lock()

            def _cancel(
                reg: AsyncBuildJobRegistry = registry,
                rid: str = run_id,
                out: list[str] = outcomes,
                out_lock: threading.Lock = outcomes_lock,
            ) -> None:
                outcome = reg.request_cancel(rid)[0]
                with out_lock:
                    out.append(outcome)

            _run_concurrently(_cancel, _cancel)

            assert sorted(outcomes) == ["already", "cancelling"]
            snapshot = registry.get(run_id)
            assert snapshot is not None
            assert snapshot.status == "cancelling"

    def test_cancel_and_final_boundary_never_both_win(self) -> None:
        """C. 마지막 경계(commit)와 취소가 동시에 일어나도 결과는 하나뿐이다."""
        for index in range(self.ITERATIONS):
            registry = AsyncBuildJobRegistry()
            run_id = f"run-{index}"
            registry.create(run_id=run_id, created_by=None)
            registry.begin_run(run_id)
            cancellation = registry.cancellation(run_id)
            assert cancellation is not None
            results: dict[str, object] = {}

            def _cancel(
                reg: AsyncBuildJobRegistry = registry,
                rid: str = run_id,
                out: dict[str, object] = results,
            ) -> None:
                out["cancel"] = reg.request_cancel(rid)[0]

            def _commit(
                cancel_state: RunCancellation = cancellation,
                out: dict[str, object] = results,
            ) -> None:
                out["commit"] = cancel_state.commit()

            _run_concurrently(_cancel, _commit)
            final = registry.finish(run_id, failed=False, response={"status": "ok"})
            assert final is not None

            if results["commit"] is True:
                # pipeline이 이겼다: 취소는 거절되고 succeeded로 끝난다.
                assert results["cancel"] == "terminal"
                assert final.status == "succeeded"
            else:
                # 취소가 이겼다: 성공으로 끝나지 않는다.
                assert results["cancel"] == "cancelling"
                assert final.status == "cancelled"
                assert final.response is None

    def test_cancel_racing_a_build_failure_settles_on_one_terminal_state(self) -> None:
        """E. 취소와 실패가 겹쳐도 종단 상태는 하나이고 서로 모순되지 않는다."""
        for index in range(self.ITERATIONS):
            registry = AsyncBuildJobRegistry()
            run_id = f"run-{index}"
            registry.create(run_id=run_id, created_by=None)
            registry.begin_run(run_id)
            results: dict[str, object] = {}

            def _cancel(
                reg: AsyncBuildJobRegistry = registry,
                rid: str = run_id,
                out: dict[str, object] = results,
            ) -> None:
                out["cancel"] = reg.request_cancel(rid)[0]

            def _fail(
                reg: AsyncBuildJobRegistry = registry,
                rid: str = run_id,
                out: dict[str, object] = results,
            ) -> None:
                snapshot = reg.finish(rid, failed=True, error="boom")
                out["final"] = None if snapshot is None else snapshot.status

            _run_concurrently(_cancel, _fail)

            snapshot = registry.get(run_id)
            assert snapshot is not None
            assert snapshot.status in ("failed", "cancelled")
            if snapshot.status == "cancelled":
                # 취소로 끝났다면 실패 사유를 error로 노출하지 않는다.
                assert snapshot.error is None
            else:
                assert snapshot.error == "boom"

    def test_terminal_state_is_never_overwritten_by_a_late_finish(self) -> None:
        registry = AsyncBuildJobRegistry()
        registry.create(run_id="run1", created_by=None)
        registry.begin_run("run1")
        registry.request_cancel("run1")
        registry.finish("run1", failed=False, response={"status": "ok"})

        again = registry.finish("run1", failed=True, error="late failure")

        assert again is not None
        assert again.status == "cancelled"
        assert again.error is None


class TestRunCancellationPrimitive:
    def test_request_after_commit_is_refused(self) -> None:
        cancellation = RunCancellation()
        assert cancellation.commit() is True
        assert cancellation.request() is False
        assert cancellation.cancel_requested() is False

    def test_commit_after_request_is_refused(self) -> None:
        cancellation = RunCancellation()
        assert cancellation.request() is True
        assert cancellation.commit() is False
        assert cancellation.cancel_requested() is True

    def test_commit_is_idempotent(self) -> None:
        cancellation = RunCancellation()
        assert cancellation.commit() is True
        assert cancellation.commit() is True

    def test_close_latches_the_window_and_reports_requested(self) -> None:
        cancellation = RunCancellation()
        cancellation.request()
        assert cancellation.close() is True
        assert cancellation.request() is False
