"""Plugin exporter API(#13): 코드 내 등록과 entry point 발견을 검증한다."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import kpubdata_builder.exporters.registry as registry
from kpubdata_builder import ArtifactDataset
from kpubdata_builder.exporters import (
    EXPORTER_REGISTRY,
    BaseExporter,
    ExportResult,
    get_exporter,
    load_entry_point_exporters,
    register_exporter,
)
from kpubdata_builder.spec import ExportTarget


class _FakeExporter(BaseExporter):
    @property
    def name(self) -> str:
        return "fake"

    def export(
        self, artifact: ArtifactDataset, target: ExportTarget, output_dir: Path
    ) -> ExportResult:
        destination = output_dir / target.output_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        _ = destination.write_text("fake", encoding="utf-8")
        return ExportResult(
            output_path=destination, file_size=destination.stat().st_size, format=self.name
        )


class _FakeExporter2(_FakeExporter):
    @property
    def name(self) -> str:
        return "fake2"


class _FakeEntryPoint:
    def __init__(self, name: str, value: object) -> None:
        self.name = name
        self._value = value

    def load(self) -> object:
        return self._value


@pytest.fixture(autouse=True)
def _restore_registry() -> Iterator[None]:
    # 전역 레지스트리를 스냅샷 후 복원해 테스트 간 오염을 막는다.
    snapshot = dict(EXPORTER_REGISTRY)
    try:
        yield
    finally:
        EXPORTER_REGISTRY.clear()
        EXPORTER_REGISTRY.update(snapshot)


def test_builtin_exporters_are_registered() -> None:
    assert set(EXPORTER_REGISTRY) >= {"jsonl", "markdown"}


def test_register_and_get_exporter() -> None:
    register_exporter(_FakeExporter())

    assert isinstance(get_exporter("fake"), _FakeExporter)


def test_register_duplicate_without_override_raises() -> None:
    register_exporter(_FakeExporter())

    with pytest.raises(ValueError, match="already registered"):
        register_exporter(_FakeExporter())


def test_register_duplicate_with_override_replaces() -> None:
    first = _FakeExporter()
    second = _FakeExporter()
    register_exporter(first)

    register_exporter(second, override=True)

    assert get_exporter("fake") is second


def test_get_exporter_unknown_raises() -> None:
    with pytest.raises(KeyError, match="unknown exporter kind"):
        get_exporter("does-not-exist")


def test_load_entry_point_exporters_discovers_class_and_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 클래스(인스턴스화 필요)와 인스턴스 둘 다 entry point에서 발견·등록되는지 확인한다.
    eps = [
        _FakeEntryPoint("by-class", _FakeExporter),
        _FakeEntryPoint("by-instance", _FakeExporter2()),
    ]

    def fake_entry_points(*, group: str) -> list[_FakeEntryPoint]:
        assert group == registry.EXPORTER_ENTRY_POINT_GROUP
        return eps

    monkeypatch.setattr(registry, "entry_points", fake_entry_points)

    loaded = load_entry_point_exporters(override=True)

    assert loaded == ["fake", "fake2"]
    assert isinstance(get_exporter("fake"), _FakeExporter)
    assert isinstance(get_exporter("fake2"), _FakeExporter2)


def test_load_entry_point_exporters_rejects_non_exporter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_entry_points(*, group: str) -> list[_FakeEntryPoint]:
        del group
        return [_FakeEntryPoint("bad", object())]

    monkeypatch.setattr(registry, "entry_points", fake_entry_points)

    with pytest.raises(TypeError, match="did not resolve to a BaseExporter"):
        load_entry_point_exporters()
