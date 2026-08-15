"""Polars SQL execution isolated in a cancellable child process."""

from __future__ import annotations

import json
import math
import multiprocessing
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import date, datetime
from datetime import time as time_value
from decimal import Decimal
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import cast

from ..spec import JsonValue
from .models import QueryResult


class QueryExecutionError(RuntimeError):
    pass


class QueryTimeoutError(QueryExecutionError):
    pass


MAX_QUERY_RESPONSE_BYTES = 8 * 1024 * 1024


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return cast(JsonValue, value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (date, datetime, time_value)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)


def _query_worker(connection: Connection, table_path: str, canonical_sql: str, limit: int) -> None:
    try:
        import polars as pl

        frame = pl.scan_parquet(table_path)
        context = pl.SQLContext({"dataset": frame}, eager=False, register_globals=False)
        bounded_sql = f"SELECT * FROM ({canonical_sql}) AS _kpubdata_result LIMIT {limit + 1}"
        result = context.execute(bounded_sql).collect()
        rows = [
            {str(key): _json_value(value) for key, value in row.items()}
            for row in result.to_dicts()
        ]
        payload = {
            "ok": True,
            "columns": list(result.columns),
            "rows": rows[:limit],
            "truncated": len(rows) > limit,
        }
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_QUERY_RESPONSE_BYTES:
            connection.send({"ok": False})
        else:
            connection.send(payload)
    except BaseException:
        # Engine messages can contain absolute parquet paths. Never cross the
        # process boundary with raw exceptions or tracebacks.
        with suppress(BrokenPipeError, EOFError, OSError):
            connection.send({"ok": False})
    finally:
        connection.close()


class QueryEngine:
    def __init__(
        self,
        *,
        timeout_seconds: float = 10.0,
        worker: Callable[[Connection, str, str, int], None] = _query_worker,
    ) -> None:
        self._timeout_seconds = timeout_seconds
        self._worker = worker

    def execute(self, table_path: Path, canonical_sql: str, *, limit: int) -> QueryResult:
        started = time.monotonic()
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=False)
        process = context.Process(
            target=self._worker,
            args=(child, str(table_path), canonical_sql, limit),
            daemon=True,
        )
        process_started = False
        try:
            process.start()
            process_started = True
            child.close()
            if not parent.poll(self._timeout_seconds):
                self._stop_process(process)
                raise QueryTimeoutError("query execution timed out")
            try:
                payload = parent.recv()
            except (EOFError, OSError) as exc:
                raise QueryExecutionError("query execution failed") from exc
            process.join(timeout=1.0)
            if process.is_alive():
                self._stop_process(process)
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                raise QueryExecutionError("query execution failed")
            columns = payload.get("columns")
            rows = payload.get("rows")
            truncated = payload.get("truncated")
            if (
                not isinstance(columns, list)
                or not isinstance(rows, list)
                or not isinstance(truncated, bool)
            ):
                raise QueryExecutionError("query returned an invalid result")
            elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
            return QueryResult(
                columns=tuple(str(column) for column in columns),
                rows=tuple(cast(dict[str, JsonValue], row) for row in rows),
                truncated=truncated,
                execution_ms=elapsed_ms,
            )
        finally:
            child.close()
            parent.close()
            if process_started:
                if process.is_alive():
                    self._stop_process(process)
                if not process.is_alive():
                    process.close()
            else:
                with suppress(ValueError):
                    process.close()

    @staticmethod
    def _stop_process(process: BaseProcess) -> None:
        if not process.is_alive():
            process.join(timeout=0)
            return
        process.terminate()
        process.join(timeout=1.0)
        if process.is_alive():
            process.kill()
            process.join(timeout=1.0)
        if process.is_alive():
            raise QueryExecutionError("query process could not be stopped")


__all__ = [
    "MAX_QUERY_RESPONSE_BYTES",
    "QueryEngine",
    "QueryExecutionError",
    "QueryTimeoutError",
]
