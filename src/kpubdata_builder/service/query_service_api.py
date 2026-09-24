"""Query 도메인 서비스 (#596, providers·uploads 에 이은 세 번째 조각).

``POST /query`` 한 엔드포인트지만 **오류 분류가 이 도메인의 본체**다: 권한·아티팩트
부재·문맥 오류·안전하지 않은 SQL·혼잡·타임아웃·실행 실패를 각각 다른 상태 코드와
``code`` 로 내보낸다. 한 클래스에 묶여 있을 때보다 여기 모여 있는 편이 "어떤 실패가
어떤 응답이 되는가"를 확인하기 쉽다.

앞선 두 조각과 같은 규칙: **자기 의존성만 받고**, wire 계약(상태 코드·``code`` 값·본문
키)은 그대로다.

이름이 ``query_service_api`` 인 이유: ``kpubdata_builder.query.service`` 에 이미 실행
엔진 쪽 ``QueryService`` 가 있다. 이 모듈은 그 엔진을 쓰는 **HTTP 도메인 서비스**라,
import 할 때 둘이 헷갈리지 않도록 이름을 분리했다.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast

from kpubdata_builder.query.engine import QueryExecutionError, QueryTimeoutError
from kpubdata_builder.query.models import QueryRequest, QueryStage
from kpubdata_builder.query.resolver import (
    QueryArtifactUnavailableError,
    QueryContextError,
    resolve_query_context,
)
from kpubdata_builder.query.security import UnsafeQueryError, validate_read_only_sql
from kpubdata_builder.query.service import QueryBusyError, QueryService
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.responses import ServiceResponse
from kpubdata_builder.spec import JsonValue

_ALLOWED_FIELDS = {"dataset_id", "run_id", "stage", "source", "sql", "limit"}


def query_request_from_body(body: Mapping[str, JsonValue] | None) -> QueryRequest:
    """요청 본문을 ``QueryRequest`` 로 검증·변환한다.

    알 수 없는 필드를 거부한다 — 오타를 조용히 무시하면 사용자는 자기 의도와 다른
    쿼리가 돈 것을 모른다.
    """
    if body is None:
        raise ValueError("request body is required")
    if not set(body).issubset(_ALLOWED_FIELDS):
        raise ValueError("request contains unknown fields")
    dataset_id = body.get("dataset_id")
    run_id = body.get("run_id")
    stage = body.get("stage")
    sql = body.get("sql")
    source = body.get("source")
    limit = body.get("limit", 100)
    if not isinstance(dataset_id, str) or not dataset_id:
        raise ValueError("dataset_id must be a non-empty string")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("run_id must be a non-empty string")
    if stage not in ("silver", "gold"):
        raise ValueError("stage must be silver or gold")
    if not isinstance(sql, str) or not sql:
        raise ValueError("sql must be a non-empty string")
    if source is not None and (not isinstance(source, str) or not source):
        raise ValueError("source must be a non-empty string when provided")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
        raise ValueError("limit must be an integer from 1 to 500")
    return QueryRequest(
        dataset_id=dataset_id,
        run_id=run_id,
        stage=cast(QueryStage, stage),
        source=source,
        sql=sql,
        limit=limit,
    )


class QueryApiService:
    """서버가 resolve 한 stage 테이블에 대한 읽기 전용 SQL 실행."""

    def __init__(self, *, output_root: Path, engine: QueryService) -> None:
        self._output_root = output_root
        self._engine = engine

    def query(
        self, body: Mapping[str, JsonValue] | None, *, principal: Principal
    ) -> ServiceResponse:
        """서버가 resolve한 stage 테이블에 대해 검증된 SQL 쿼리 1건을 실행한다."""
        try:
            request = query_request_from_body(body)
            context = resolve_query_context(self._output_root, request, principal)
            validated = validate_read_only_sql(request.sql)
            result = self._engine.execute(
                context.table_path, validated.canonical_sql, limit=request.limit
            )
        except PermissionError:
            return ServiceResponse(403, {"error": "forbidden", "code": "forbidden"})
        except QueryArtifactUnavailableError:
            return ServiceResponse(
                404, {"error": "query artifact unavailable", "code": "artifact_unavailable"}
            )
        except QueryContextError as exc:
            return ServiceResponse(400, {"error": str(exc), "code": "invalid_context"})
        except UnsafeQueryError as exc:
            return ServiceResponse(400, {"error": str(exc), "code": "unsafe_query"})
        except QueryBusyError:
            return ServiceResponse(429, {"error": "query is busy", "code": "query_busy"})
        except QueryTimeoutError:
            return ServiceResponse(504, {"error": "query timed out", "code": "query_timeout"})
        except QueryExecutionError:
            return ServiceResponse(
                400, {"error": "query execution failed", "code": "query_execution_failed"}
            )
        except ValueError as exc:
            return ServiceResponse(400, {"error": str(exc), "code": "invalid_request"})

        return ServiceResponse(
            200,
            {
                "columns": list(result.columns),
                "rows": list(result.rows),
                "truncated": result.truncated,
                "execution_ms": result.execution_ms,
                "startup_ms": result.startup_ms,
                "engine_execution_ms": result.engine_execution_ms,
            },
        )


__all__ = ["QueryApiService", "query_request_from_body"]
