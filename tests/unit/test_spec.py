"""Tests for spec models."""

from __future__ import annotations

import datetime

import pytest

from kpubdata_builder.errors import SpecLoadError
from kpubdata_builder.spec import BuildSpec, ExportTarget, SourceRef, parse_spec


def test_build_spec_instantiation() -> None:
    """BuildSpec can be instantiated with minimum valid values."""
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=(SourceRef(provider="datago", dataset="air_quality"),),
        exports=(ExportTarget(kind="jsonl", output_path="out/data.jsonl"),),
    )

    assert spec.dataset_id == "dataset.sample"


def _valid_spec(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "dataset_id": "test.ds",
        "title": "Test",
        "description": "desc",
        "sources": [{"provider": "datago", "dataset": "air_quality"}],
        "exports": [{"kind": "jsonl", "output_path": "out.jsonl"}],
    }
    base.update(overrides)
    return base


def test_parse_spec_accepts_valid_json_params() -> None:
    data = _valid_spec(
        sources=[
            {
                "provider": "datago",
                "dataset": "air_quality",
                "params": {"page": 1, "nested": {"a": [1, 2, True, None]}},
            }
        ]
    )
    spec = parse_spec(data)
    assert spec.sources[0].params["page"] == 1


def test_parse_spec_rejects_datetime_in_params() -> None:
    data = _valid_spec(
        sources=[
            {
                "provider": "datago",
                "dataset": "air_quality",
                "params": {"ts": datetime.datetime(2026, 1, 1)},
            }
        ]
    )
    with pytest.raises(SpecLoadError, match="non-JSON value"):
        parse_spec(data)


def test_parse_spec_rejects_date_in_params() -> None:
    data = _valid_spec(
        sources=[
            {
                "provider": "datago",
                "dataset": "air_quality",
                "params": {"d": datetime.date(2026, 1, 1)},
            }
        ]
    )
    with pytest.raises(SpecLoadError, match="non-JSON value"):
        parse_spec(data)


def test_parse_spec_rejects_non_json_in_export_options() -> None:
    data = _valid_spec(
        exports=[
            {
                "kind": "jsonl",
                "output_path": "out.jsonl",
                "options": {"bad": datetime.date(2026, 1, 1)},
            }
        ]
    )
    with pytest.raises(SpecLoadError, match="non-JSON value"):
        parse_spec(data)
