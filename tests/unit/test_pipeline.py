"""Pipeline orchestrator(#48): Bronze→Silver→Gold 실행·워크스페이스·manifest 검증."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import cast

import polars as pl
import pytest

import kpubdata_builder.pipeline.orchestrator as orchestrator
from kpubdata_builder.pipeline import BuildContext, BuildResult, run_build
from kpubdata_builder.spec import BuildSpec, ExportTarget, JsonValue, SourceRef


class _FakeResult:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items: list[dict[str, JsonValue]] = items

    @property
    def items(self) -> Iterable[dict[str, JsonValue]]:
        return self._items


class _FakeDataset:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items: list[dict[str, JsonValue]] = items

    def list(self, **_params: JsonValue) -> _FakeResult:
        return _FakeResult(self._items)


class _FakeClient:
    """source_key → 레코드 매핑을 돌려주는 테스트용 클라이언트."""

    def __init__(self, data: dict[str, list[dict[str, JsonValue]]]) -> None:
        self._data: dict[str, list[dict[str, JsonValue]]] = data

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


def test_build_context_create_validates_and_defaults_run_id(tmp_path: Path) -> None:
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))

    ctx = BuildContext.create(spec, output_root=tmp_path)

    assert ctx.run_id  # 비어 있지 않은 기본 run_id
    assert ctx.output_root == tmp_path
    assert ctx.spec is spec


def test_run_build_executes_full_pipeline_and_writes_workspace(tmp_path: Path) -> None:
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
    manifest = cast(
        dict[str, JsonValue], json.loads(result.manifest_path.read_text(encoding="utf-8"))
    )
    assert manifest["build_id"] == "run1"
    inputs = cast(list[str], manifest["inputs"])
    assert "datago.apt_trade" in inputs
    outputs = cast(list[str], manifest["outputs"])
    assert str(run_dir / "silver" / "datago.apt_trade" / "schema.json") in outputs
    assert str(run_dir / "silver" / "datago.apt_trade" / "stats.json") in outputs
    assert str(run_dir / "silver" / "datago.apt_trade" / "preview.json") in outputs
    assert str(run_dir / "silver" / "datago.apt_trade" / "validation.json") in outputs
    assert str(run_dir / "gold" / "datago.apt_trade" / "package.json") in outputs

    # gold parquet 산출
    gold_parquet = run_dir / "gold" / "datago.apt_trade" / "table.parquet"
    assert gold_parquet.exists()
    assert pl.read_parquet(gold_parquet).to_dicts() == [
        {"id": "1", "amount": 1000},
        {"id": "2", "amount": 2500},
    ]


def test_run_build_writes_dataset_card_readme(tmp_path: Path) -> None:
    # 성공한 빌드의 gold 디렉터리에 dataset card README.md가 생성되는지 검증한다 (#37).
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
    client = _FakeClient(
        {"datago.apt_trade": [{"id": "1", "amount": 1000}, {"id": "2", "amount": 2500}]}
    )

    result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

    readme = tmp_path / "run1" / "gold" / "datago.apt_trade" / "README.md"
    assert readme.exists()
    text = readme.read_text(encoding="utf-8")
    assert "# Apartment Trades" in text
    assert "## Schema" in text
    assert "- datago.apt_trade" in text

    manifest = cast(
        dict[str, JsonValue], json.loads(result.manifest_path.read_text(encoding="utf-8"))
    )
    outputs = cast(list[str], manifest["outputs"])
    assert str(readme) in outputs


def test_run_build_uses_alias_as_source_key(tmp_path: Path) -> None:
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade", alias="trades"))
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

    assert result.status == "ok"
    assert result.outcomes[0].source_key == "trades"
    assert (tmp_path / "run1" / "gold" / "trades" / "table.parquet").exists()


def test_run_build_records_failure_when_source_missing(tmp_path: Path) -> None:
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
    manifest = cast(
        dict[str, JsonValue], json.loads(result.manifest_path.read_text(encoding="utf-8"))
    )
    assert manifest["errors"]


def test_run_build_preserves_partial_artifacts_when_later_stage_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}, {"id": "2"}]})

    def _fail_gold(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("gold failed")

    monkeypatch.setattr(orchestrator, "build_gold_package", _fail_gold)

    result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

    assert result.status == "failed"
    assert result.outcomes[0].stages_completed == ("bronze", "silver")
    assert (tmp_path / "run1" / "bronze" / "datago.apt_trade").is_dir()
    assert (tmp_path / "run1" / "silver" / "datago.apt_trade" / "table.parquet").exists()
    assert not (tmp_path / "run1" / "gold" / "datago.apt_trade").exists()

    manifest = cast(
        dict[str, JsonValue], json.loads(result.manifest_path.read_text(encoding="utf-8"))
    )
    outputs = cast(list[str], manifest["outputs"])
    assert str(tmp_path / "run1" / "silver" / "datago.apt_trade" / "preview.json") in outputs
    assert str(tmp_path / "run1" / "gold" / "datago.apt_trade" / "table.parquet") not in outputs



def test_run_build_writes_schema_summaries_to_manifest(tmp_path: Path) -> None:
    # 성공한 빌드의 manifest.json에 소스별 schema summary가 기록되는지 검증한다 (#11).
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
    client = _FakeClient(
        {"datago.apt_trade": [{"id": "1", "amount": 1000}, {"id": "2", "amount": 2500}]}
    )

    result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

    manifest = cast(
        dict[str, JsonValue], json.loads(result.manifest_path.read_text(encoding="utf-8"))
    )
    summaries = cast(dict[str, JsonValue], manifest["schema_summaries"])
    apt = cast(dict[str, JsonValue], summaries["datago.apt_trade"])
    assert apt["total_fields"] == 2
    fields = cast(list[dict[str, JsonValue]], apt["fields"])
    assert [(f["name"], f["nullable"]) for f in fields] == [("id", False), ("amount", False)]
    # 타입 문자열은 polars dtype 표현을 그대로 싣는다(정수 컬럼).
    amount_type = cast(str, fields[1]["type"])
    assert "Int" in amount_type


def test_run_build_writes_provenance_to_manifest(tmp_path: Path) -> None:
    # 성공한 빌드의 manifest.json에 소스별 상세 provenance가 기록되는지 검증한다 (#12).
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
    client = _FakeClient(
        {"datago.apt_trade": [{"id": "1", "amount": 1000}, {"id": "2", "amount": 2500}]}
    )

    result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

    manifest = cast(
        dict[str, JsonValue], json.loads(result.manifest_path.read_text(encoding="utf-8"))
    )
    provenance = cast(list[dict[str, JsonValue]], manifest["provenance"])
    assert len(provenance) == 1
    entry = provenance[0]
    assert entry["provider"] == "datago"
    assert entry["dataset"] == "apt_trade"
    assert entry["record_count"] == 2
    assert cast(str, entry["data_checksum"]).startswith("sha256:")
    assert cast(str, entry["fetched_at"]).endswith("+00:00")


def test_run_build_rejects_unsafe_run_id(tmp_path: Path) -> None:
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    with pytest.raises(ValueError, match="run_id"):
        _ = run_build(spec, client=client, output_root=tmp_path, run_id="../escape")
