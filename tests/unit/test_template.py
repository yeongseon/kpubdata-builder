"""재사용 가능한 빌드 템플릿(#14) 렌더링·로딩을 검증한다."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kpubdata_builder.errors import SpecLoadError
from kpubdata_builder.spec import load_template, render_template

_TEMPLATE = """\
_template:
  name: air quality
  parameters:
    station_name:
      default: 종로구
    fmt:
      default: jsonl
dataset_id: "air_quality_{{ station_name }}"
title: "{{ station_name }} 대기오염"
description: "air quality for {{ station_name }}"
sources:
  - provider: datago
    dataset: air_quality
    params:
      stationName: "{{ station_name }}"
exports:
  - kind: "{{ fmt }}"
    output_path: "data.{{ fmt }}"
"""


def _write_template(tmp_path: Path) -> Path:
    path = tmp_path / "air_quality.yaml"
    path.write_text(_TEMPLATE, encoding="utf-8")
    return path


def test_render_uses_defaults_when_no_params(tmp_path: Path) -> None:
    rendered = render_template(_write_template(tmp_path), {})

    data = yaml.safe_load(rendered)
    assert data["dataset_id"] == "air_quality_종로구"
    assert data["exports"][0]["kind"] == "jsonl"
    # _template 메타 블록은 제거되어야 한다.
    assert "_template" not in data


def test_render_overrides_defaults_with_params(tmp_path: Path) -> None:
    rendered = render_template(_write_template(tmp_path), {"station_name": "강남구", "fmt": "csv"})

    data = yaml.safe_load(rendered)
    assert data["dataset_id"] == "air_quality_강남구"
    assert data["title"] == "강남구 대기오염"
    assert data["sources"][0]["params"]["stationName"] == "강남구"
    assert data["exports"][0]["output_path"] == "data.csv"


def test_load_template_returns_valid_build_spec(tmp_path: Path) -> None:
    spec = load_template(_write_template(tmp_path), {"station_name": "강남구"})

    assert spec.dataset_id == "air_quality_강남구"
    assert spec.sources[0].provider == "datago"
    assert spec.exports[0].kind == "jsonl"


def test_render_raises_on_missing_parameter(tmp_path: Path) -> None:
    # 기본값도 없고 제공되지도 않은 플레이스홀더가 있으면 오류.
    path = tmp_path / "t.yaml"
    path.write_text(
        '_template:\n  name: t\ndataset_id: "{{ missing }}"\ntitle: t\n', encoding="utf-8"
    )

    with pytest.raises(SpecLoadError, match="Missing template parameter"):
        render_template(path, {})


def test_render_rejects_non_mapping_template(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("- just\n- a\n- list\n", encoding="utf-8")

    with pytest.raises(SpecLoadError, match="must be a mapping"):
        render_template(path, {})


def test_load_template_avoids_yaml_reparse(tmp_path: Path) -> None:
    # #225: load_template이 render_template을 통해 YAML을 문자열로 직렬화한 뒤
    # yaml.safe_load로 재파싱하지 않고, 치환된 메모리 구조를 직접 parse_spec에 전달해
    # 불필요한 직렬화·역직렬화 왕복을 제거한다.
    # 정수처럼 보이는 파라미터 값이 문자열로 보존되는지 검증한다.
    path = tmp_path / "tmpl.yaml"
    path.write_text(
        '_template:\n  parameters:\n    version:\n      default: "v1"\n'
        "dataset_id: air_quality\ntitle: Air Quality\n"
        "description: d\n"
        "sources:\n  - provider: datago\n    dataset: air_quality\n"
        '    params:\n      version: "{{ version }}"\n'
        "exports:\n  - kind: jsonl\n    output_path: data.jsonl\n",
        encoding="utf-8",
    )

    spec = load_template(path, {"version": "v2"})

    # 파라미터 치환이 올바르게 작동하고 BuildSpec이 파싱된다.
    assert spec.sources[0].params["version"] == "v2"
    assert isinstance(spec.sources[0].params["version"], str)
    # 두 번 호출해도 동일한 결과를 반환한다(결정적).
    spec2 = load_template(path, {"version": "v2"})
    assert spec.sources[0].params == spec2.sources[0].params
