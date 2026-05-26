"""JsonlExporter의 출력 규칙을 테스트로 고정한다.

JSONL은 "한 줄 = 한 레코드"라는 계약이 핵심이므로, 줄 수·유니코드 보존·
키 정렬·빈 데이터 정책·반환 메타데이터를 회귀 테스트로 못 박는다.
"""

from __future__ import annotations

import json
from pathlib import Path

from kpubdata_builder import ArtifactDataset
from kpubdata_builder.exporters import JsonlExporter
from kpubdata_builder.spec import ExportTarget


def test_each_record_is_one_json_line(tmp_path: Path) -> None:
    # 레코드 2개면 파일도 정확히 2줄이어야 하고, 각 줄은 독립 JSON이어야 한다.
    artifact = ArtifactDataset(records=({"id": "1"}, {"id": "2"}))
    target = ExportTarget(kind="jsonl", output_path="out/data.jsonl")

    result = JsonlExporter().export(artifact, target, tmp_path)

    lines = result.output_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [json.loads(line) for line in lines] == [{"id": "1"}, {"id": "2"}]


def test_unicode_is_preserved_without_ascii_escaping(tmp_path: Path) -> None:
    # 한글이 \uXXXX로 escape되지 않고 그대로 보존되는지 확인한다.
    artifact = ArtifactDataset(records=({"name": "대기오염정보"},))
    target = ExportTarget(kind="jsonl", output_path="out/data.jsonl")

    result = JsonlExporter().export(artifact, target, tmp_path)

    raw = result.output_path.read_text(encoding="utf-8")
    assert "대기오염정보" in raw
    assert "\\u" not in raw


def test_keys_are_sorted_for_deterministic_output(tmp_path: Path) -> None:
    # 삽입 순서와 무관하게 키가 정렬되어 결정적(deterministic) 출력이 되는지 확인한다.
    artifact = ArtifactDataset(records=({"b": "2", "a": "1"},))
    target = ExportTarget(kind="jsonl", output_path="out/data.jsonl")

    result = JsonlExporter().export(artifact, target, tmp_path)

    assert result.output_path.read_text(encoding="utf-8") == '{"a": "1", "b": "2"}\n'


def test_non_empty_output_ends_with_single_trailing_newline(tmp_path: Path) -> None:
    # 비어있지 않은 출력은 마지막 줄바꿈 1개로 끝나야 한다(끝에 빈 줄이 없어야 함).
    artifact = ArtifactDataset(records=({"id": "1"},))
    target = ExportTarget(kind="jsonl", output_path="out/data.jsonl")

    result = JsonlExporter().export(artifact, target, tmp_path)

    raw = result.output_path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert not raw.endswith("\n\n")


def test_empty_records_write_empty_file(tmp_path: Path) -> None:
    # 빈 데이터는 빈 파일(내용 없음)로 기록되는 정책을 고정한다.
    artifact = ArtifactDataset(records=())
    target = ExportTarget(kind="jsonl", output_path="out/data.jsonl")

    result = JsonlExporter().export(artifact, target, tmp_path)

    assert result.output_path.read_text(encoding="utf-8") == ""
    assert result.file_size == 0


def test_returns_metadata_pointing_to_created_file(tmp_path: Path) -> None:
    # 반환된 Path가 실제로 생성된 파일을 가리키고 메타데이터가 정확한지 확인한다.
    artifact = ArtifactDataset(records=({"id": "1"},))
    target = ExportTarget(kind="jsonl", output_path="out/data.jsonl")

    result = JsonlExporter().export(artifact, target, tmp_path)

    assert result.output_path == tmp_path / "out/data.jsonl"
    assert result.output_path.is_file()
    assert result.file_size == result.output_path.stat().st_size
    assert result.format == "jsonl"
