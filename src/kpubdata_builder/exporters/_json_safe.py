"""Export 공용 JSON 안전 변환 (#629 후속).

Gold 테이블은 Polars에서 오므로 ``date``/``datetime``/``time``/``Decimal`` 같은
파이썬 객체가 레코드에 그대로 담긴다. ``json.dumps``는 이들을 직렬화하지 못해
``TypeError``를 던지는데, 그 예외는 서비스 경계에서 마스킹되기 때문에 사용자는
``casts: {deal_date: date}``를 선언했다는 이유만으로 원인을 알 수 없는 빌드
실패를 본다.

exporter마다 따로 처리하면 같은 선언이 포맷에 따라 다르게 실패한다. 변환 규칙을
한 곳에 둔다.
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from typing import Any

__all__ = ["json_safe"]


def json_safe(value: Any) -> Any:
    """JSON으로 직렬화 가능한 값으로 바꾼다 — 구조는 그대로 둔다.

    날짜/시각은 ISO-8601로, ``Decimal``은 정보 손실이 없는 문자열로 옮긴다.
    ``float``로 바꾸면 소수 자릿수가 조용히 달라진다 — 금액 컬럼에서 그것은
    데이터 변경이다.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        return value.total_seconds()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        # 바이너리를 임의 인코딩으로 문자열화하면 원문이 바뀐다. base64 같은
        # 표현을 고르는 것도 계약이므로, 여기서 조용히 정하지 않는다.
        raise TypeError(
            "binary values cannot be exported to a text format; "
            "declare a cast that turns this column into text first"
        )
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(item) for item in value]
    return str(value)
