"""Split 지원(#38): 분할 로직·spec 로딩·검증을 확인한다."""

from __future__ import annotations

import pytest

from kpubdata_builder import ValidationError
from kpubdata_builder.spec import JsonValue, SplitSpec, parse_spec
from kpubdata_builder.spec.validator import validate_spec
from kpubdata_builder.stages.gold import apply_splits


def _records(n: int) -> list[dict[str, JsonValue]]:
    return [{"id": str(i)} for i in range(n)]


def test_ratio_split_allocates_exact_counts_and_partitions() -> None:
    spec = SplitSpec(mode="ratio", ratios={"train": 0.8, "test": 0.2}, seed=0)
    records = _records(10)

    result = apply_splits(records, spec)

    assert len(result["train"]) == 8
    assert len(result["test"]) == 2
    # 분할은 원본을 완전 분할(disjoint + 합집합 = 전체)해야 한다.
    train_ids = {row["id"] for row in result["train"]}
    test_ids = {row["id"] for row in result["test"]}
    assert train_ids.isdisjoint(test_ids)
    assert train_ids | test_ids == {row["id"] for row in records}


def test_ratio_split_is_reproducible_with_seed() -> None:
    spec = SplitSpec(mode="ratio", ratios={"train": 0.5, "test": 0.5}, seed=42)
    records = _records(20)

    assert apply_splits(records, spec) == apply_splits(records, spec)


def test_ratio_split_preserves_original_order_within_split() -> None:
    spec = SplitSpec(mode="ratio", ratios={"a": 0.5, "b": 0.5}, seed=1)
    records = _records(6)

    result = apply_splits(records, spec)

    for rows in result.values():
        ids = [int(row["id"]) for row in rows]  # type: ignore[arg-type]
        assert ids == sorted(ids)


def test_key_split_groups_by_column_value() -> None:
    spec = SplitSpec(mode="key", key="year")
    records: list[dict[str, JsonValue]] = [
        {"id": "1", "year": "2024"},
        {"id": "2", "year": "2025"},
        {"id": "3", "year": "2024"},
    ]

    result = apply_splits(records, spec)

    assert set(result) == {"2024", "2025"}
    assert len(result["2024"]) == 2
    assert len(result["2025"]) == 1


def test_apply_splits_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="split mode"):
        apply_splits(_records(1), SplitSpec(mode="bogus"))


def _base_spec_dict() -> dict[str, object]:
    return {
        "dataset_id": "apt_trade",
        "title": "Apartment Trades",
        "description": "seoul apartment trades",
        "sources": [{"provider": "datago", "dataset": "apt_trade"}],
        "exports": [{"kind": "jsonl", "output_path": "data.jsonl"}],
    }


def test_parse_spec_reads_ratio_splits() -> None:
    data = _base_spec_dict()
    data["splits"] = {"mode": "ratio", "ratios": {"train": 0.8, "test": 0.2}, "seed": 7}

    spec = parse_spec(data)

    assert spec.splits == SplitSpec(
        mode="ratio", ratios={"train": 0.8, "test": 0.2}, key="", seed=7
    )


def test_parse_spec_splits_default_none() -> None:
    assert parse_spec(_base_spec_dict()).splits is None


def test_validate_spec_rejects_ratios_not_summing_to_one() -> None:
    data = _base_spec_dict()
    data["splits"] = {"mode": "ratio", "ratios": {"train": 0.7, "test": 0.2}}

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(parse_spec(data))

    assert any("sum to 1.0" in p for p in exc_info.value.problems)


def test_validate_spec_rejects_key_mode_without_key() -> None:
    data = _base_spec_dict()
    data["splits"] = {"mode": "key"}

    with pytest.raises(ValidationError) as exc_info:
        validate_spec(parse_spec(data))

    assert any("splits.key" in p for p in exc_info.value.problems)


def test_validate_spec_accepts_valid_ratio_splits() -> None:
    data = _base_spec_dict()
    data["splits"] = {"mode": "ratio", "ratios": {"train": 0.8, "test": 0.2}}

    validate_spec(parse_spec(data))  # 예외가 없어야 한다.
