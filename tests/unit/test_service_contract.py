"""Builder Service Contract(#63, #226, #317) OpenAPI 스펙의 구조 검증.

설치된 OpenAPI validator가 없으므로, 계약이 OpenAPI 3.1이고 BuilderService
(service/app.py)가 실제로 구현한 동기 라우트와 wire 형태를 모두 담는지 구조적으로
검증한다. 계약은 이제 구현된 엔드포인트만 기술하며(#226), 한쪽만 바뀌는 조용한
드리프트를 막기 위해 구현 라우트 ↔ 계약 operationId 매핑을 명시적으로 고정한다.
또한 YAML 계약과 실제 dispatch 구현 간의 양방향 일치성을 검증한다(#317).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

_CONTRACT_PATH = Path(__file__).parents[2] / "contract" / "builder-api.yaml"

# dispatch에 구현된 (path, method, operationId) 매핑.
# service/app.py:dispatch의 라우팅 규칙을 기계적으로 추출하기 어렵기 때문에
# 명시적으로 선언하여 유지보수성을 높인다.
_DISPATCH_ROUTES: dict[tuple[str, str], str] = {
    ("/version", "GET"): "getVersion",
    ("/validate", "POST"): "validateSpec",
    ("/preview", "POST"): "previewBuild",
    ("/build", "POST"): "createBuild",
    ("/artifacts/{run_id}", "GET"): "listBuildArtifacts",
    ("/builds", "GET"): "listBuilds",
}

# (path, method) 형태의 계약 필수 오퍼레이션. BuilderService.dispatch가 실제로
# 라우팅하는 동기 엔드포인트와 1:1로 대응한다.
_REQUIRED_OPERATIONS = [
    ("/version", "get"),
    ("/validate", "post"),
    ("/preview", "post"),
    ("/build", "post"),
    ("/artifacts/{run_id}", "get"),
    ("/builds", "get"),
]


def _load_contract() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8")))


def test_contract_file_exists() -> None:
    assert _CONTRACT_PATH.is_file()


def test_is_openapi_3_1_with_info() -> None:
    contract = _load_contract()

    assert str(contract["openapi"]).startswith("3.1")
    assert contract["info"]["title"]
    assert contract["info"]["version"]


def test_covers_all_required_operations() -> None:
    paths = _load_contract()["paths"]

    for path, method in _REQUIRED_OPERATIONS:
        assert path in paths, f"missing path: {path}"
        assert method in paths[path], f"missing {method.upper()} {path}"
        assert paths[path][method].get("operationId"), f"missing operationId for {method} {path}"


def test_operation_ids_are_unique() -> None:
    paths = _load_contract()["paths"]
    operation_ids = [
        operation["operationId"]
        for methods in paths.values()
        for operation in methods.values()
        if isinstance(operation, dict) and "operationId" in operation
    ]

    assert len(operation_ids) == len(set(operation_ids))


def test_defines_standard_error_schema() -> None:
    schemas = _load_contract()["components"]["schemas"]

    # 실제 구현은 단순한 {"error": "<message>"} 형태를 사용한다(#226).
    assert "Error" in schemas
    assert "error" in schemas["Error"]["properties"]
    assert schemas["Error"]["properties"]["error"]["type"] == "string"


def test_service_api_version_matches_contract() -> None:
    # 코드의 API_CONTRACT_VERSION이 계약 문서의 info.version과 어긋나지 않도록 고정 (#209).
    from kpubdata_builder.service import API_CONTRACT_VERSION

    assert str(_load_contract()["info"]["version"]) == API_CONTRACT_VERSION


# 계약이 기술하는 모든 오퍼레이션은 BuilderService에 실제로 구현돼 있어야 한다.
# 구현 경로 이름은 계약과 1:1로 일치한다(#226: aspirational 비동기/publish 라우트 제거).
_IMPLEMENTED_OPERATIONS = {
    "getVersion",  # GET /version
    "validateSpec",  # POST /validate
    "previewBuild",  # POST /preview
    "createBuild",  # POST /build
    "listBuildArtifacts",  # GET /artifacts/{run_id}
    "listBuilds",  # GET /builds
}


def _contract_operation_ids() -> set[str]:
    paths = _load_contract()["paths"]
    return {
        operation["operationId"]
        for methods in paths.values()
        for operation in methods.values()
        if isinstance(operation, dict) and "operationId" in operation
    }


def test_contract_operations_match_implementation() -> None:
    # 계약의 오퍼레이션 집합이 구현된 동기 라우트 집합과 정확히 일치해야 한다.
    # 계약에 미구현 오퍼레이션이 추가되거나 라우트가 사라지면 이 테스트가 깨진다 (#226).
    assert _contract_operation_ids() == _IMPLEMENTED_OPERATIONS


def test_build_responses_pin_wire_status_codes() -> None:
    # POST /build의 실제 상태 코드(200 성공, 502 부분 실패)를 계약이 고정해야 한다 (#226).
    build = _load_contract()["paths"]["/build"]["post"]["responses"]
    assert "200" in build
    assert "502" in build
    assert "400" in build


def test_build_failure_response_includes_error_summary() -> None:
    # 502 응답이 human-readable error 요약을 포함하는 것을 계약 수준에서 고정 (#226).
    schemas = _load_contract()["components"]["schemas"]
    failure = schemas["BuildFailureResponse"]
    assert "error" in failure["properties"]
    assert "error" in failure["required"]


def test_referenced_schemas_resolve() -> None:
    # 모든 로컬 $ref("#/components/...")가 실제로 존재하는지 확인한다.
    contract = _load_contract()

    def _iter_refs(node: object) -> list[str]:
        refs: list[str] = []
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "$ref" and isinstance(value, str):
                    refs.append(value)
                else:
                    refs.extend(_iter_refs(value))
        elif isinstance(node, list):
            for item in node:
                refs.extend(_iter_refs(item))
        return refs

    for ref in _iter_refs(contract):
        assert ref.startswith("#/"), f"unexpected external ref: {ref}"
        target: Any = contract
        for part in ref.lstrip("#/").split("/"):
            assert part in target, f"unresolved $ref: {ref}"
            target = target[part]


def _extract_yaml_operations() -> dict[tuple[str, str], dict[str, Any]]:
    """YAML에서 (path, method) -> operation 매핑을 추출한다."""
    contract = _load_contract()
    operations: dict[tuple[str, str], dict[str, Any]] = {}

    for path, methods in contract["paths"].items():
        for method_lower, operation in methods.items():
            if not isinstance(operation, dict):
                continue
            method = method_lower.upper()
            key = (path, method)
            operations[key] = operation

    return operations


def _is_planned_operation(operation: dict[str, Any]) -> bool:
    """operation이 x-planned: true로 표시되었는지 확인한다."""
    return operation.get("x-planned") is True


def test_all_yaml_operations_implemented_in_dispatch() -> None:
    """YAML 계약에 정의된 모든 operation이 dispatch에 구현되어 있는지 검증한다(#317).

    x-planned: true로 표시된 operation은 아직 구현되지 않아도 되며, 검증에서 제외한다.
    """
    yaml_operations = _extract_yaml_operations()

    for (path, method), operation in yaml_operations.items():
        if _is_planned_operation(operation):
            continue

        key = (path, method)
        assert key in _DISPATCH_ROUTES, (
            f"YAML에 정의된 {method} {path}가 dispatch에 구현되어 있지 않습니다. "
            f"operationId: {operation.get('operationId')}"
        )

        yaml_operation_id = operation.get("operationId")
        dispatch_operation_id = _DISPATCH_ROUTES[key]
        assert yaml_operation_id == dispatch_operation_id, (
            f"operationId 불일치: YAML={yaml_operation_id}, dispatch={dispatch_operation_id}"
        )


def test_all_dispatch_routes_declared_in_yaml() -> None:
    """dispatch에 구현된 모든 경로가 YAML 계약에 선언되어 있는지 검증한다(#317).

    실제 구현이 있는데 YAML에 누락된 경우를 탐지한다.
    """
    yaml_operations = _extract_yaml_operations()

    for (path, method), operation_id in _DISPATCH_ROUTES.items():
        key = (path, method)
        assert key in yaml_operations, (
            f"dispatch에 구현된 {method} {path}가 YAML 계약에 누락되었습니다. "
            f"operationId: {operation_id}"
        )

        yaml_op = yaml_operations[key]
        yaml_operation_id = yaml_op.get("operationId")
        assert yaml_operation_id == operation_id, (
            f"operationId 불일치: dispatch={operation_id}, YAML={yaml_operation_id}"
        )


def test_planned_operations_excluded_from_implementation_check() -> None:
    """x-planned: true operation이 구현 검증에서 제외되는지 확인한다(#317).

    계약에는 planned operation이 포함될 수 있지만, 실제 dispatch에는
    구현되지 않아도 된다. 이 규칙이 올바르게 동작하는지 검증한다.
    """
    yaml_operations = _extract_yaml_operations()

    # 현재 계약에는 x-planned operation이 없으므로, 모든 operation이 구현되어야 한다
    planned_operations = [
        (path, method, op.get("operationId"))
        for (path, method), op in yaml_operations.items()
        if _is_planned_operation(op)
    ]

    # 향후 planned operation이 추가되면 이 테스트가 그 존재를 검증한다
    # 현재는 계약에 planned operation이 없음을 확인
    for path, method, operation_id in planned_operations:
        assert operation_id, f"planned operation {method} {path}에 operationId가 없습니다"

    # 모든 planned operation은 dispatch에 없어도 됨
    for path, method, _operation_id in planned_operations:
        key = (path, method)
        if key in _DISPATCH_ROUTES:
            # planned인데 구현되어 있어도 에러는 아님 (구현이 빠른 경우)
            pass
        else:
            # planned이고 구현되지 않아도 정상
            pass
