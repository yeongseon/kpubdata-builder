"""Read-only query request/result models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..spec import JsonValue

QueryStage = Literal["silver", "gold"]


@dataclass(frozen=True)
class QueryRequest:
    dataset_id: str
    run_id: str
    stage: QueryStage
    sql: str
    source: str | None = None
    limit: int = 100


@dataclass(frozen=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: tuple[dict[str, JsonValue], ...]
    truncated: bool
    execution_ms: int


__all__ = ["QueryRequest", "QueryResult", "QueryStage"]
