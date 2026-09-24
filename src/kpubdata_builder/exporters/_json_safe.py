"""Export 공용 JSON 안전 변환 (#629 후속).

Gold 테이블은 Polars에서 오므로 ``date``/``datetime``/``Decimal`` 같은 파이썬
객체가 레코드에 그대로 담긴다. ``json.dumps``는 이들을 직렬화하지 못해
``TypeError``를 던지는데, 그 예외는 서비스 경계에서 마스킹되기 때문에 사용자는
``casts: {deal_date: date}``를 선언했다는 이유만으로 원인을 알 수 없는 빌드
실패를 본다.

**표준 표현이 하나뿐인 타입만 바꾼다.** 임의의 객체를 ``str()``로 떨어뜨리거나
``set``을 리스트로 펴는 것은 데이터를 조용히 바꾸는 일이다 — 그런 값은 그대로
``json.dumps``에 도달해 ``TypeError``로 실패해야 한다. 어떤 표현을 고를지는
계약이고, 이 계층이 말없이 정할 것이 아니다.
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from typing import Any

__all__ = ["json_safe"]


def json_safe(value: Any) -> Any:
    """무손실 표준 표현이 있는 값만 JSON 호환 값으로 바꾼다.

    날짜/시각은 ISO-8601로, ``Decimal``은 문자열로 옮긴다. ``float``로 바꾸면
    소수 자릿수가 조용히 달라진다 — 금액 컬럼에서 그것은 데이터 변경이다.

    그 밖의 타입은 **손대지 않고 그대로 돌려준다.** 직렬화 가능 여부의 판정은
    ``json.dumps``가 하고, 불가능하면 ``TypeError``로 드러난다.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [json_safe(item) for item in value]
    if isinstance(value, tuple):
        # tuple은 json.dumps가 배열로 직렬화하는 기존 동작을 유지한다.
        return [json_safe(item) for item in value]
    return value
