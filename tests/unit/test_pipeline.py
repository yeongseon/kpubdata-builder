"""Pipeline orchestrator(#48): Bronze→Silver→Gold 실행·워크스페이스·manifest 검증."""

from __future__ import annotations

import json
from collections.abc import Iterable

import polars as pl
import pytest

from kpubdata_builder.pipeline import BuildContext, BuildResult, run_build
from kpubdata_builder.spec import BuildSpec, ExportTarget, JsonValue, SourceRef


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
    """source_key → 레코드 매핑을 돌려주는 테스트용 클라이언트."""

    def __init__(self, data: dict[str, list[dict[str, JsonValue]]]) -> None:
        self._data = data

    def dataset(self, source_key: str) -> _FakeDataset:
        if source_key not in self._data:
            raise KeyError(f"unknown source: {source_key}")
        return _FakeDataset(self._data[source_key])


def _spec(*sources: SourceRef) -> BuildSpec:
    return BuildSpec(
        dataset_id="apt_trade",
        title="Apartment Trades",
        description="seoul apartment trades",
        sources=tuple(sources),
        exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
    )


def test_build_context_create_validates_and_defaults_run_id(tmp_path) -> None:  # type: ignore[no-untyped-def]
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))

    ctx = BuildContext.create(spec, output_root=tmp_path)

    assert ctx.run_id  # 비어 있지 않은 기본 run_id
    assert ctx.output_root == tmp_path
    assert ctx.spec is spec


def test_run_build_executes_full_pipeline_and_writes_workspace(tmp_path) -> None:  # type: ignore[no-untyped-def]
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
    client = _FakeClient(
        {"datago.apt_trade": [{"id": "1", "amount": 1000}, {"id": "2", "amount": 2500}]}
    )

    result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

    assert isinstance(result, BuildResult)
    assert result.status == "ok"
    assert len(result.outcomes) == 1
    outcome = result.outcomes[0]
    assert outcome.source_key == "datago.apt_trade"
    assert outcome.status == "ok"
    assert outcome.stages_completed == ("bronze", "silver", "gold")

    # run workspace 디렉터리 구조
    run_dir = tmp_path / "run1"
    assert (run_dir / "bronze").is_dir()
    assert (run_dir / "silver").is_dir()
    assert (run_dir / "gold").is_dir()

    # manifest 기록
    assert result.manifest_path.exists()
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["build_id"] == "run1"
    assert "datago.apt_trade" in manifest["inputs"]

    # gold parquet 산출
    gold_parquet = run_dir / "gold" / "datago.apt_trade" / "table.parquet"
    assert gold_parquet.exists()
    assert pl.read_parquet(gold_parquet).to_dicts() == [
        {"id": "1", "amount": 1000},
        {"id": "2", "amount": 2500},
    ]


def test_run_build_uses_alias_as_source_key(tmp_path) -> None:  # type: ignore[no-untyped-def]
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade", alias="trades"))
    client = _FakeClient({"trades": [{"id": "1"}]})

    result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

    assert result.status == "ok"
    assert result.outcomes[0].source_key == "trades"


def test_run_build_records_failure_when_source_missing(tmp_path) -> None:  # type: ignore[no-untyped-def]
    spec = _spec(SourceRef(provider="datago", dataset="missing"))
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

    assert result.status == "failed"
    outcome = result.outcomes[0]
    assert outcome.status == "failed"
    assert outcome.error is not None
    assert "bronze" not in outcome.stages_completed

    # 실패해도 manifest는 남는다
    assert result.manifest_path.exists()
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["errors"]


def test_run_build_rejects_unsafe_run_id(tmp_path) -> None:  # type: ignore[no-untyped-def]
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    with pytest.raises(ValueError, match="run_id"):
        run_build(spec, client=client, output_root=tmp_path, run_id="../escape")
