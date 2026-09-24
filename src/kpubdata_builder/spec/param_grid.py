"""``param_grid`` 전개 — 여러 파라미터 조합에 걸친 반복 호출 (#613).

한 source 가 단 한 번의 호출 조합만 표현할 수 있어서, 논문 실험의 3개 dataset
중 어느 것도 BuildSpec 만으로는 수집할 수 없었다. 자치구 25 × 월 60 = 1,500 개
조합을 source 1,500 개로 쓰는 것은 우회가 아니다 — source 마다 별도 Silver/Gold
산출물이 되어 하나의 dataset 으로 합쳐지지 않는다.

**전개 순서가 계약이다.** 순서가 바뀌면 concat 된 Bronze 의 바이트가 바뀌고,
``artifact_id`` 가 따라 바뀌어 R1 의 "같은 스냅샷·계약·빌더로 재빌드하면 같은
결과" 주장이 깨진다. 그래서 순서를 구현 세부가 아니라 이 모듈의 공개 계약으로
못박는다.
"""

from __future__ import annotations

from itertools import product

from .models import JsonValue

__all__ = ["expand_param_grid"]


def expand_param_grid(
    params: dict[str, JsonValue],
    param_grid: dict[str, tuple[JsonValue, ...]],
) -> tuple[dict[str, JsonValue], ...]:
    """공통 ``params`` 와 ``param_grid`` 를 호출 조합 목록으로 전개한다.

    순서 계약:

    1. **키는 이름순으로 정렬한다.** 선언에 쓴 YAML 키 순서에 의존하지 않는다 —
       ``canonical_spec_mapping()`` 이 스냅샷을 쓸 때 키를 정렬하므로, 선언 순서에
       기대면 같은 digest 의 spec 이 다른 순서로 호출하게 된다.
    2. **마지막 키가 가장 빨리 변한다** (``itertools.product`` 와 같은 중첩 순서).
    3. 각 키 안에서는 **선언된 값 순서를 그대로 지킨다.** 값은 정렬하지 않는다 —
       ``["202001", ..., "202012"]`` 같은 목록은 사람이 의도한 순서가 있고, 그것을
       뒤집을 이유가 없다.

    공통 ``params`` 는 모든 조합에 병합된다. 키가 겹치면 ``param_grid`` 가 이긴다 —
    그런 선언은 validator 가 이미 거부하므로 여기 도달하지 않지만, 라이브러리로
    직접 호출하는 경로를 위해 동작을 정의해 둔다.

    매개변수:
        params: 모든 조합에 공통으로 붙는 파라미터.
        param_grid: 키별 값 목록. 비어 있으면 ``params`` 하나만 돌려준다.

    반환값:
        호출 조합 튜플. ``param_grid`` 가 비면 길이 1(``params`` 자체)이다.
    """
    if not param_grid:
        return (dict(params),)

    keys = sorted(param_grid)
    value_lists = [param_grid[key] for key in keys]
    return tuple(
        {**params, **dict(zip(keys, combination, strict=True))}
        for combination in product(*value_lists)
    )
