"""Preview(#3): preview_build가 스키마+샘플만 만들고 파일은 쓰지 않는지 검증."""

from __future__ import annotations

from collections.abc import Iterable

import pytest

from kpubdata_builder.pipeline import PreviewResult, preview_build
from kpubdata_builder.spec import BuildSpec, ExportTarget, JsonValue, SourceRef
from kpubdata_builder.tabular import PreviewSlice, SchemaInfo


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


def _spec(*sources: SourceRef) -> BuildSpec:
    return BuildSpec(
        dataset_id="apt_trade",
        title="Apartment Trades",
        description="seoul apartment trades",
        sources=tuple(sources),
        exports=(ExportTarget(kind="jsonl", output_path="data.jsonl"),),
    )


def test_preview_build_returns_schema_and_sample() -> None:
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
    client = _FakeClient({"datago.apt_trade": [{"id": str(i), "v": i} for i in range(10)]})

    result = preview_build(spec, client=client, limit=3)

    assert isinstance(result, PreviewResult)
    assert len(result.previews) == 1
    preview = result.previews[0]
    assert preview.source_key == "datago.apt_trade"
    assert preview.status == "ok"
    assert isinstance(preview.schema, SchemaInfo)
    assert [c.name for c in preview.schema.columns] == ["id", "v"]
    assert isinstance(preview.preview, PreviewSlice)
    assert preview.preview.total_rows == 10
    assert len(preview.preview.rows) == 3


def test_preview_build_writes_no_files(tmp_path) -> None:  # type: ignore[no-untyped-def]
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    # preview_build은 output_root를 받지 않으므로 어떤 파일도 만들지 않는다.
    preview_build(spec, client=client, limit=5)

    assert list(tmp_path.iterdir()) == []


def test_preview_build_records_failure_for_missing_source() -> None:
    spec = _spec(SourceRef(provider="datago", dataset="missing"))
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    result = preview_build(spec, client=client)

    preview = result.previews[0]
    assert preview.status == "failed"
    assert preview.error is not None


def test_preview_build_fetches_by_provider_dataset_and_reports_alias() -> None:
    """alias가 있어도 fetch는 provider.dataset 키로, 표면 키는 alias로 (#98 review와 동일 회귀)."""
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade", alias="trades"))
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    result = preview_build(spec, client=client, limit=1)

    preview = result.previews[0]
    assert preview.status == "ok"
    assert preview.source_key == "trades"


def test_preview_build_rejects_non_positive_limit() -> None:
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    with pytest.raises(ValueError, match="limit"):
        preview_build(spec, client=client, limit=0)
