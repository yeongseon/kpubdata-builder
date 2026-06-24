"""dict ↔ Polars 변환 유틸 (#49).

원시 JSON 유사 레코드와 Polars DataFrame 사이의 양방향 변환을 담당한다.
공개 API에 Polars 타입을 강제하지 않도록 records 측은 plain dict를 사용한다.

주요 함수:
    - records_to_dataframe: 레코드 시퀀스 → pl.DataFrame
    - dataframe_to_records: pl.DataFrame → plain dict 리스트
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

import polars as pl

from ..errors import TabularError
from ..spec import JsonValue

# IEEE-754 double가 정확히 표현할 수 있는 정수의 절대값 상한(2^53). 이 범위를 넘는
# 정수가 float와 같은 컬럼에 섞이면 Polars가 f64로 업캐스트하며 조용히 반올림한다.
_SAFE_INTEGER = 2**53

# 타입 형태(shape) 통합용 센티넬.
_NULL = object()  # 알 수 없음/부재(null) — 어떤 타입과도 통합 가능.
_CONFLICT = object()  # 통합 불가능한 이질 타입.


def _shape(value: object) -> object:
    """값의 (중첩 포함) 타입 형태를 만든다.

    int/float는 "num"으로 묶어 [1, 2.5] 같은 정상 수치 혼합은 허용하되, list/struct
    내부까지 재귀하여 list[int] vs list[str], struct{x:int} vs struct{x:str} 같은
    중첩 이질 타입을 구분한다 (#199).
    """
    if value is None:
        return _NULL
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "num"
    if isinstance(value, str):
        return "str"
    if isinstance(value, Mapping):
        return ("map", {str(k): _shape(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        elem: object = _NULL
        for item in value:
            elem = _unify(elem, _shape(item))
        return ("list", elem)
    return ("scalar", type(value).__name__)


def _collect_numeric_kinds(value: object, path: str, acc: dict[str, set[str]]) -> None:
    """정밀도 위험 판별용 수치 종류를 구조적 경로별로 모은다 (#198).

    list 요소는 동일 dtype로 합쳐지므로 같은 경로(``path[]``)로 모으고, struct 필드는
    각각 별도 컬럼이 되므로 키별 경로(``path.key``)로 분리한다. top-level 값뿐 아니라
    중첩 list/struct 내부까지 재귀하므로, ``[{"v": [big_int]}, {"v": [2.5]}]`` 처럼
    중첩된 곳에서 일어나는 f64 업캐스트 반올림도 잡아낸다.
    """
    if isinstance(value, bool):
        return
    if isinstance(value, float):
        acc.setdefault(path, set()).add("float")
    elif isinstance(value, int):
        if abs(value) > _SAFE_INTEGER:
            acc.setdefault(path, set()).add("unsafe_int")
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _collect_numeric_kinds(item, f"{path}.{key}", acc)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_numeric_kinds(item, f"{path}[]", acc)


def _unify(a: object, b: object) -> object:
    """두 타입 형태를 통합한다. null은 무엇과도 통합되고, 충돌이면 _CONFLICT를 반환한다."""
    if a is _CONFLICT or b is _CONFLICT:
        return _CONFLICT
    if a is _NULL:
        return b
    if b is _NULL:
        return a
    if isinstance(a, str) and isinstance(b, str):
        return a if a == b else _CONFLICT
    if isinstance(a, tuple) and isinstance(b, tuple) and a[0] == b[0]:
        kind = a[0]
        if kind == "list":
            unified = _unify(a[1], b[1])
            return _CONFLICT if unified is _CONFLICT else ("list", unified)
        if kind == "map":
            merged: dict[str, object] = dict(cast(dict[str, object], a[1]))
            for key, shape in cast(dict[str, object], b[1]).items():
                # 한쪽에만 있는 키는 선택적 필드로 보고 null과 통합(허용)한다.
                merged[key] = _unify(merged.get(key, _NULL), shape)
                if merged[key] is _CONFLICT:
                    return _CONFLICT
            return ("map", merged)
        if kind == "scalar":
            return a if a[1] == b[1] else _CONFLICT
    return _CONFLICT


def records_to_dataframe(records: Sequence[dict[str, JsonValue]]) -> pl.DataFrame:
    """원시 레코드 매핑을 Polars DataFrame으로 변환한다.

    Polars 자동 추론에만 의존하면 혼합 타입 컬럼이 검증 전에 조용히 강제 변환되어
    원시 값이 바뀔 수 있다. 그래서 변환 전에 다음을 먼저 감지해 명확한 에러로 빠르게
    실패한다:

    - 컬럼(및 중첩 list/struct)에 호환되지 않는 타입이 섞인 경우 (#187, #199).
    - 큰 정수가 float와 같은 컬럼에 섞여 f64 업캐스트 시 정밀도가 손실되는 경우 (#198).

    매개변수:
        records: JSON 호환 레코드 시퀀스.

    반환값:
        pl.DataFrame: 입력 레코드의 컬럼 구조를 반영한 DataFrame.

    예외:
        TabularError: 이질 타입이 섞였거나 정수 정밀도 손실이 발생할 수 있는 경우.
    """
    shapes: dict[str, object] = {}
    conflicts: list[str] = []
    numeric_kinds: dict[str, set[str]] = {}

    for record in records:
        for key, value in record.items():
            _collect_numeric_kinds(value, key, numeric_kinds)
            shape = _unify(shapes.get(key, _NULL), _shape(value))
            shapes[key] = shape
            if shape is _CONFLICT and key not in conflicts:
                conflicts.append(key)

    if conflicts:
        raise TabularError(
            "heterogeneous column types detected (refusing to silently coerce): "
            f"{sorted(conflicts)}"
        )

    precision_risk = sorted(
        path
        for path, kinds in numeric_kinds.items()
        if "float" in kinds and "unsafe_int" in kinds
    )
    if precision_risk:
        raise TabularError(
            "integer precision loss risk: columns mix floats with integers beyond the "
            f"IEEE-754 safe range (±2^53) and would round on f64 upcast: {precision_risk}. "
            "Use an explicit cast to keep these columns as strings or integers."
        )

    # infer_schema_length=None: 모든 레코드를 스캔해 dtype를 추론한다. 기본 추론 윈도우
    # (앞쪽 일부 행)만 보면, 윈도우 밖에서 처음 등장하는 float가 Int64로 추론된 컬럼에서
    # 조용히 정수로 잘려나간다 (#216).
    return pl.DataFrame(list(records), infer_schema_length=None)


def dataframe_to_records(df: pl.DataFrame) -> list[dict[str, JsonValue]]:
    """Polars DataFrame을 plain dict 레코드 리스트로 변환한다.

    매개변수:
        df: 변환할 DataFrame.

    반환값:
        list[dict[str, JsonValue]]: 행별 dict 표현.
    """
    return cast(list[dict[str, JsonValue]], df.to_dicts())


__all__ = [
    "dataframe_to_records",
    "records_to_dataframe",
]
