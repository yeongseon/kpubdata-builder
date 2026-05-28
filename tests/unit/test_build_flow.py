from __future__ import annotations

import json
from pathlib import Path

import pytest

from kpubdata_builder.errors import ValidationError
from kpubdata_builder.executor import execute_sources
from kpubdata_builder.spec import BuildSpec, ExportTarget, SourceRef


class FakeResult:
    def __init__(self, items: list[dict[str, object]]) -> None:
        self.items = items


class FakeDataset:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self._records = records
        self.seen_params: dict[str, object] | None = None

    def list(self, **params: object) -> FakeResult:
        self.seen_params = dict(params)
        return FakeResult(self._records)


class FakeClient:
    def __init__(self, records: list[dict[str, object]]) -> None:
        self._records = records
        self.seen_keys: list[str] = []

    def dataset(self, source_key: str) -> FakeDataset:
        self.seen_keys.append(source_key)
        return FakeDataset(self._records)


def _spec(kind: str = "jsonl") -> BuildSpec:
    return BuildSpec(
        dataset_id="test.dataset",
        title="Test",
        description="desc",
        sources=(SourceRef(provider="datago", dataset="air_quality"),),
        exports=(ExportTarget(kind=kind, output_path="data.jsonl"),),
        metadata={"owner": "team-data"},
    )


def test_execute_sources_collects_records_by_key() -> None:
    client = FakeClient([{"id": "1"}, {"id": "2"}])
    result = execute_sources(_spec(), client)
    assert result == {"datago.air_quality": [{"id": "1"}, {"id": "2"}]}
    assert client.seen_keys == ["datago.air_quality"]


def test_execute_sources_uses_alias_as_key() -> None:
    spec = BuildSpec(
        dataset_id="test.dataset",
        title="Test",
        description="desc",
        sources=(SourceRef(provider="datago", dataset="air_quality", alias="air"),),
        exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
    )
    client = FakeClient([{"id": "1"}])
    result = execute_sources(spec, client)
    assert "air" in result
    assert client.seen_keys == ["datago.air_quality"]


def test_execute_build_runs_full_pipeline(tmp_path: Path) -> None:
    from kpubdata_builder.build import execute_build

    client = FakeClient([{"id": "1", "name": "강남구"}, {"id": "2", "name": "서초구"}])
    result = execute_build(_spec(), client, output_dir=tmp_path)

    assert len(result.artifact_paths) == 1
    artifact = result.artifact_paths[0]
    assert artifact.exists()
    lines = artifact.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["name"] == "강남구"

    assert result.manifest_path.exists()
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["inputs"] == ["datago.air_quality"]
    assert manifest["row_counts"] == {"records": 2}
    assert manifest["outputs"] == [str(artifact)]


def test_execute_build_rejects_unsupported_export_kind(tmp_path: Path) -> None:
    from kpubdata_builder.build import execute_build

    client = FakeClient([{"id": "1"}])
    with pytest.raises(ValidationError):
        execute_build(_spec(kind="xml"), client, output_dir=tmp_path)


def test_cli_build_command_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import kpubdata_builder.cli as cli

    monkeypatch.setattr(cli, "_create_client", lambda: FakeClient([{"id": "1"}]))

    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        "dataset_id: test.ds\n"
        "title: Test\n"
        "description: desc\n"
        "sources:\n"
        "  - provider: datago\n"
        "    dataset: air_quality\n"
        "exports:\n"
        "  - kind: jsonl\n"
        "    output_path: data.jsonl\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "out"
    code = cli.main(["build", str(spec_path), "--output-dir", str(output_dir)])
    assert code == 0
    # 새 파이프라인은 output_dir/<run_id>/ 아래에 manifest를 기록한다.
    run_dirs = list(output_dir.iterdir())
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    assert (run_dir / "manifest.json").exists()
    assert (run_dir / "gold").is_dir()
