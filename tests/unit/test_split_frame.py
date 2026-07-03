"""Split 지원(#260): Polars-native 분할 로직 확인."""

from __future__ import annotations

import polars as pl
import pytest

from kpubdata_builder.spec import SplitSpec
from kpubdata_builder.stages.gold import apply_splits_to_frame


def test_apply_splits_to_frame_ratio_split() -> None:
    spec = SplitSpec(mode="ratio", ratios={"train": 0.8, "test": 0.2}, seed=0)
    frame = pl.DataFrame({"id": [str(i) for i in range(10)], "value": range(10)})

    result = apply_splits_to_frame(frame, spec)

    assert len(result["train"]) == 8
    assert len(result["test"]) == 2
    # 분할은 원본을 완전 분할(disjoint + 합집합 = 전체)해야 한다.
    train_ids = set(result["train"]["id"])
    test_ids = set(result["test"]["id"])
    assert train_ids.isdisjoint(test_ids)
    all_ids = set(frame["id"])
    assert train_ids | test_ids == all_ids


def test_apply_splits_to_frame_is_reproducible() -> None:
    spec = SplitSpec(mode="ratio", ratios={"train": 0.5, "test": 0.5}, seed=42)
    frame = pl.DataFrame({"id": [str(i) for i in range(20)]})

    result1 = apply_splits_to_frame(frame, spec)
    result2 = apply_splits_to_frame(frame, spec)

    for name in result1:
        assert result1[name].to_dicts() == result2[name].to_dicts()


def test_apply_splits_to_frame_preserves_order() -> None:
    spec = SplitSpec(mode="ratio", ratios={"a": 0.5, "b": 0.5}, seed=1)
    frame = pl.DataFrame({"id": list(range(6))})

    result = apply_splits_to_frame(frame, spec)

    for split_df in result.values():
        ids = split_df["id"].to_list()
        assert ids == sorted(ids)


def test_apply_splits_to_frame_key_split() -> None:
    spec = SplitSpec(mode="key", key="year")
    frame = pl.DataFrame({"id": ["1", "2", "3"], "year": ["2024", "2025", "2024"]})

    result = apply_splits_to_frame(frame, spec)

    assert set(result.keys()) == {"2024", "2025"}
    assert len(result["2024"]) == 2
    assert len(result["2025"]) == 1


def test_apply_splits_to_frame_key_missing() -> None:
    spec = SplitSpec(mode="key", key="year")
    frame = pl.DataFrame({"id": ["1", "2"], "value": [100, 200]})

    result = apply_splits_to_frame(frame, spec)

    assert set(result.keys()) == {"__missing__"}
    assert len(result["__missing__"]) == 2


def test_apply_splits_to_frame_key_with_null() -> None:
    spec = SplitSpec(mode="key", key="cat")
    frame = pl.DataFrame({"id": ["1", "2", "3"], "cat": ["A", None, "B"]})

    result = apply_splits_to_frame(frame, spec)

    assert "__null__" in result
    assert len(result["__null__"]) == 1
    assert result["__null__"]["id"][0] == "2"


def test_apply_splits_to_frame_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="split mode"):
        frame = pl.DataFrame({"id": ["1"]})
        apply_splits_to_frame(frame, SplitSpec(mode="bogus"))
