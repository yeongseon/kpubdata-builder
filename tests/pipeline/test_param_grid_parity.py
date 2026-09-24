"""BuildSpec 의 ``param_grid`` 와 배포 스크립트 경로의 대조 (#613).

`scripts/generate_fetch_params.py` 가 만드는 1,500개 조합 목록을 spec 두 줄로
대체하는 것이 이 기능의 목적이다. 그러려면 **같은 조합 집합** 을 호출한다는 것이
확인되어야 한다.

순서는 다르다. 그리고 그 차이는 의도된 것이다 — 아래 테스트가 이유까지 고정한다.
"""

from __future__ import annotations

from typing import Any

from kpubdata_builder.spec import expand_param_grid

_DISTRICTS = ["11110", "11140", "11170"]
_MONTHS = ["202001", "202002"]


def _script_order(districts: list[str], months: list[str]) -> list[dict[str, str]]:
    """`scripts/generate_fetch_params.py:generate_params` 와 같은 중첩 순서.

    바깥이 자치구, 안쪽이 연월이다.
    """
    return [{"LAWD_CD": code, "DEAL_YMD": ym} for code in districts for ym in months]


def test_the_same_combinations_are_produced() -> None:
    """조합 **집합** 이 같다 — 이것이 "1,500줄을 두 줄로" 의 실제 주장이다."""
    from_spec = expand_param_grid({}, {"LAWD_CD": tuple(_DISTRICTS), "DEAL_YMD": tuple(_MONTHS)})

    assert {tuple(sorted(c.items())) for c in from_spec} == {
        tuple(sorted(c.items())) for c in _script_order(_DISTRICTS, _MONTHS)
    }


def test_the_combination_count_matches() -> None:
    assert len(
        expand_param_grid({}, {"LAWD_CD": tuple(_DISTRICTS), "DEAL_YMD": tuple(_MONTHS)})
    ) == len(_script_order(_DISTRICTS, _MONTHS))


def test_the_order_deliberately_differs_from_the_script() -> None:
    """순서는 다르고, 다른 것이 맞다.

    스크립트는 선언 순서(자치구 바깥, 연월 안쪽)로 돈다. spec 경로는 키를
    **이름순**으로 정렬하므로 `DEAL_YMD` 가 바깥이 된다.

    선언 순서를 따르면 안 되는 이유: `canonical_spec_mapping()` 이 스냅샷을 쓸 때
    매핑 키를 정렬한다. 선언 순서에 기대면 스냅샷을 다시 읽어 재생한 빌드가
    **원래 빌드와 다른 순서로 호출** 하게 되고, 그러면 같은 digest 의 recipe 가
    다른 Bronze 바이트를 낸다. 재현성이 거기서 깨진다.

    결과적으로 BuildSpec 경로로 재빌드한 Bronze 는 기존 배포 산출물과 **레코드
    집합은 같지만 바이트는 다르다.** #636(HF 데이터셋 재빌드)에서 기존 산출물과
    바이트 단위로 비교하면 이 차이가 먼저 나온다 — 데이터 차이가 아니다.
    """
    from_spec = expand_param_grid({}, {"LAWD_CD": tuple(_DISTRICTS), "DEAL_YMD": tuple(_MONTHS)})

    assert list(from_spec) != _script_order(_DISTRICTS, _MONTHS)
    # 바깥/안쪽이 뒤바뀐 것이 차이의 전부다.
    assert list(from_spec) == [
        {"DEAL_YMD": ym, "LAWD_CD": code} for ym in _MONTHS for code in _DISTRICTS
    ]


class _FakeDataset:
    def __init__(self, seen: list[dict[str, Any]]) -> None:
        self._seen = seen

    def list(self, **params: Any) -> Any:
        self._seen.append(dict(params))
        key = f"{params['LAWD_CD']}-{params['DEAL_YMD']}"
        return type("R", (), {"items": [{"id": key}]})()


class _FakeClient:
    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []

    def dataset(self, source_key: str) -> Any:
        return _FakeDataset(self.seen)


def test_a_build_fetches_exactly_the_script_s_combination_set() -> None:
    """엔진을 실제로 돌려 호출된 조합이 스크립트의 것과 같은지 본다."""
    from kpubdata_builder.spec import SourceRef
    from kpubdata_builder.stages.bronze.resolve import build_bronze_artifact_for_source

    client = _FakeClient()
    source = SourceRef(
        provider="datago",
        dataset="apt_trade",
        param_grid={"LAWD_CD": tuple(_DISTRICTS), "DEAL_YMD": tuple(_MONTHS)},
    )

    artifact = build_bronze_artifact_for_source(source, client=client)  # type: ignore[arg-type]

    assert {tuple(sorted(c.items())) for c in client.seen} == {
        tuple(sorted(c.items())) for c in _script_order(_DISTRICTS, _MONTHS)
    }
    assert len(artifact.raw_records) == len(_script_order(_DISTRICTS, _MONTHS))
