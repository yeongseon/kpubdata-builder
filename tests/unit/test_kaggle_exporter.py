"""KaggleExporter의 출력 규칙을 테스트로 고정한다.

CSV 본문은 CsvExporter와 동일하게 schema 우선 컬럼 순서를 따라야 하고,
dataset-metadata.json은 title/id/licenses/resources를 담아야 한다. 라이선스
오버라이드, 기존 메타데이터 병합, I/O 실패 정책을 회귀 테스트로 못 박는다.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

from kpubdata_builder import ArtifactDataset, ExportError
from kpubdata_builder.exporters import EXPORTER_REGISTRY, KaggleExporter
from kpubdata_builder.spec import ExportTarget


def _read_rows(path: Path) -> list[list[str]]:
    return list(csv.reader(io.StringIO(path.read_text(encoding="utf-8"))))


def _read_metadata(directory: Path) -> dict[str, object]:
    raw = (directory / "dataset-metadata.json").read_text(encoding="utf-8")
    return json.loads(raw)


def test_writes_csv_following_schema_and_valid_metadata(tmp_path: Path) -> None:
    # CSV 헤더는 schema 순서를 따르고, 메타데이터 json은 유효해야 한다.
    artifact = ArtifactDataset(
        records=({"b": "2", "a": "1"}, {"a": "3", "b": "4"}),
        schema={"a": "str", "b": "str"},
        metadata={"title": "Air Quality", "dataset_id": "kpub/air"},
    )
    target = ExportTarget(kind="kaggle", output_path="out/data.csv")

    result = KaggleExporter().export(artifact, target, tmp_path)

    assert _read_rows(result.output_path) == [["a", "b"], ["1", "2"], ["3", "4"]]

    metadata = _read_metadata(result.output_path.parent)
    assert metadata["title"] == "Air Quality"
    assert metadata["id"] == "kpub/air"
    assert metadata["licenses"] == [{"name": "CC-BY-4.0"}]
    assert metadata["resources"] == [{"path": "data.csv", "description": "Main dataset file"}]


def test_empty_records_with_schema_writes_header_only(tmp_path: Path) -> None:
    # schema는 있고 records가 없으면 헤더 한 줄만 기록한다.
    artifact = ArtifactDataset(records=(), schema={"id": "str", "name": "str"})
    target = ExportTarget(kind="kaggle", output_path="out/data.csv")

    result = KaggleExporter().export(artifact, target, tmp_path)

    assert result.output_path.read_text(encoding="utf-8") == "id,name\n"


def test_license_override_from_metadata(tmp_path: Path) -> None:
    # metadata.license가 있으면 그 값이 licenses name으로 반영된다.
    artifact = ArtifactDataset(
        records=({"id": "1"},),
        schema={"id": "str"},
        metadata={"license": "CC0-1.0", "title": "X", "dataset_id": "kpub/x"},
    )
    target = ExportTarget(kind="kaggle", output_path="out/data.csv")

    result = KaggleExporter().export(artifact, target, tmp_path)

    metadata = _read_metadata(result.output_path.parent)
    assert metadata["licenses"] == [{"name": "CC0-1.0"}]


def test_formula_injection_trigger_chars_prefixed_in_kaggle(tmp_path: Path) -> None:
    # KaggleExporter도 _format_cell을 공유하므로 수식 트리거 값에 접두사가 붙어야 한다.
    artifact = ArtifactDataset(
        records=({"cmd": '=HYPERLINK("evil.com")'},),
        schema={"cmd": "str"},
        metadata={"title": "T", "dataset_id": "kpub/t"},
    )
    target = ExportTarget(kind="kaggle", output_path="out/data.csv")

    result = KaggleExporter().export(artifact, target, tmp_path)

    rows = _read_rows(result.output_path)
    assert rows[1][0] == '\'=HYPERLINK("evil.com")'


def test_registry_exposes_kaggle_exporter() -> None:
    # Kaggle exporter가 kind 문자열 "kaggle"로 레지스트리에 등록되어 있는지 확인한다.
    assert isinstance(EXPORTER_REGISTRY["kaggle"], KaggleExporter)


def test_merges_resource_into_existing_metadata(tmp_path: Path) -> None:
    # 같은 디렉터리에 두 번 내보내면 resources에 두 경로가 모두 누적된다.
    target_one = ExportTarget(kind="kaggle", output_path="out/first.csv")
    target_two = ExportTarget(kind="kaggle", output_path="out/second.csv")
    artifact = ArtifactDataset(
        records=({"id": "1"},),
        schema={"id": "str"},
        metadata={"title": "First", "dataset_id": "kpub/first"},
    )

    first = KaggleExporter().export(artifact, target_one, tmp_path)
    KaggleExporter().export(
        ArtifactDataset(
            records=({"id": "2"},),
            schema={"id": "str"},
            metadata={"title": "Second", "dataset_id": "kpub/second"},
        ),
        target_two,
        tmp_path,
    )

    metadata = _read_metadata(first.output_path.parent)
    # 권한적 필드(title/id/licenses)는 최신 export 값으로 갱신되고, resources는 누적된다 (#202).
    assert metadata["title"] == "Second"
    assert metadata["id"] == "kpub/second"
    paths = {entry["path"] for entry in metadata["resources"]}  # type: ignore[index, union-attr]
    assert paths == {"first.csv", "second.csv"}


def test_reexport_refreshes_stale_top_level_metadata(tmp_path: Path) -> None:
    # 설정 변경 후 같은 파일로 재실행하면 stale id/title/licenses가 갱신되어야 한다 (#202).
    target = ExportTarget(kind="kaggle", output_path="out/data.csv")

    KaggleExporter().export(
        ArtifactDataset(
            records=({"id": "1"},),
            schema={"id": "str"},
            metadata={"title": "Old", "dataset_id": "kpub/old", "license": "CC-BY-4.0"},
        ),
        target,
        tmp_path,
    )
    result = KaggleExporter().export(
        ArtifactDataset(
            records=({"id": "1"},),
            schema={"id": "str"},
            metadata={"title": "New", "dataset_id": "kpub/new", "license": "CC0-1.0"},
        ),
        target,
        tmp_path,
    )

    metadata = _read_metadata(result.output_path.parent)
    assert metadata["title"] == "New"
    assert metadata["id"] == "kpub/new"
    assert metadata["licenses"] == [{"name": "CC0-1.0"}]


def test_wraps_io_failure_in_export_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 파일 쓰기 실패가 ExportError로 래핑되는지 확인한다.
    artifact = ArtifactDataset(records=({"id": "1"},), schema={"id": "str"})
    target = ExportTarget(kind="kaggle", output_path="out/data.csv")

    def raise_io_error(self: Path, data: str, *, encoding: str) -> int:
        del self, data, encoding
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "write_text", raise_io_error)

    with pytest.raises(ExportError):
        KaggleExporter().export(artifact, target, tmp_path)
