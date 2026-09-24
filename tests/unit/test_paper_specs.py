"""논문 실험 3개 dataset 이 BuildSpec 만으로 선언된다 (#611, #613, #636).

두 이슈가 막고 있던 것이 이것이다 — 표현할 수 있는가. 선언 파일이 실제로
로더와 validator 를 통과하는지 여기서 고정한다. 문서에 "이제 가능하다" 라고
적는 것과, 그 선언이 계속 유효한지 확인하는 것은 다르다.

실제 수집과 기존 HF 산출물 대조는 #636 이 잇는다 — 공공 API 1,500회 호출과
API 키가 필요하므로 단위 테스트의 일이 아니다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from kpubdata_builder.spec import (
    compute_spec_digest,
    expand_param_grid,
    parse_spec,
    serialize_spec_bytes,
)
from kpubdata_builder.spec.validator import validate_spec

_SPECS = Path(__file__).parents[2] / "specs"

_PAPER_SPECS = [
    "seoul-apartment-trades.yaml",
    "seoul-apartment-rent.yaml",
    "seoul-bike-rent-month.yaml",
]


def _load(name: str):  # noqa: ANN202 - BuildSpec
    return parse_spec(yaml.safe_load((_SPECS / name).read_text(encoding="utf-8")))


@pytest.mark.parametrize("name", _PAPER_SPECS)
def test_the_spec_parses_and_validates(name: str) -> None:
    validate_spec(_load(name))


@pytest.mark.parametrize("name", _PAPER_SPECS)
def test_the_spec_round_trips_through_the_snapshot(name: str) -> None:
    # recipe 가 스냅샷으로 나갔다가 그대로 돌아와야 R1 이 비교할 것이 생긴다.
    spec = _load(name)

    assert parse_spec(yaml.safe_load(serialize_spec_bytes(spec).decode("utf-8"))) == spec


@pytest.mark.parametrize(
    ("name", "expected"),
    [("seoul-apartment-trades.yaml", 1500), ("seoul-apartment-rent.yaml", 1500)],
)
def test_the_real_estate_grids_expand_to_the_script_s_call_count(name: str, expected: int) -> None:
    """25 자치구 × 60 개월. 생성된 1,500줄 목록이 spec 두 줄이 된 것이 요점이다."""
    source = _load(name).sources[0]

    assert len(expand_param_grid(dict(source.params), dict(source.param_grid))) == expected


def test_the_bike_spec_needs_no_grid() -> None:
    # 따릉이는 공공 API 가 아니라 배포 파일로 온다 — 반복 호출이 아니라 업로드
    # 스냅샷 하나다. 세대 차이는 coalesce/zfill/null_tokens 가 흡수한다.
    source = _load("seoul-bike-rent-month.yaml").sources[0]

    assert source.kind == "file"
    assert not source.param_grid
    assert set(source.schema.coalesce) == {"ym_raw", "distance_m", "duration_min"}


def test_each_spec_has_its_own_recipe_identity() -> None:
    digests = {
        name: compute_spec_digest(serialize_spec_bytes(_load(name))) for name in _PAPER_SPECS
    }

    assert len(set(digests.values())) == len(_PAPER_SPECS)
