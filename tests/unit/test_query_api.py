"""Query API routing and HTTP-worker starvation boundary tests."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import cast

import pytest

import kpubdata_builder.query.resolver as resolver_module
import kpubdata_builder.service.app as app_module
from kpubdata_builder.query.engine import QueryExecutionError
from kpubdata_builder.query.models import QueryResult
from kpubdata_builder.query.resolver import ResolvedQueryContext
from kpubdata_builder.query.service import QueryService
from kpubdata_builder.service.app import _OWNERSHIP_ENV, BuilderService
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.stages._stage_reader import silver_source_dir


class _BlockingEngine:
    def __init__(self) -> None:
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()

    def execute(self, table_path: Path, canonical_sql: str, *, limit: int) -> QueryResult:
        del table_path, canonical_sql, limit
        with self._lock:
            self.calls += 1
            if self.calls == 2:
                self.started.set()
        self.release.wait(timeout=5)
        return QueryResult((), (), False, 1)


class _QueryStub:
    def execute(self, table_path: Path, canonical_sql: str, *, limit: int) -> QueryResult:
        del table_path, canonical_sql, limit
        return QueryResult(("value",), ({"value": 1},), False, 3)


def _context(tmp_path: Path) -> ResolvedQueryContext:
    return ResolvedQueryContext("ds", "r1", "silver", "source", tmp_path / "table.parquet")


def _body() -> dict[str, JsonValue]:
    return {
        "sql": "SELECT * FROM dataset",
        "dataset_id": "ds",
        "run_id": "r1",
        "stage": "silver",
        "source": "source",
    }


def test_query_response_contains_only_documented_result_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        app_module, "resolve_query_context", lambda root, request, principal: _context(tmp_path)
    )
    service = BuilderService(
        output_root=tmp_path,
        client_factory=lambda: cast(object, None),
        query_service=cast(QueryService, _QueryStub()),
    )

    response = service.query(_body(), principal=Principal("dev"))

    assert response.status_code == 200
    assert set(cast(dict[str, JsonValue], response.body)) == {
        "columns",
        "rows",
        "truncated",
        "execution_ms",
    }


def test_query_syntax_error_returns_400_with_unsafe_query_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        app_module, "resolve_query_context", lambda root, request, principal: _context(tmp_path)
    )
    service = BuilderService(
        output_root=tmp_path,
        client_factory=lambda: cast(object, None),
        query_service=cast(QueryService, _QueryStub()),
    )
    body = _body()
    body["sql"] = "SELECT * FROMM dataset"

    response = service.query(body, principal=Principal("dev"))

    assert response.status_code == 400
    payload = cast(dict[str, JsonValue], response.body)
    assert payload["code"] == "unsafe_query"
    assert isinstance(payload["error"], str) and payload["error"]


class _ExecutionFailingQueryStub:
    def execute(self, table_path: Path, canonical_sql: str, *, limit: int) -> QueryResult:
        del table_path, canonical_sql, limit
        raise QueryExecutionError("query execution failed")


def test_query_execution_error_returns_400_with_execution_failed_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Valid SQL that the engine cannot execute is a runtime error, distinct from a
    syntax/policy rejection (``unsafe_query``) — both surface as 400 but with
    different stable ``code`` values so clients can distinguish them."""
    monkeypatch.setattr(
        app_module, "resolve_query_context", lambda root, request, principal: _context(tmp_path)
    )
    service = BuilderService(
        output_root=tmp_path,
        client_factory=lambda: cast(object, None),
        query_service=cast(QueryService, _ExecutionFailingQueryStub()),
    )

    response = service.query(_body(), principal=Principal("dev"))

    assert response.status_code == 400
    payload = cast(dict[str, JsonValue], response.body)
    assert payload["code"] == "query_execution_failed"


