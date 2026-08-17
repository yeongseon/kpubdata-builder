"""``pipeline.orchestrator.run_build``의 structured event 방출 테스트 (#496).

HTTP route/ownership/bounded query는 test_events_api.py가 다룬다. 이 파일은
실제 실행 boundary(run/source fetch/medallion stage/quality checkpoint)에서만
event가 발생하는지, 실행되지 않은 stage를 완료로 가장하지 않는지, 실패해도
이전 성공 event가 남아 있는지를 orchestrator 수준에서 직접 검증한다.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

from kpubdata_builder.events import BuildEvent, BuildEventStore
from kpubdata_builder.pipeline import run_build
from kpubdata_builder.spec import BuildSpec, ExportTarget, JsonValue, SourceRef
from kpubdata_builder.spec.models import SchemaContract


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


def _spec(*sources: SourceRef, quality: object = None) -> BuildSpec:
    kwargs: dict[str, object] = {}
    if quality is not None:
        kwargs["quality"] = quality
    return BuildSpec(
        dataset_id="events.fixture",
        title="Events Fixture",
        description="fixture",
        sources=tuple(sources),
        exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
        **kwargs,
    )


def _names(events: tuple[BuildEvent, ...]) -> list[str]:
    return [e.event for e in events]


class TestRunLifecycle:
    def test_successful_run_emits_started_then_finished(self, tmp_path: Path) -> None:
        client = _FakeClient({"datago.air": [{"id": "1"}]})
        store = BuildEventStore(tmp_path)
        spec = _spec(SourceRef(provider="datago", dataset="air", alias="air"))

        result = run_build(
            spec, client=client, output_root=tmp_path, run_id="r1", event_store=store
        )

        assert result.status == "ok"
        events = store.list_for_run("r1", limit=100, tail=False)
        assert events[0].event == "run_started"
        assert events[-1].event == "run_finished"
        assert events[-1].status == "ok"

    def test_failed_run_emits_run_failed_with_status_fail(self, tmp_path: Path) -> None:
        client = _FakeClient({})  # 모든 source가 fetch 실패
        store = BuildEventStore(tmp_path)
        spec = _spec(SourceRef(provider="datago", dataset="missing", alias="air"))

        result = run_build(
            spec, client=client, output_root=tmp_path, run_id="r1", event_store=store
        )

        assert result.status == "failed"
        events = store.list_for_run("r1", limit=100, tail=False)
        assert events[-1].event == "run_failed"
        assert events[-1].status == "fail"

    def test_no_event_store_does_not_raise(self, tmp_path: Path) -> None:
        """event_store를 생략한 기존 호출자(CLI 등)는 아무 것도 바뀌지 않는다."""
        client = _FakeClient({"datago.air": [{"id": "1"}]})
        spec = _spec(SourceRef(provider="datago", dataset="air", alias="air"))

        result = run_build(spec, client=client, output_root=tmp_path, run_id="r1")

        assert result.status == "ok"

    def test_run_events_are_timezone_aware_utc(self, tmp_path: Path) -> None:
        client = _FakeClient({"datago.air": [{"id": "1"}]})
        store = BuildEventStore(tmp_path)
        spec = _spec(SourceRef(provider="datago", dataset="air", alias="air"))

        run_build(spec, client=client, output_root=tmp_path, run_id="r1", event_store=store)

        events = store.list_for_run("r1", limit=100, tail=False)
        assert events
        for event in events:
            assert event.timestamp.tzinfo is not None


class TestSourceFetchLifecycle:
    def test_fetch_success_emits_started_then_completed(self, tmp_path: Path) -> None:
        client = _FakeClient({"datago.air": [{"id": "1"}, {"id": "2"}]})
        store = BuildEventStore(tmp_path)
        spec = _spec(SourceRef(provider="datago", dataset="air", alias="air"))

        run_build(spec, client=client, output_root=tmp_path, run_id="r1", event_store=store)

        events = store.list_for_run("r1", limit=100, tail=False)
        fetch_events = [e for e in events if e.event.startswith("source_fetch")]
        assert [e.event for e in fetch_events] == ["source_fetch_started", "source_fetch_completed"]
        assert fetch_events[0].source_key == "air"
        assert fetch_events[1].metrics == {"records": 2}

    def test_fetch_failure_emits_failed_not_completed(self, tmp_path: Path) -> None:
        client = _FakeClient({})
        store = BuildEventStore(tmp_path)
        spec = _spec(SourceRef(provider="datago", dataset="missing", alias="air"))

        run_build(spec, client=client, output_root=tmp_path, run_id="r1", event_store=store)

        events = store.list_for_run("r1", limit=100, tail=False)
        fetch_events = [e for e in events if e.event.startswith("source_fetch")]
        assert [e.event for e in fetch_events] == ["source_fetch_started", "source_fetch_failed"]
        assert fetch_events[1].status == "fail"

    def test_source_key_is_output_facing_alias(self, tmp_path: Path) -> None:
        client = _FakeClient({"datago.air": [{"id": "1"}]})
        store = BuildEventStore(tmp_path)
        spec = _spec(SourceRef(provider="datago", dataset="air", alias="my-alias"))

        run_build(spec, client=client, output_root=tmp_path, run_id="r1", event_store=store)

        events = store.list_for_run("r1", limit=100, tail=False)
        source_keys = {e.source_key for e in events if e.source_key is not None}
        assert source_keys == {"my-alias"}


class TestStageLifecycle:
    def test_full_success_emits_all_four_stages_in_order(self, tmp_path: Path) -> None:
        client = _FakeClient({"datago.air": [{"id": "1"}]})
        store = BuildEventStore(tmp_path)
        spec = _spec(SourceRef(provider="datago", dataset="air", alias="air"))

        run_build(spec, client=client, output_root=tmp_path, run_id="r1", event_store=store)

        events = store.list_for_run("r1", limit=100, tail=False)
        stage_events = [(e.stage, e.event) for e in events if e.stage is not None]
        assert stage_events == [
            ("bronze", "stage_started"),
            ("bronze", "stage_completed"),
            ("silver", "stage_started"),
            ("silver", "stage_completed"),
            ("gold", "stage_started"),
            ("gold", "stage_completed"),
            ("export", "stage_started"),
            ("export", "stage_completed"),
        ]

    def test_bronze_failure_does_not_emit_silver_or_gold(self, tmp_path: Path) -> None:
        client = _FakeClient({})
        store = BuildEventStore(tmp_path)
        spec = _spec(SourceRef(provider="datago", dataset="missing", alias="air"))

        run_build(spec, client=client, output_root=tmp_path, run_id="r1", event_store=store)

        events = store.list_for_run("r1", limit=100, tail=False)
        stages_seen = {e.stage for e in events if e.stage is not None}
        assert stages_seen == {"bronze"}
        bronze_events = [e.event for e in events if e.stage == "bronze"]
        assert bronze_events == ["stage_started", "stage_failed"]

    def test_silver_validation_failure_bronze_stays_completed(self, tmp_path: Path) -> None:
        """silver가 실패해도 bronze의 성공 event는 삭제되지 않는다(append-only)."""
        client = _FakeClient({"datago.air": [{"id": "1"}]})
        store = BuildEventStore(tmp_path)
        # required column이 실제로 존재하지 않는 컬럼이라 silver validation이 실패한다.
        spec = BuildSpec(
            dataset_id="events.fixture",
            title="Events Fixture",
            description="fixture",
            sources=(
                SourceRef(
                    provider="datago",
                    dataset="air",
                    alias="air",
                    schema=SchemaContract(required=("does_not_exist",)),
                ),
            ),
            exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
        )

        result = run_build(
            spec, client=client, output_root=tmp_path, run_id="r1", event_store=store
        )

        assert result.status == "failed"
        events = store.list_for_run("r1", limit=100, tail=False)
        bronze_events = [e.event for e in events if e.stage == "bronze"]
        assert bronze_events == ["stage_started", "stage_completed"]
        silver_events = [e.event for e in events if e.stage == "silver"]
        assert silver_events == ["stage_started", "stage_failed"]
        assert "gold" not in {e.stage for e in events}

    def test_not_reached_stage_never_marked_completed(self, tmp_path: Path) -> None:
        """도달하지 못한 stage는 completed/failed 어느 쪽으로도 기록되지 않는다."""
        client = _FakeClient({})
        store = BuildEventStore(tmp_path)
        spec = _spec(SourceRef(provider="datago", dataset="missing", alias="air"))

        run_build(spec, client=client, output_root=tmp_path, run_id="r1", event_store=store)

        events = store.list_for_run("r1", limit=100, tail=False)
        for stage in ("silver", "gold", "export"):
            assert stage not in {e.stage for e in events}


class TestQualityCheckpoint:
    def test_quality_evaluated_emitted_once_per_source(self, tmp_path: Path) -> None:
        client = _FakeClient({"datago.air": [{"id": "1"}]})
        store = BuildEventStore(tmp_path)
        spec = _spec(SourceRef(provider="datago", dataset="air", alias="air"))

        run_build(spec, client=client, output_root=tmp_path, run_id="r1", event_store=store)

        events = store.list_for_run("r1", limit=100, tail=False)
        quality_events = [e for e in events if e.event == "quality_evaluated"]
        assert len(quality_events) == 1
        assert quality_events[0].status == "ok"

    def test_quality_evaluated_survives_downstream_gate_failure(self, tmp_path: Path) -> None:
        """quality FAIL로 소스가 실패해도 quality_evaluated event 자체는 남는다."""
        from kpubdata_builder.spec.models import QualityPolicy

        client = _FakeClient({"datago.air": [{"id": "1"}, {"id": "2"}]})
        store = BuildEventStore(tmp_path)
        spec = _spec(
            SourceRef(provider="datago", dataset="air", alias="air"),
            quality=QualityPolicy(min_rows=100, min_rows_severity="fail"),
        )

        result = run_build(
            spec, client=client, output_root=tmp_path, run_id="r1", event_store=store
        )

        assert result.status == "failed"
        events = store.list_for_run("r1", limit=100, tail=False)
        quality_events = [e for e in events if e.event == "quality_evaluated"]
        assert len(quality_events) == 1
        assert quality_events[0].status == "fail"
        assert quality_events[0].metrics is not None
        assert quality_events[0].metrics["fail_count"] >= 1


class TestPartialRunMultiSource:
    def test_one_success_one_failure_timeline_shows_both(self, tmp_path: Path) -> None:
        client = _FakeClient({"datago.good": [{"id": "1"}]})
        store = BuildEventStore(tmp_path)
        spec = _spec(
            SourceRef(provider="datago", dataset="good", alias="good"),
            SourceRef(provider="datago", dataset="bad", alias="bad"),
        )

        result = run_build(
            spec, client=client, output_root=tmp_path, run_id="r1", event_store=store
        )

        assert result.status == "failed"
        events = store.list_for_run("r1", limit=100, tail=False)
        good_events = [e.event for e in events if e.source_key == "good"]
        bad_events = [e.event for e in events if e.source_key == "bad"]
        assert "stage_completed" in good_events  # 성공한 source의 event가 살아남는다
        assert "stage_failed" in bad_events
        assert events[-1].event == "run_failed"


def _selective_failing_append(
    monkeypatch: pytest.MonkeyPatch, should_fail: Callable[[BuildEvent], bool]
) -> None:
    """``event``가 ``should_fail``에 해당할 때만 append를 실패시키는 fault injection.

    나머지 event는 원래 구현(``BuildEventStore.append``)에 그대로 위임한다 —
    특정 boundary 하나만 장애를 겪고 나머지 timeline은 정상 기록되는, 실제
    transient 장애(디스크 hiccup 등)에 가까운 상황을 흉내낸다.
    """
    original_append = BuildEventStore.append

    def _append(self: BuildEventStore, event: BuildEvent) -> BuildEvent:
        if should_fail(event):
            raise RuntimeError(f"simulated event store outage for {event.event}")
        return original_append(self, event)

    monkeypatch.setattr(BuildEventStore, "append", _append)


def _manifest_warnings(manifest_path: Path) -> list[str]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return list(manifest["warnings"])


class TestRecorderFailureIsolation:
    def test_event_append_failure_does_not_fail_otherwise_successful_build(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """event 기록 인프라 장애가 이미 성공한 build를 실패시키지 않는다 (#496).

        BuildEventStore.append() 자체는 실패를 삼키지 않지만(test_event_store.py),
        recorder가 그 실패를 흡수해 build 진행에는 영향을 주지 않는다 — 그러나
        (ADR 0003의 "파생 인덱스라 잃어도 된다"는 이유가 아니라) event 기록
        실패가 manifest/소스 outcome이라는 *다른* 정본을 침범하지 않기 위해서다.
        그 흡수가 조용한 실종이 아니라는 것은 아래 fault-injection 테스트들이
        ``BuildManifest.warnings``를 통해 확인한다.
        """
        client = _FakeClient({"datago.air": [{"id": "1"}]})
        store = BuildEventStore(tmp_path)

        def _broken_append(self: BuildEventStore, event: BuildEvent) -> BuildEvent:
            raise RuntimeError("simulated event store outage")

        monkeypatch.setattr(BuildEventStore, "append", _broken_append)
        spec = _spec(SourceRef(provider="datago", dataset="air", alias="air"))

        result = run_build(
            spec, client=client, output_root=tmp_path, run_id="r1", event_store=store
        )

        assert result.status == "ok"
        assert result.outcomes[0].status == "ok"
        assert _manifest_warnings(result.manifest_path)  # 실패가 완전히 사라지지 않는다

    def test_run_started_append_failure_still_builds_and_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run_started append가 실패해도 build는 계속 진행되고 manifest에 남는다 (#496)."""
        client = _FakeClient({"datago.air": [{"id": "1"}]})
        store = BuildEventStore(tmp_path)
        _selective_failing_append(monkeypatch, lambda e: e.event == "run_started")
        spec = _spec(SourceRef(provider="datago", dataset="air", alias="air"))

        result = run_build(
            spec, client=client, output_root=tmp_path, run_id="r1", event_store=store
        )

        assert result.status == "ok"
        assert result.manifest_path.exists()  # AGENTS.md '매니페스트 누락 금지'
        warnings = _manifest_warnings(result.manifest_path)
        assert any("run_started" in w for w in warnings)
        # 이후 event(run_finished 등)는 store가 복구됐으므로 정상 기록된다 —
        # 하나의 장애가 나머지 timeline까지 지워버리지 않는다(append-only).
        events = store.list_for_run("r1", limit=100, tail=False)
        assert events[-1].event == "run_finished"

    def test_stage_completed_append_failure_does_not_flip_successful_source_to_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """bronze stage_completed append 실패가 실제로 성공한 source를 실패로 뒤집지 않는다.

        stage_completed는 실제 persist(``persist_bronze_artifact``)가 *이미 끝난 뒤*
        호출된다 — 여기서 예외가 파이프라인 제어 흐름으로 새어나가면
        ``_run_source_pipeline``의 공통 except가 "이 source 실패"로 잘못 해석해
        실제로는 디스크에 이미 쓰인 bronze 산출물을 실패로 보고하게 된다(#496).
        """
        client = _FakeClient({"datago.air": [{"id": "1"}]})
        store = BuildEventStore(tmp_path)
        _selective_failing_append(
            monkeypatch, lambda e: e.event == "stage_completed" and e.stage == "bronze"
        )
        spec = _spec(SourceRef(provider="datago", dataset="air", alias="air"))

        result = run_build(
            spec, client=client, output_root=tmp_path, run_id="r1", event_store=store
        )

        assert result.status == "ok"
        assert result.outcomes[0].status == "ok"
        assert result.outcomes[0].stages_completed == ("bronze", "silver", "gold")
        bronze_dir = tmp_path / "r1" / "bronze"
        assert bronze_dir.exists() and any(bronze_dir.iterdir())  # 실제 산출물이 살아있다
        warnings = _manifest_warnings(result.manifest_path)
        assert any("stage_completed" in w and "bronze" in w for w in warnings)

    def test_run_finished_append_failure_still_writes_manifest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run_finished append 실패가 manifest 기록 자체를 막지 않는다 (#496).

        recorder.run_finished()은 manifest_writer보다 먼저 호출된다
        (pipeline/orchestrator.py) — 만약 이 실패가 그대로 전파돼 run_build를
        중단시키면 manifest.json이 아예 생기지 않는, AGENTS.md '매니페스트
        누락 금지'를 위반하는 훨씬 나쁜 상태가 된다.
        """
        client = _FakeClient({"datago.air": [{"id": "1"}]})
        store = BuildEventStore(tmp_path)
        _selective_failing_append(monkeypatch, lambda e: e.event == "run_finished")
        spec = _spec(SourceRef(provider="datago", dataset="air", alias="air"))

        result = run_build(
            spec, client=client, output_root=tmp_path, run_id="r1", event_store=store
        )

        assert result.status == "ok"
        assert result.manifest_path.exists()
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert manifest["errors"] == []  # 실제 소스 실행은 전혀 실패하지 않았다
        assert any("run_finished" in w for w in manifest["warnings"])

    def test_run_failed_append_failure_still_writes_manifest_with_real_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """run_failed append 실패도 manifest 기록과 실제 실패 사유를 가리지 않는다 (#496)."""
        client = _FakeClient({})  # 모든 source가 fetch 실패
        store = BuildEventStore(tmp_path)
        _selective_failing_append(monkeypatch, lambda e: e.event == "run_failed")
        spec = _spec(SourceRef(provider="datago", dataset="missing", alias="air"))

        result = run_build(
            spec, client=client, output_root=tmp_path, run_id="r1", event_store=store
        )

        assert result.status == "failed"
        assert result.manifest_path.exists()
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert manifest["errors"]  # 진짜 실패 사유는 event 기록 실패와 무관하게 남는다
        assert any("run_failed" in w for w in manifest["warnings"])
