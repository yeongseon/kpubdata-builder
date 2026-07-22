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


def test_run_build_card_uses_alias_as_source_identity(tmp_path: Path) -> None:
    # #225: alias가 설정된 경우 dataset card의 sources 항목도 output_key(alias)를 사용해야
    # manifest의 inputs 필드와 일치해야 한다.
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade", alias="trades"))
    client = _FakeClient({"datago.apt_trade": [{"id": "1", "amount": 1000}]})

    result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

    readme = tmp_path / "run1" / "gold" / "trades" / "README.md"
    assert readme.exists()
    text = readme.read_text(encoding="utf-8")
    # card는 alias(output_key)를 source 식별자로 사용해야 한다.
    assert "- trades" in text
    # fetch_key(provider.dataset)는 card에 나타나지 않아야 한다.
    assert "- datago.apt_trade" not in text

    # manifest inputs도 alias를 사용한다 — 두 곳이 일치해야 한다.
    import json
    from typing import cast

    manifest = cast(dict[str, object], json.loads(result.manifest_path.read_text(encoding="utf-8")))
    assert "trades" in cast(list[str], manifest["inputs"])


def test_run_build_redacts_path_from_unexpected_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # #225: 예상치 못한 예외(OS 오류 등)의 절대 경로가 클라이언트에 노출되지 않아야 한다.
    # #246: 상세 정보는 warnings.warn이 아닌 logger.error로 기록해야 한다.
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    def _fail_with_path(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("failed: /absolute/path/to/file.json")

    monkeypatch.setattr(orchestrator, "build_gold_package", _fail_with_path)

    import logging

    with caplog.at_level(logging.ERROR, logger="kpubdata_builder.pipeline.orchestrator"):
        result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

    outcome = result.outcomes[0]
    assert outcome.status == "failed"
    # 클라이언트에게 돌아가는 error 메시지에는 절대 경로가 없어야 한다.
    assert "/absolute/path" not in (outcome.error or "")
    # 상세 정보는 logger.error로만 기록된다 (#246).
    assert any("/absolute/path" in r.getMessage() for r in caplog.records)


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


def test_run_build_fails_source_when_silver_validation_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 검증 실패한 Silver 데이터셋은 Gold로 흘러가지 않고 소스가 실패 처리되어야 한다 (#189).
    import dataclasses

    from kpubdata_builder.stages.silver import build_silver_dataset as real_build
    from kpubdata_builder.stages.silver.models import ValidationResult
    from kpubdata_builder.stages.silver.validate import ValidationProblem

    def _invalid_silver(*args: object, **kwargs: object) -> object:
        dataset = real_build(*args, **kwargs)  # type: ignore[arg-type]
        return dataclasses.replace(
            dataset,
            validation=ValidationResult(
                ok=False,
                problems=(
                    ValidationProblem(
                        code="synthetic_failure",
                        field=None,
                        message="synthetic validation failure",
                    ),
                ),
            ),
        )

    monkeypatch.setattr(orchestrator, "build_silver_dataset", _invalid_silver)

    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    result = run_build(spec, client=client, output_root=tmp_path, run_id="run1")

    assert result.status == "failed"
    outcome = result.outcomes[0]
    assert outcome.status == "failed"
    assert "synthetic validation failure" in (outcome.error or "")
    # Gold 단계까지 가지 않는다.
    assert "gold" not in outcome.stages_completed
    assert not (tmp_path / "run1" / "gold" / "datago.apt_trade").exists()


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


def test_run_build_executes_sources_concurrently(tmp_path: Path) -> None:
    # 소스별 fetch가 순차 실행되면 총 시간이 소스 수 * delay만큼 걸린다. 스레드 풀로
    # 동시 실행되면 총 소요 시간이 delay 1~2회 분량에 가까워야 한다 (#247).
    import time

    delay = 0.2

    class _SlowClient(_FakeClient):
        def dataset(self, source_key: str) -> _FakeDataset:
            time.sleep(delay)
            return super().dataset(source_key)

    spec = _spec(
        SourceRef(provider="datago", dataset="a"),
        SourceRef(provider="datago", dataset="b"),
        SourceRef(provider="datago", dataset="c"),
    )
    client = _SlowClient(
        {"datago.a": [{"id": "1"}], "datago.b": [{"id": "1"}], "datago.c": [{"id": "1"}]}
    )

    started = time.monotonic()
    result = run_build(spec, client=client, output_root=tmp_path, run_id="run-parallel")
    elapsed = time.monotonic() - started

    assert result.status == "ok"
    assert len(result.outcomes) == 3
    # 순차 실행이면 3 * delay(0.6s) 이상 걸린다. 병렬 실행이면 2 * delay(0.4s) 미만이어야 한다.
    assert elapsed < delay * 2


def test_run_build_preserves_source_order_in_manifest_with_multiple_sources(
    tmp_path: Path,
) -> None:
    # 스레드 풀 완료 순서가 뒤바뀌어도 manifest의 inputs/outcomes는 spec.sources
    # 순서를 유지해 결정적이어야 한다 (#247: executor.map은 제출 순서로 결과를 반환).
    spec = _spec(
        SourceRef(provider="datago", dataset="a"),
        SourceRef(provider="datago", dataset="b"),
        SourceRef(provider="datago", dataset="c"),
    )
    client = _FakeClient(
        {"datago.a": [{"id": "1"}], "datago.b": [{"id": "1"}], "datago.c": [{"id": "1"}]}
    )

    result = run_build(spec, client=client, output_root=tmp_path, run_id="run-order")

    assert result.status == "ok"
    assert [o.source_key for o in result.outcomes] == [
        "datago.a",
        "datago.b",
        "datago.c",
    ]
    manifest = cast(
        dict[str, JsonValue], json.loads(result.manifest_path.read_text(encoding="utf-8"))
    )
    assert manifest["inputs"] == ["datago.a", "datago.b", "datago.c"]


def test_run_build_validates_spec_before_running(tmp_path: Path) -> None:
    # 잘못된 spec(소스 없음)은 단계 진입 전 fail-fast로 거부되어야 한다 (#212).
    from kpubdata_builder.errors import ValidationError

    bad_spec = BuildSpec(
        dataset_id="apt_trade",
        title="Apartment Trades",
        description="seoul apartment trades",
        sources=(),
        exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
    )
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    with pytest.raises(ValidationError, match="at least one source"):
        _ = run_build(bad_spec, client=client, output_root=tmp_path, run_id="run1")

    # fail-fast: manifest나 워크스페이스가 생성되지 않는다.
    assert not (tmp_path / "run1").exists()
