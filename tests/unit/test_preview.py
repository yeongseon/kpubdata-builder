"""Preview(#3, #497): preview_build가 스키마+샘플+diff를 만들고 파일은 쓰지 않는지 검증."""

from __future__ import annotations

import builtins
import random as random_module
from collections.abc import Iterable
from pathlib import Path

import pytest

import kpubdata_builder.pipeline.preview as preview_module
from kpubdata_builder.errors import ValidationError
from kpubdata_builder.pipeline import PreviewResult, preview_build
from kpubdata_builder.spec import BuildSpec, ExportTarget, JsonValue, SourceRef
from kpubdata_builder.spec.models import SchemaContract
from kpubdata_builder.stages.silver.build import build_silver_dataset
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


def test_preview_build_writes_no_files(monkeypatch: pytest.MonkeyPatch) -> None:
    # 이전 테스트는 preview_build에 전달되지도 않는 temp 디렉터리가 비었음을 확인해
    # 사실상 항상 통과했다(#196). 실제 파일시스템 쓰기 경로를 가로채 preview_build가
    # 어떤 쓰기도 하지 않음을 보장한다.
    spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    real_open = builtins.open

    def _guard_open(file, mode="r", *args, **kwargs):  # type: ignore[no-untyped-def]
        if any(flag in mode for flag in ("w", "a", "x", "+")):
            raise AssertionError(f"unexpected file write during preview: {file!r} (mode={mode})")
        return real_open(file, mode, *args, **kwargs)

    def _boom(self: Path, *args: object, **kwargs: object) -> object:
        raise AssertionError(f"unexpected filesystem write during preview: {self!r}")

    monkeypatch.setattr(builtins, "open", _guard_open)
    monkeypatch.setattr(Path, "write_text", _boom)
    monkeypatch.setattr(Path, "write_bytes", _boom)
    monkeypatch.setattr(Path, "mkdir", _boom)

    # 쓰기 경로가 호출되면 AssertionError로 실패한다.
    result = preview_build(spec, client=client, limit=5)

    assert isinstance(result, PreviewResult)


def test_preview_build_validates_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    # 유효하지 않은 spec은 부분 실행/빈 결과 대신 빠르게 실패해야 한다 (#193).
    spec = BuildSpec(
        dataset_id="apt_trade",
        title="Apartment Trades",
        description="seoul apartment trades",
        sources=(SourceRef(provider="datago", dataset="apt_trade"),),
        exports=(ExportTarget(kind="unsupported_kind", output_path="data.x"),),
    )
    client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

    with pytest.raises(ValidationError):
        preview_build(spec, client=client, limit=5)


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


# ---------------------------------------------------------------------------
# Sampling (#497)
# ---------------------------------------------------------------------------


