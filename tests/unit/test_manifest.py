"""빌드 매니페스트 모델과 파일 기록 동작을 검증한다."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from kpubdata_builder import ManifestError
from kpubdata_builder.manifest import BuildManifest, manifest_writer, write_manifest


def test_build_manifest_instantiation() -> None:
    """필수 필드만으로 BuildManifest를 생성할 수 있어야 한다."""
    # 데이터 클래스가 최소 생성 요건을 충족하는지 확인한다.
    started_at = datetime.now(tz=timezone.utc)
    finished_at = datetime.now(tz=timezone.utc)

    manifest = BuildManifest(build_id="build-1", started_at=started_at, finished_at=finished_at)

    assert manifest.build_id == "build-1"


def test_manifest_writer_creates_parent_directories(tmp_path: Path) -> None:
    # 중첩 출력 경로가 존재하지 않아도 부모 디렉터리를 만들어 기록하는지 검증한다.
    manifest = BuildManifest(
        build_id="build-1",
        started_at=datetime.now(tz=timezone.utc),
        finished_at=datetime.now(tz=timezone.utc),
    )
    output_path = tmp_path / "nested" / "build" / "manifest.json"

    manifest_writer(manifest, output_path)

    assert output_path.exists()


def test_manifest_writer_wraps_io_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 디스크 쓰기 실패가 ManifestError로 래핑되는지 확인한다.
    manifest = BuildManifest(
        build_id="build-1",
        started_at=datetime.now(tz=timezone.utc),
        finished_at=datetime.now(tz=timezone.utc),
    )
    output_path = tmp_path / "manifest.json"

    def raise_io_error(self: Path, data: str, *, encoding: str) -> int:
        del self, data, encoding
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", raise_io_error)

    with pytest.raises(ManifestError):
        manifest_writer(manifest, output_path)


def test_manifest_writer_emits_valid_json_with_all_fields(tmp_path: Path) -> None:
    # 기록된 manifest.json이 파싱 가능한 유효 JSON이고 필수 필드를 모두 담는지 검증한다.
    started_at = datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc)
    finished_at = datetime(2026, 5, 26, 1, 0, 5, tzinfo=timezone.utc)
    manifest = BuildManifest(
        build_id="build-1",
        started_at=started_at,
        finished_at=finished_at,
        inputs=("datago.air_quality",),
        outputs=("out/data.jsonl", "out/README.md"),
        warnings=("schema drift",),
        errors=(),
        row_counts={"silver": 2, "gold": 2},
    )
    output_path = tmp_path / "manifest.json"

    manifest_writer(manifest, output_path)

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload == {
        "build_id": "build-1",
        "started_at": "2026-05-26T01:00:00+00:00",
        "finished_at": "2026-05-26T01:00:05+00:00",
        "inputs": ["datago.air_quality"],
        "outputs": ["out/data.jsonl", "out/README.md"],
        "warnings": ["schema drift"],
        "errors": [],
        "row_counts": {"gold": 2, "silver": 2},
    }


def test_manifest_writer_normalizes_timestamps_to_utc(tmp_path: Path) -> None:
    # 비 UTC 타임존 입력이 UTC ISO 문자열로 정규화되는지 확인한다.
    kst = timezone(timedelta(hours=9))
    manifest = BuildManifest(
        build_id="build-1",
        started_at=datetime(2026, 5, 26, 10, 0, 0, tzinfo=kst),
        finished_at=datetime(2026, 5, 26, 10, 0, 0, tzinfo=kst),
    )
    output_path = tmp_path / "manifest.json"

    manifest_writer(manifest, output_path)

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    # KST 10:00 == UTC 01:00.
    assert payload["started_at"] == "2026-05-26T01:00:00+00:00"
    assert payload["finished_at"] == "2026-05-26T01:00:00+00:00"


def test_manifest_writer_output_is_deterministic(tmp_path: Path) -> None:
    # 정렬된 키 + 단일 trailing newline으로 결정적 출력이 보장되는지 고정한다.
    manifest = BuildManifest(
        build_id="build-1",
        started_at=datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc),
        row_counts={"silver": 1, "gold": 1, "bronze": 1},
    )
    output_path = tmp_path / "manifest.json"

    manifest_writer(manifest, output_path)

    raw = output_path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    assert not raw.endswith("\n\n")
    # 최상위 키가 알파벳순으로 직렬화되어야 한다(sort_keys=True).
    top_level_keys = list(json.loads(raw).keys())
    assert top_level_keys == sorted(top_level_keys)


def test_write_manifest_alias_matches_manifest_writer(tmp_path: Path) -> None:
    # 공개 별칭 write_manifest가 manifest_writer와 동일한 바이트 출력을 내는지 확인한다.
    manifest = BuildManifest(
        build_id="build-1",
        started_at=datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc),
        outputs=("out/data.jsonl",),
    )
    via_writer = tmp_path / "writer.json"
    via_alias = tmp_path / "alias.json"

    manifest_writer(manifest, via_writer)
    write_manifest(manifest, via_alias)

    assert via_writer.read_bytes() == via_alias.read_bytes()


def test_manifest_writer_is_byte_identical_on_repeat(tmp_path: Path) -> None:
    # 같은 manifest를 두 번 기록하면 바이트 단위로 동일해야 한다(결정성 직접 검증).
    manifest = BuildManifest(
        build_id="build-1",
        started_at=datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 5, 26, 1, 0, 5, tzinfo=timezone.utc),
        inputs=("b_src", "a_src"),
        outputs=("out/b.jsonl", "out/a.jsonl"),
        row_counts={"b_src": 2, "a_src": 1},
    )
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"

    manifest_writer(manifest, first)
    manifest_writer(manifest, second)

    assert first.read_bytes() == second.read_bytes()


def test_manifest_writer_preserves_non_ascii(tmp_path: Path) -> None:
    # ensure_ascii=False이므로 한글이 \uXXXX로 escape되지 않고 그대로 보존되어야 한다.
    manifest = BuildManifest(
        build_id="build-1",
        started_at=datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 5, 26, 1, 0, 0, tzinfo=timezone.utc),
        inputs=("datago.대기오염정보",),
        warnings=("스키마 변동 감지",),
    )
    output_path = tmp_path / "manifest.json"

    manifest_writer(manifest, output_path)

    raw = output_path.read_text(encoding="utf-8")
    assert "대기오염정보" in raw
    assert "스키마 변동 감지" in raw
    assert "\\u" not in raw
