"""레코드를 명명된 분할로 나누는 로직 (#38).

SplitSpec에 따라 레코드 시퀀스를 train/val/test 같은 비율 분할 또는 컬럼 값
기반(연도/지역/카테고리) 분할로 나눈다. 비율 분할은 시드 기반으로 결정적이며,
레코드 순서와 무관하게 재현 가능하다.

주요 함수:
    - apply_splits: 레코드 + SplitSpec → {split 이름: 레코드 튜플}
"""

from __future__ import annotations

import random
from collections.abc import Sequence

from ...spec import JsonValue, SplitSpec

Record = dict[str, JsonValue]


def _allocate_counts(total: int, ratios: dict[str, float], names: list[str]) -> dict[str, int]:
    """비율을 정수 카운트로 배분한다(합 = total). 잔여는 큰 소수부 순으로 분배한다."""
    ratio_sum = sum(ratios.values())
    exact = {name: total * ratios[name] / ratio_sum for name in names}
    counts = {name: int(exact[name]) for name in names}
    remainder = total - sum(counts.values())
    # 소수부가 큰 순서(동률이면 이름 정순)로 잔여를 배분해 결정성을 보장한다.
    by_fraction = sorted(
        names,
        key=lambda name: (-(exact[name] - counts[name]), name),
    )
    for name in by_fraction[:remainder]:
        counts[name] += 1
    return counts


def _ratio_split(
    records: Sequence[Record], ratios: dict[str, float], seed: int
) -> dict[str, tuple[Record, ...]]:
    """비율에 따라 레코드를 결정적으로 분할한다."""
    names = sorted(ratios)
    counts = _allocate_counts(len(records), ratios, names)
    order = list(range(len(records)))
    random.Random(seed).shuffle(order)

    result: dict[str, tuple[Record, ...]] = {}
    position = 0
    for name in names:
        chosen = order[position : position + counts[name]]
        position += counts[name]
        # 원본 순서를 보존해 결과를 안정적으로 만든다.
        result[name] = tuple(records[index] for index in sorted(chosen))
    return result


# 실제 레코드 값과 충돌하지 않도록 문자열이 아닌 단일 객체를 센티널로 사용한다.
# object()는 str() 가능하지만, 동일한 object() 인스턴스는 정체성(identity)으로만
# 구분된다. 이 고유한 정체성을 내부 버킷 키로 사용하므로, "__missing__" 또는
# "__null__" 리터럴 문자열 값을 가진 레코드가 잘못된 버킷에 합산되는 충돌을
# 방지한다 (#225).
_MISSING_KEY_SENTINEL: object = object()
_NULL_VALUE_SENTINEL: object = object()

_SENTINEL_NAMES: dict[object, str] = {
    _MISSING_KEY_SENTINEL: "__missing__",
    _NULL_VALUE_SENTINEL: "__null__",
}


def _key_split(records: Sequence[Record], key: str) -> dict[str, tuple[Record, ...]]:
    """컬럼 값에 따라 레코드를 그룹으로 분할한다(값 → 분할 이름).

    키가 없는 레코드는 "__missing__" 버킷, None 값은 "__null__" 버킷,
    빈 문자열은 "" 버킷으로 각각 분리한다.

    센티널 객체를 내부 버킷 키로 사용해 키가 없는/None인 레코드와 리터럴
    "__missing__"/"__null__" 문자열을 가진 레코드가 컬렉션 단계에서 합산되지
    않도록 분리한다. 출력 dict 구성 시 센티널을 문자열 이름으로 변환하면서
    이름이 충돌하면 병합(extend)한다 (#225).
    """
    grouped: dict[object, list[Record]] = {}
    for record in records:
        if key not in record:
            bucket: object = _MISSING_KEY_SENTINEL
        elif record[key] is None:
            bucket = _NULL_VALUE_SENTINEL
        else:
            bucket = str(record[key])
        grouped.setdefault(bucket, []).append(record)
    # 센티널을 출력 이름으로 변환; 이름 충돌 시 병합해 레코드 손실을 막는다.
    result: dict[str, list[Record]] = {}
    for k, rows in grouped.items():
        name: str = k if isinstance(k, str) else _SENTINEL_NAMES[k]
        result.setdefault(name, []).extend(rows)
    return {name: tuple(rows) for name, rows in result.items()}


def apply_splits(records: Sequence[Record], spec: SplitSpec) -> dict[str, tuple[Record, ...]]:
    """SplitSpec에 따라 레코드를 명명된 분할로 나눈다.

    매개변수:
        records: 분할할 레코드 시퀀스.
        spec: 분할 정의.

    반환값:
        dict[str, tuple[Record, ...]]: 분할 이름 → 레코드 튜플.

    예외:
        ValueError: 지원하지 않는 split 모드인 경우.
    """
    if spec.mode == "ratio":
        return _ratio_split(records, spec.ratios, spec.seed)
    if spec.mode == "key":
        return _key_split(records, spec.key)
    raise ValueError(f"Unsupported split mode: {spec.mode!r}")


__all__ = ["apply_splits"]
