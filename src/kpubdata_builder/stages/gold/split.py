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
    # 소수부가 큰 순서(동률이면 이름 순)로 잔여를 배분해 결정성을 보장한다.
    by_fraction = sorted(names, key=lambda name: (exact[name] - counts[name], name), reverse=True)
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


def _key_split(records: Sequence[Record], key: str) -> dict[str, tuple[Record, ...]]:
    """컬럼 값에 따라 레코드를 그룹으로 분할한다(값 → 분할 이름)."""
    grouped: dict[str, list[Record]] = {}
    for record in records:
        bucket = str(record.get(key, ""))
        grouped.setdefault(bucket, []).append(record)
    return {name: tuple(rows) for name, rows in grouped.items()}


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