class TestSampling:
    def test_default_sample_mode_is_first(self) -> None:
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        client = _FakeClient({"datago.apt_trade": [{"id": str(i)} for i in range(10)]})

        result = preview_build(spec, client=client, limit=3)

        preview = result.previews[0]
        assert preview.sample_mode == "first"
        assert [row["id"] for row in preview.preview.rows] == ["0", "1", "2"]
        assert [row["id"] for row in preview.source_sample] == ["0", "1", "2"]

    def test_explicit_first_matches_default(self) -> None:
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        client = _FakeClient({"datago.apt_trade": [{"id": str(i)} for i in range(10)]})

        default_result = preview_build(spec, client=client, limit=3)
        explicit_result = preview_build(spec, client=client, limit=3, sample_mode="first")

        assert default_result.previews[0].preview.rows == explicit_result.previews[0].preview.rows
        assert default_result.previews[0].source_sample == explicit_result.previews[0].source_sample

    def test_random_mode_is_reproducible_with_same_seed(self) -> None:
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        client = _FakeClient({"datago.apt_trade": [{"id": str(i)} for i in range(200)]})

        first = preview_build(spec, client=client, limit=5, sample_mode="random", seed=42)
        second = preview_build(spec, client=client, limit=5, sample_mode="random", seed=42)

        assert first.previews[0].source_sample == second.previews[0].source_sample
        assert first.previews[0].preview.rows == second.previews[0].preview.rows

    def test_random_mode_differs_with_different_seed(self) -> None:
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        client = _FakeClient({"datago.apt_trade": [{"id": str(i)} for i in range(200)]})

        seed_a = preview_build(spec, client=client, limit=5, sample_mode="random", seed=1)
        seed_b = preview_build(spec, client=client, limit=5, sample_mode="random", seed=2)

        assert seed_a.previews[0].source_sample != seed_b.previews[0].source_sample

    def test_random_mode_matches_select_indices_algorithm(self) -> None:
        # 구현이 실제로 random.Random(seed).sample(range(n), k)에 위임하는지,
        # 전역 random 상태가 아니라 seed에만 의존하는지 화이트박스로 고정한다.
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        records = [{"id": str(i)} for i in range(50)]
        client = _FakeClient({"datago.apt_trade": records})

        result = preview_build(spec, client=client, limit=6, sample_mode="random", seed=7)

        expected_indices = sorted(random_module.Random(7).sample(range(50), 6))
        expected_ids = [records[i]["id"] for i in expected_indices]
        assert [row["id"] for row in result.previews[0].source_sample] == expected_ids

    def test_random_mode_does_not_disturb_global_random_state(self) -> None:
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        client = _FakeClient({"datago.apt_trade": [{"id": str(i)} for i in range(50)]})

        random_module.seed(1234)
        before = random_module.random()
        random_module.seed(1234)
        preview_build(spec, client=client, limit=5, sample_mode="random", seed=99)
        after = random_module.random()

        assert before == after

    def test_rejects_invalid_sample_mode(self) -> None:
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

        with pytest.raises(ValueError, match="sample_mode"):
            preview_build(spec, client=client, sample_mode="shuffle")  # type: ignore[arg-type]

    def test_rejects_non_int_seed(self) -> None:
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

        with pytest.raises(TypeError, match="seed"):
            preview_build(spec, client=client, sample_mode="random", seed="7")  # type: ignore[arg-type]

    def test_rejects_bool_seed(self) -> None:
        # bool은 int의 하위 타입이지만 seed 의미가 없다.
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

        with pytest.raises(TypeError, match="seed"):
            preview_build(spec, client=client, sample_mode="random", seed=True)

    def test_random_sample_bounded_by_total_rows_and_limit(self) -> None:
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        client = _FakeClient({"datago.apt_trade": [{"id": str(i)} for i in range(3)]})

        result = preview_build(spec, client=client, limit=10, sample_mode="random", seed=0)

        # total_rows(3)보다 큰 limit(10)을 요청해도 실제 존재하는 행만 반환한다.
        assert len(result.previews[0].source_sample) == 3
        assert len(result.previews[0].preview.rows) == 3


# ---------------------------------------------------------------------------
# Diff (#497)
# ---------------------------------------------------------------------------


