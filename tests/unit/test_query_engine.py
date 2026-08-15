"""Query child cancellation and bounded result execution tests."""

from __future__ import annotations

import os
import time
from multiprocessing.connection import Connection
from pathlib import Path

import pytest

import kpubdata_builder.query.engine as engine_module
from kpubdata_builder.query.engine import QueryEngine, QueryExecutionError, QueryTimeoutError


def _sleeping_worker(
    connection: Connection, table_path: str, canonical_sql: str, limit: int
) -> None:
    del canonical_sql, limit
    Path(table_path).write_text(str(os.getpid()), encoding="utf-8")
    try:
        time.sleep(60)
    finally:
        connection.close()


def _pid_is_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
                return False
            return exit_code.value == 259
        finally:
            ctypes.windll.kernel32.CloseHandle(process)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_timeout_leaves_child_not_alive(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    # Windows spawn imports the test module in a fresh interpreter, so leave
    # enough time for the worker to publish its PID before exercising timeout.
    engine = QueryEngine(timeout_seconds=5, worker=_sleeping_worker)

    with pytest.raises(QueryTimeoutError):
        engine.execute(pid_file, "SELECT * FROM dataset", limit=1)

    pid = int(pid_file.read_text(encoding="utf-8"))
    assert not _pid_is_alive(pid)


def test_real_engine_executes_canonical_sql_and_hard_limit(tmp_path: Path) -> None:
    import polars as pl

    table_path = tmp_path / "table.parquet"
    pl.DataFrame({"value": [3, 1, 2]}).write_parquet(table_path)

    result = QueryEngine(timeout_seconds=5).execute(
        table_path,
        "SELECT value FROM dataset ORDER BY value",
        limit=2,
    )

    assert result.columns == ("value",)
    assert result.rows == ({"value": 1}, {"value": 2})
    assert result.truncated is True


def test_real_engine_raises_execution_error_for_unresolvable_column(tmp_path: Path) -> None:
    """Passes AST validation but fails inside Polars: a runtime error, not a syntax one."""
    import polars as pl

    table_path = tmp_path / "table.parquet"
    pl.DataFrame({"value": [1, 2, 3]}).write_parquet(table_path)

    with pytest.raises(QueryExecutionError):
        QueryEngine(timeout_seconds=5).execute(
            table_path,
            "SELECT nonexistent_column FROM dataset",
            limit=1,
        )


class _CaptureConnection:
    def __init__(self) -> None:
        self.payload: object = None
        self.closed = False

    def send(self, payload: object) -> None:
        self.payload = payload

    def close(self) -> None:
        self.closed = True


def test_worker_rejects_result_over_response_byte_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import polars as pl

    table_path = tmp_path / "table.parquet"
    pl.DataFrame({"value": ["x" * 200]}).write_parquet(table_path)
    connection = _CaptureConnection()
    monkeypatch.setattr(engine_module, "MAX_QUERY_RESPONSE_BYTES", 100)

    engine_module._query_worker(  # type: ignore[arg-type]
        connection, str(table_path), "SELECT * FROM dataset", 1
    )

    assert connection.payload == {"ok": False}
    assert connection.closed is True


class _StubbornProcess:
    def __init__(self) -> None:
        self.alive = True
        self.calls: list[str] = []

    def is_alive(self) -> bool:
        return self.alive

    def terminate(self) -> None:
        self.calls.append("terminate")

    def kill(self) -> None:
        self.calls.append("kill")
        self.alive = False

    def join(self, timeout: float | None = None) -> None:
        self.calls.append(f"join:{timeout}")


def test_stop_process_uses_bounded_join_then_kill_fallback() -> None:
    process = _StubbornProcess()

    QueryEngine._stop_process(process)  # type: ignore[arg-type]

    assert process.calls == ["terminate", "join:1.0", "kill", "join:1.0"]
    assert process.is_alive() is False