def test_query_saturation_rejects_immediately_and_version_remains_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _BlockingEngine()
    query_service = QueryService(engine=cast(object, engine), max_concurrency=2)  # type: ignore[arg-type]
    monkeypatch.setattr(
        app_module, "resolve_query_context", lambda root, request, principal: _context(tmp_path)
    )
    service = BuilderService(
        output_root=tmp_path,
        client_factory=lambda: cast(object, None),
        query_service=query_service,
    )
    responses: list[int] = []
    threads = [
        threading.Thread(
            target=lambda: responses.append(
                service.query(_body(), principal=Principal("dev")).status_code
            )
        )
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    assert engine.started.wait(timeout=2)

    started = time.monotonic()
    saturated = service.query(_body(), principal=Principal("dev"))
    assert saturated.status_code == 429
    assert cast(dict[str, JsonValue], saturated.body)["code"] == "query_busy"
    assert time.monotonic() - started < 0.25
    assert engine.calls == 2
    assert service.version().status_code == 200

    engine.release.set()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert responses == [200, 200]


class TestQueryOwnershipEnforcement:
    """ENFORCE_OWNERSHIP regression for POST /query (#504).

    resolve_query_context를 몽키패치하지 않고 실제로 호출해, resolver의 기존
    소유권 판정(``_ownership_allowed``)이 service 계층에서 403으로 이어지는지만
    확인한다 — ownership 구현 자체는 건드리지 않는다.
    """

    def _prepare_run(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, created_by: str
    ) -> None:
        manifest = {"inputs": ["source"], "created_by": created_by}
        monkeypatch.setattr(
            resolver_module.datasets_service, "read_manifest", lambda root, run_id: manifest
        )
        monkeypatch.setattr(
            resolver_module.datasets_service,
            "read_snapshot_dataset_id",
            lambda root, run_id: "ds",
        )
        monkeypatch.setattr(
            resolver_module.stages_service,
            "stage_status_for_source",
            lambda root, run_id, value, source: object(),
        )
        monkeypatch.setattr(
            resolver_module.stages_service,
            "stage_status_of",
            lambda status, requested_stage: "completed",
        )
        source_dir = silver_source_dir(tmp_path, "r1", "source")
        source_dir.mkdir(parents=True)
        (source_dir / "table.parquet").write_bytes(b"fixture")

    def _service(self, tmp_path: Path) -> BuilderService:
        return BuilderService(
            output_root=tmp_path,
            client_factory=lambda: cast(object, None),
            query_service=cast(QueryService, _QueryStub()),
        )

    def test_query_from_non_owner_returns_403(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        self._prepare_run(monkeypatch, tmp_path, created_by="oidc:userA")
        service = self._service(tmp_path)

        response = service.query(_body(), principal=Principal(kind="oidc", identifier="userB"))

        assert response.status_code == 403
        assert cast(dict[str, JsonValue], response.body)["code"] == "forbidden"

    def test_query_from_owner_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        self._prepare_run(monkeypatch, tmp_path, created_by="oidc:userA")
        service = self._service(tmp_path)

        response = service.query(_body(), principal=Principal(kind="oidc", identifier="userA"))

        assert response.status_code == 200

    def test_query_from_dev_principal_bypasses_ownership(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """dev/service principal은 소유권 검사를 우회한다 (기존 semantics)."""
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        self._prepare_run(monkeypatch, tmp_path, created_by="oidc:userA")
        service = self._service(tmp_path)

        response = service.query(_body(), principal=Principal("dev"))

        assert response.status_code == 200


class TestQueryRequestValidation:
    """Request-shape 400 regression for POST /query (#504)."""

    def test_stage_bronze_returns_400(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            app_module, "resolve_query_context", lambda root, request, principal: _context(tmp_path)
        )
        service = BuilderService(
            output_root=tmp_path,
            client_factory=lambda: cast(object, None),
            query_service=cast(QueryService, _QueryStub()),
        )
        body = _body()
        body["stage"] = "bronze"

        response = service.query(body, principal=Principal("dev"))

        assert response.status_code == 400

    @pytest.mark.parametrize("bad_limit", [0, 501])
    def test_limit_out_of_range_returns_400(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_limit: int
    ) -> None:
        monkeypatch.setattr(
            app_module, "resolve_query_context", lambda root, request, principal: _context(tmp_path)
        )
        service = BuilderService(
            output_root=tmp_path,
            client_factory=lambda: cast(object, None),
            query_service=cast(QueryService, _QueryStub()),
        )
        body = _body()
        body["limit"] = bad_limit

        response = service.query(body, principal=Principal("dev"))

        assert response.status_code == 400

    def test_limit_non_integer_returns_400(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            app_module, "resolve_query_context", lambda root, request, principal: _context(tmp_path)
        )
        service = BuilderService(
            output_root=tmp_path,
            client_factory=lambda: cast(object, None),
            query_service=cast(QueryService, _QueryStub()),
        )
        body = _body()
        body["limit"] = "10"

        response = service.query(body, principal=Principal("dev"))

        assert response.status_code == 400