class TestDiff:
    def test_no_change_yields_empty_diffs(self) -> None:
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        client = _FakeClient(
            {"datago.apt_trade": [{"id": "1", "label": "a"}, {"id": "2", "label": "b"}]}
        )

        result = preview_build(spec, client=client, limit=5)

        preview = result.previews[0]
        assert preview.diff_available is True
        assert preview.diffs == ()
        assert preview.transform_summary is not None
        assert preview.transform_summary.changed_cells == 0
        assert preview.transform_summary.changed_rows == 0
        assert preview.diff_truncated is False

    def test_declared_cast_produces_diff_with_transform_label(self) -> None:
        source = SourceRef(
            provider="datago",
            dataset="apt_trade",
            schema=SchemaContract(casts={"amount": "int"}),
        )
        spec = _spec(source)
        client = _FakeClient(
            {"datago.apt_trade": [{"id": "1", "amount": "128000"}, {"id": "2", "amount": "50"}]}
        )

        result = preview_build(spec, client=client, limit=5)

        preview = result.previews[0]
        assert preview.diff_available is True
        assert len(preview.diffs) == 2
        first = preview.diffs[0]
        assert first.row == 0
        assert first.column == "amount"
        assert first.before == "128000"
        assert first.after == 128000
        assert first.transform == "cast:int"
        assert preview.transform_summary is not None
        assert preview.transform_summary.changed_cells == 2
        assert preview.transform_summary.changed_rows == 2
        assert preview.diff_truncated is False

    def test_wide_dataset_truncates_diffs_but_keeps_accurate_summary_end_to_end(self) -> None:
        # #497 sample/diff memory 상한: limit(행 수)만으로는 wide dataset(컬럼 수多)의
        # diff item 개수를 막지 못한다 — MAX_PREVIEW_DIFF_ITEMS가 실제로 응답에
        # 담기는 PreviewDiffItem 개수를 자르고, diff_truncated로 명시하는지 end-to-end로
        # 확인한다. 1행 × (MAX_PREVIEW_DIFF_ITEMS + 100)컬럼 모두 cast로 변경한다.
        max_items = preview_module.MAX_PREVIEW_DIFF_ITEMS
        column_count = max_items + 100
        columns = [f"c{i}" for i in range(column_count)]
        casts = dict.fromkeys(columns, "int")
        source = SourceRef(
            provider="datago",
            dataset="apt_trade",
            schema=SchemaContract(casts=casts),
        )
        spec = _spec(source)
        row = {c: "1" for c in columns}
        client = _FakeClient({"datago.apt_trade": [row]})

        result = preview_build(spec, client=client, limit=1)

        preview = result.previews[0]
        assert preview.status == "ok"
        assert preview.diff_available is True
        assert len(preview.diffs) == max_items
        assert preview.diff_truncated is True
        assert preview.transform_summary is not None
        assert preview.transform_summary.changed_cells == column_count
        assert preview.transform_summary.changed_rows == 1

    def test_columns_without_declared_cast_never_produce_a_diff_end_to_end(self) -> None:
        # 현재 normalize_table()은 casts에 없는 컬럼의 값을 절대 바꾸지 않으므로
        # (records_to_dataframe()이 원본 값을 그대로 옮긴다), casts에 없는 컬럼은
        # end-to-end로 diff 자체가 생기지 않는다 — "transform=None" 분기는
        # _diff_sample() 자체를 직접 검증한다(아래 TestDiffSampleHelper).
        source = SourceRef(
            provider="datago",
            dataset="apt_trade",
            schema=SchemaContract(casts={"amount": "int"}),
        )
        spec = _spec(source)
        client = _FakeClient({"datago.apt_trade": [{"id": "1", "amount": "1", "label": "x"}]})

        result = preview_build(spec, client=client, limit=5)

        diffs_by_column = {d.column: d for d in result.previews[0].diffs}
        assert diffs_by_column["amount"].transform == "cast:int"
        assert "label" not in diffs_by_column
        assert "id" not in diffs_by_column


