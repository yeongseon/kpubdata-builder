from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder.errors import SpecLoadError
from kpubdata_builder.spec import (
    BuildSpec,
    ExportTarget,
    SourceRef,
    load_spec,
    parse_spec,
)


def _valid_payload() -> dict[str, object]:
    return {
        "dataset_id": "dataset.sample",
        "title": "Sample Dataset",
        "description": "Sample description",
        "sources": [{"provider": "datago", "dataset": "air_quality"}],
        "exports": [{"kind": "jsonl", "output_path": "out/data.jsonl"}],
    }


def test_build_spec_instantiation() -> None:
    """최소 유효값만으로 BuildSpec을 생성할 수 있어야 한다."""
    # 데이터 클래스 기본 생성 경로가 깨지지 않았는지 확인한다.
    spec = BuildSpec(
        dataset_id="dataset.sample",
        title="Sample Dataset",
        description="Sample description",
        sources=(SourceRef(provider="datago", dataset="air_quality"),),
        exports=(ExportTarget(kind="jsonl", output_path="out/data.jsonl"),),
    )

    assert spec.dataset_id == "dataset.sample"
    assert spec.transforms == ()
    assert spec.metadata == {}
    assert spec.publish is False


def test_parse_spec_converts_nested_to_typed_objects() -> None:
    """Top-level lists become tuples of SourceRef / ExportTarget."""
    spec = parse_spec(_valid_payload())

    assert isinstance(spec, BuildSpec)
    assert isinstance(spec.sources, tuple)
    assert isinstance(spec.exports, tuple)
    assert isinstance(spec.sources[0], SourceRef)
    assert isinstance(spec.exports[0], ExportTarget)
    assert spec.sources[0].provider == "datago"
    assert spec.sources[0].dataset == "air_quality"
    assert spec.exports[0].kind == "jsonl"
    assert spec.exports[0].output_path == "out/data.jsonl"


def test_parse_spec_applies_optional_defaults() -> None:
    """transforms/metadata/publish default when omitted from YAML."""
    spec = parse_spec(_valid_payload())

    assert spec.transforms == ()
    assert spec.metadata == {}
    assert spec.publish is False
    assert spec.sources[0].params == {}
    assert spec.sources[0].normalization_mode == "canonical"
    assert spec.sources[0].alias == ""
    assert spec.exports[0].options == {}


def test_parse_spec_preserves_provided_optionals() -> None:
    """Provided optional fields are carried through unchanged."""
    payload = _valid_payload()
    payload["transforms"] = ["normalize", "dedupe"]
    payload["metadata"] = {"owner": "kpubdata"}
    payload["publish"] = True
    sources = payload["sources"]
    assert isinstance(sources, list)
    sources[0]["params"] = {"year": 2024}
    sources[0]["normalization_mode"] = "raw"
    sources[0]["alias"] = "aq"
    exports = payload["exports"]
    assert isinstance(exports, list)
    exports[0]["options"] = {"compression": "gzip"}

    spec = parse_spec(payload)

    assert spec.transforms == ("normalize", "dedupe")
    assert spec.metadata == {"owner": "kpubdata"}
    assert spec.publish is True
    assert spec.sources[0].params == {"year": 2024}
    assert spec.sources[0].normalization_mode == "raw"
    assert spec.sources[0].alias == "aq"
    assert spec.exports[0].options == {"compression": "gzip"}


@pytest.mark.parametrize("missing", ["dataset_id", "title", "description"])
def test_parse_spec_rejects_missing_required_top_level_field(missing: str) -> None:
    """Missing dataset_id/title/description raises with the field name in the message."""
    payload = _valid_payload()
    del payload[missing]

    with pytest.raises(SpecLoadError, match=missing):
        parse_spec(payload)


@pytest.mark.parametrize("empty_field", ["dataset_id", "title", "description"])
def test_parse_spec_rejects_empty_required_top_level_field(empty_field: str) -> None:
    """Empty strings are rejected for required identifying fields."""
    payload = _valid_payload()
    payload[empty_field] = ""

    with pytest.raises(SpecLoadError, match=empty_field):
        parse_spec(payload)


def test_parse_spec_rejects_missing_sources() -> None:
    payload = _valid_payload()
    del payload["sources"]

    with pytest.raises(SpecLoadError, match="sources"):
        parse_spec(payload)


def test_parse_spec_rejects_empty_sources() -> None:
    payload = _valid_payload()
    payload["sources"] = []

    with pytest.raises(SpecLoadError, match="sources"):
        parse_spec(payload)


def test_parse_spec_rejects_missing_exports() -> None:
    payload = _valid_payload()
    del payload["exports"]

    with pytest.raises(SpecLoadError, match="exports"):
        parse_spec(payload)


def test_parse_spec_rejects_empty_exports() -> None:
    payload = _valid_payload()
    payload["exports"] = []

    with pytest.raises(SpecLoadError, match="exports"):
        parse_spec(payload)


@pytest.mark.parametrize("missing", ["provider", "dataset"])
def test_parse_spec_rejects_source_missing_required_field(missing: str) -> None:
    """Each source must declare provider and dataset; message includes index + field."""
    payload = _valid_payload()
    sources = payload["sources"]
    assert isinstance(sources, list)
    del sources[0][missing]

    with pytest.raises(SpecLoadError, match=rf"sources\[0\]\.{missing}"):
        parse_spec(payload)


@pytest.mark.parametrize("missing", ["kind", "output_path"])
def test_parse_spec_rejects_export_missing_required_field(missing: str) -> None:
    """Each export must declare kind and output_path; message includes index + field."""
    payload = _valid_payload()
    exports = payload["exports"]
    assert isinstance(exports, list)
    del exports[0][missing]

    with pytest.raises(SpecLoadError, match=rf"exports\[0\]\.{missing}"):
        parse_spec(payload)


def test_load_spec_round_trips_from_yaml(tmp_path: Path) -> None:
    """A full YAML document parses into the expected BuildSpec."""
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        """
dataset_id: dataset.sample
title: Sample Dataset
description: Sample description
transforms:
  - normalize
metadata:
  owner: kpubdata
publish: true
sources:
  - provider: datago
    dataset: air_quality
    params:
      year: 2024
exports:
  - kind: jsonl
    output_path: out/data.jsonl
    options:
      compression: gzip
""".strip()
        + "\n",
        encoding="utf-8",
    )

    spec = load_spec(spec_path)

    assert spec.dataset_id == "dataset.sample"
    assert spec.transforms == ("normalize",)
    assert spec.metadata == {"owner": "kpubdata"}
    assert spec.publish is True
    assert spec.sources[0].params == {"year": 2024}
    assert spec.exports[0].options == {"compression": "gzip"}


def test_load_spec_rejects_empty_yaml(tmp_path: Path) -> None:
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text("", encoding="utf-8")

    with pytest.raises(SpecLoadError):
        load_spec(spec_path)


def test_build_spec_from_yaml_classmethod(tmp_path: Path) -> None:
    """BuildSpec.from_yaml mirrors load_spec for ergonomic call sites."""
    spec_path = tmp_path / "spec.yaml"
    spec_path.write_text(
        """
dataset_id: dataset.sample
title: Sample Dataset
description: Sample description
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: out/data.jsonl
""".strip()
        + "\n",
        encoding="utf-8",
    )

    spec = BuildSpec.from_yaml(spec_path)

    assert spec.dataset_id == "dataset.sample"
    assert spec.sources[0].provider == "datago"
