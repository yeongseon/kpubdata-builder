"""File/URL 원시 bytes를 Bronze 레코드로 파싱한다 (#498).

File upload와 URL fetch는 서로 다른 경로로 bytes를 얻지만, 그 bytes를 레코드로
바꾸는 규칙은 동일해야 한다(같은 CSV 파싱 결과는 어디서 왔든 같은 레코드가
되어야 한다) — 그래서 두 kind가 이 모듈 하나를 공유한다.

지원 포맷은 CSV/JSON/JSONL/Parquet(#498 P0 범위)이다. Excel/ZIP은 범위 밖이며
loader/validator가 이미 그 값을 거부한다.
"""

from __future__ import annotations

import io
import json
from typing import cast

import polars as pl

from ..spec import JsonValue
from ..tabular.convert import dataframe_to_records
from .errors import IngestionError

_TEXT_FORMATS = frozenset({"csv", "json", "jsonl"})


def parse_tabular_bytes(
    raw: bytes,
    *,
    format: str,
    encoding: str = "utf-8",  # noqa: A002 - 계약 필드명과 맞춘다
) -> tuple[dict[str, JsonValue], ...]:
    """원시 bytes를 ``format`` 규칙으로 파싱해 레코드 튜플로 반환한다.

    매개변수:
        raw: 파일 또는 HTTP 응답의 원시 bytes.
        format: ``"csv"`` | ``"json"`` | ``"jsonl"`` | ``"parquet"``.
        encoding: 텍스트 포맷(csv/json/jsonl) 디코딩에 쓸 인코딩. parquet은
            바이너리 포맷이라 무시된다.

    반환값:
        레코드 튜플. Bronze pipeline이 소비하는 것과 동일한
        ``dict[str, JsonValue]`` 형태다.

    예외:
        IngestionError: 빈 content, 지원하지 않는 format, 디코딩/파싱 실패.
    """
    if not raw:
        raise IngestionError("source content is empty")

    if format == "parquet":
        return _parse_parquet(raw)
    if format in _TEXT_FORMATS:
        text = _decode(raw, encoding)
        if format == "csv":
            return _parse_csv(text)
        if format == "json":
            return _parse_json(text)
        return _parse_jsonl(text)
    raise IngestionError(f"unsupported format: {format!r}")


def _decode(raw: bytes, encoding: str) -> str:
    try:
        return raw.decode(encoding)
    except (LookupError, UnicodeDecodeError) as exc:
        raise IngestionError(f"failed to decode content as {encoding!r}: {exc}") from exc


def _parse_parquet(raw: bytes) -> tuple[dict[str, JsonValue], ...]:
    try:
        frame = pl.read_parquet(io.BytesIO(raw))
    except Exception as exc:  # polars가 던지는 예외 타입이 다양해 광범위하게 잡는다
        raise IngestionError(f"failed to parse parquet content: {exc}") from exc
    return tuple(dataframe_to_records(frame))


def _parse_csv(text: str) -> tuple[dict[str, JsonValue], ...]:
    try:
        frame = pl.read_csv(io.StringIO(text), infer_schema_length=None)
    except Exception as exc:
        raise IngestionError(f"failed to parse csv content: {exc}") from exc
    return tuple(dataframe_to_records(frame))


def _parse_json(text: str) -> tuple[dict[str, JsonValue], ...]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise IngestionError(f"failed to parse json content: {exc}") from exc
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        # 자유형 key 추측(예: {"data": [...]}) 대신 명시적으로 array-of-object만
        # 허용한다 — 이 코드베이스가 quality.compare_columns 등에서 이미 지키는
        # "자유형 eval/추측 금지" 원칙과 동일하다.
        raise IngestionError(
            'json content must be a top-level array of objects (e.g. [{"col": "value"}, ...])'
        )
    return tuple(cast(list[dict[str, JsonValue]], data))


def _parse_jsonl(text: str) -> tuple[dict[str, JsonValue], ...]:
    records: list[dict[str, JsonValue]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise IngestionError(f"failed to parse jsonl line {line_number}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise IngestionError(f"jsonl line {line_number} must be a JSON object")
        records.append(parsed)
    if not records:
        raise IngestionError("jsonl content has no non-empty lines")
    return tuple(records)


__all__ = ["parse_tabular_bytes"]