class TestDiffSampleHelper:
    """``_diff_sample``을 직접 검증한다 — casts에 없는 컬럼이 바뀌는 상황은 현재
    Silver 파이프라인(값이 바뀌려면 반드시 declared cast를 거친다)에서는 end-to-end로
    재현할 수 없으므로, transform=None 분기는 이 pure 함수 레벨에서 고정한다.
    """

    def test_transform_is_null_when_column_has_no_declared_cast(self) -> None:
        diffs, summary, truncated = preview_module._diff_sample(
            [{"note": "before"}],
            [{"note": "after"}],
            columns=("note",),
            casts=None,
            max_items=preview_module.MAX_PREVIEW_DIFF_ITEMS,
        )

        assert len(diffs) == 1
        assert diffs[0].transform is None
        assert summary.changed_cells == 1
        assert summary.changed_rows == 1
        assert truncated is False

    def test_transform_is_null_when_column_not_in_casts_mapping(self) -> None:
        diffs, _summary, _truncated = preview_module._diff_sample(
            [{"note": "before", "amount": "1"}],
            [{"note": "after", "amount": 1}],
            columns=("note", "amount"),
            casts={"amount": "int"},
            max_items=preview_module.MAX_PREVIEW_DIFF_ITEMS,
        )

        by_column = {d.column: d for d in diffs}
        assert by_column["note"].transform is None
        assert by_column["amount"].transform == "cast:int"

    def test_no_change_across_all_rows_yields_zero_summary(self) -> None:
        diffs, summary, truncated = preview_module._diff_sample(
            [{"a": 1}, {"a": 2}],
            [{"a": 1}, {"a": 2}],
            columns=("a",),
            casts=None,
            max_items=preview_module.MAX_PREVIEW_DIFF_ITEMS,
        )

        assert diffs == ()
        assert summary.changed_cells == 0
        assert summary.changed_rows == 0
        assert truncated is False

    def test_max_items_caps_materialized_diffs_but_keeps_accurate_summary(self) -> None:
        # #497 sample/diff memory 상한: limit은 행 수만 제한하므로 wide dataset에서
        # diffs 리스트 자체가 무제한 커질 수 있다 — max_items로 리스트만 자르고,
        # changed_cells/changed_rows는 잘리지 않은 실제 합계를 유지해야 한다.
        columns = tuple(f"c{i}" for i in range(10))
        source_rows = [dict.fromkeys(columns, "before")]
        transformed_rows = [dict.fromkeys(columns, "after")]

        diffs, summary, truncated = preview_module._diff_sample(
            source_rows,
            transformed_rows,
            columns=columns,
            casts=None,
            max_items=4,
        )

        assert len(diffs) == 4
        assert truncated is True
        assert summary.changed_cells == 10  # 잘렸어도 실제 변경 셀 수는 정확하다.
        assert summary.changed_rows == 1

    def test_max_items_not_exceeded_leaves_truncated_false(self) -> None:
        columns = ("a", "b")
        diffs, summary, truncated = preview_module._diff_sample(
            [{"a": "1", "b": "2"}],
            [{"a": 1, "b": 2}],
            columns=columns,
            casts=None,
            max_items=2,
        )

        assert len(diffs) == 2
        assert truncated is False
        assert summary.changed_cells == 2

    def test_multiple_changed_cells_across_rows(self) -> None:
        source = SourceRef(
            provider="datago",
            dataset="apt_trade",
            schema=SchemaContract(casts={"amount": "int", "active": "bool"}),
        )
        spec = _spec(source)
        client = _FakeClient(
            {
                "datago.apt_trade": [
                    {"id": "1", "amount": "100", "active": "true"},
                    {"id": "2", "amount": "200", "active": "false"},
                    {"id": "3", "amount": "3", "active": "yes"},
                ]
            }
        )

        result = preview_build(spec, client=client, limit=5)

        preview = result.previews[0]
        assert preview.diff_available is True
        # 3행 x 2컬럼(amount, active) 모두 문자열 -> 캐스팅된 값으로 바뀐다.
        assert preview.transform_summary is not None
        assert preview.transform_summary.changed_cells == 6
        assert preview.transform_summary.changed_rows == 3
        assert {d.column for d in preview.diffs} == {"amount", "active"}

    def test_diff_row_index_is_position_within_sample_not_absolute_row(self) -> None:
        source = SourceRef(
            provider="datago",
            dataset="apt_trade",
            schema=SchemaContract(casts={"amount": "int"}),
        )
        spec = _spec(source)
        # limit=2이므로 sample에는 0/1번 행만 담기고, diff의 row는 그 배열 내 위치다.
        client = _FakeClient(
            {
                "datago.apt_trade": [
                    {"id": "1", "amount": "10"},
                    {"id": "2", "amount": "20"},
                    {"id": "3", "amount": "30"},
                ]
            }
        )

        result = preview_build(spec, client=client, limit=2)

        rows = {d.row for d in result.previews[0].diffs}
        assert rows == {0, 1}

    def test_diff_unavailable_when_cast_introduces_nulls(self) -> None:
        # #188 data-loss guard: 캐스팅이 값을 null로 떨어뜨리면 전체 preview가
        # 실패 처리되며(#188), diff가 절반만 성공한 것처럼 보이지 않는다 — 이것이
        # 이 코드베이스에서 "null 변화"가 diff item으로 새는 것을 막는 실제 안전장치다.
        source = SourceRef(
            provider="datago",
            dataset="apt_trade",
            schema=SchemaContract(casts={"amount": "int"}),
        )
        spec = _spec(source)
        client = _FakeClient(
            {"datago.apt_trade": [{"id": "1", "amount": "100"}, {"id": "2", "amount": "oops"}]}
        )

        result = preview_build(spec, client=client, limit=5)

        preview = result.previews[0]
        assert preview.status == "failed"
        assert preview.diff_available is False
        assert preview.diffs == ()
        assert preview.transform_summary is None
        assert preview.source_sample == ()
        assert preview.diff_truncated is False

    def test_diff_unavailable_on_source_fetch_failure(self) -> None:
        spec = _spec(SourceRef(provider="datago", dataset="missing"))
        client = _FakeClient({"datago.apt_trade": [{"id": "1"}]})

        result = preview_build(spec, client=client)

        preview = result.previews[0]
        assert preview.status == "failed"
        assert preview.diff_available is False
        assert preview.diffs == ()
        assert preview.transform_summary is None
        assert preview.source_sample == ()
        assert preview.diff_truncated is False

    def test_diff_unavailable_when_row_count_is_not_preserved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 오늘의 Silver 경로는 행을 filter/reorder하지 않지만(test_silver.py의
        # TestRowPreservingInvariant), 그 전제가 깨진 가상의 미래를 시뮬레이션해
        # count guard가 fail-closed로 동작하는지 검증한다.
        real_build_silver_dataset = build_silver_dataset

        def _dropping_build_silver_dataset(bronze, **kwargs):  # type: ignore[no-untyped-def]
            silver = real_build_silver_dataset(bronze, **kwargs)
            dropped_table = silver.table.head(silver.table.height - 1)
            return type(silver)(
                table=dropped_table,
                schema=silver.schema,
                statistics=type(silver.statistics)(
                    row_count=dropped_table.height,
                    null_counts=silver.statistics.null_counts,
                    duplicate_rate=silver.statistics.duplicate_rate,
                ),
                preview=silver.preview,
                validation=silver.validation,
                source_bronze=silver.source_bronze,
            )

        monkeypatch.setattr(preview_module, "build_silver_dataset", _dropping_build_silver_dataset)
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        client = _FakeClient({"datago.apt_trade": [{"id": str(i)} for i in range(5)]})

        result = preview_build(spec, client=client, limit=5)

        preview = result.previews[0]
        assert preview.status == "ok"  # 실패가 아니라 diff만 안전하게 무효화된다.
        assert preview.diff_available is False
        assert preview.diffs == ()
        assert preview.transform_summary is None
        assert preview.source_sample == ()
        assert preview.diff_truncated is False
        # sample(transformed) 자체는 여전히 채워진다 — diff만 신뢰할 수 없을 뿐.
        assert len(preview.preview.rows) == 4


# ---------------------------------------------------------------------------
# Regression (#497) — 기존 필드/동작이 보존되는지.
# ---------------------------------------------------------------------------


class TestRegression:
    def test_existing_fields_unaffected_by_new_sample_mode_param(self) -> None:
        spec = _spec(SourceRef(provider="datago", dataset="apt_trade"))
        client = _FakeClient({"datago.apt_trade": [{"id": str(i), "v": i} for i in range(10)]})

        result = preview_build(spec, client=client, limit=3)

        preview = result.previews[0]
        assert preview.source_key == "datago.apt_trade"
        assert preview.status == "ok"
        assert [c.name for c in preview.schema.columns] == ["id", "v"]
        assert preview.preview.total_rows == 10
        assert len(preview.preview.rows) == 3
        assert preview.statistics.row_count == 10
