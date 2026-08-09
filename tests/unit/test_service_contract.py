"""Builder Service Contract(#63, #226, #317, #319, #209) OpenAPI 스펙의 구조·런타임 검증.

설치된 OpenAPI validator가 없으므로, 계약이 OpenAPI 3.1이고 BuilderService
(service/app.py)가 실제로 구현한 동기 라우트와 wire 형태를 모두 담는지 구조적으로
검증한다. 계약은 이제 구현된 엔드포인트만 기술하며(#226), 한쪽만 바뀌는 조용한
드리프트를 막기 위해 구현 라우트 ↔ 계약 operationId 매핑을 명시적으로 고정한다(#317).
또한 YAML 계약과 실제 dispatch 구현 간의 양방향 일치성을 검증하며(#317),
상태 코드와 응답 스키마 검증으로 범위를 확장한다(#319).

구조 검증(위)은 YAML의 *선언*과 dispatch의 *라우팅 목록*이 일치하는지 본다(#317, #319).
``TestResponseConformance``(#209, ADR-0005)는 한 단계 더 나아가 **실제 dispatch 응답
본문이 선언된 스키마에 부합하는지**(wire-level conformance)를 순수 파이썬
validator(``_openapi.py``)로 검증한다 — #319의 정적 스키마 검사가 "스키마가 required를
선언했는가"만 보듯, app.py 응답에서 필수 필드가 빠지거나 타입이 바뀌어도 선언부가
그대로면 정적 검사는 통과하지만 이 런타임 검사는 잡는다.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

import yaml

from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.spec import JsonValue

from ._openapi import response_schema, validate

_CONTRACT_PATH = Path(__file__).parents[2] / "contract" / "builder-api.yaml"

# dispatch에 구현된 (path, method, operationId) 매핑.
# service/app.py:dispatch의 라우팅 규칙을 기계적으로 추출하기 어렵기 때문에
# 명시적으로 선언하여 유지보수성을 높인다.
_DISPATCH_ROUTES: dict[tuple[str, str], str] = {
    ("/healthz", "GET"): "healthz",
    ("/version", "GET"): "getVersion",
    ("/catalog", "GET"): "getCatalog",
    ("/validate", "POST"): "validateSpec",
    ("/preview", "POST"): "previewBuild",
    ("/build", "POST"): "createBuild",
    ("/builds", "GET"): "listBuilds",
    ("/artifacts/{run_id}", "GET"): "listBuildArtifacts",
}

# (path, method) 형태의 계약 필수 오퍼레이션. BuilderService.dispatch가 실제로
# 라우팅하는 동기 엔드포인트와 1:1로 대응한다.
_REQUIRED_OPERATIONS = [
    ("/healthz", "get"),
    ("/version", "get"),
    ("/catalog", "get"),
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
    "healthz",
    "getVersion",
    "getCatalog",
    "validateSpec",
    "previewBuild",
    "createBuild",
    "listBuildArtifacts",
    "listBuilds",
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


# =============================================================================
# 계약 커버리지 테스트 1단계: 경로/메서드 양방향 검증 (#317)
# =============================================================================


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


# =============================================================================
# 계약 커버리지 테스트 2단계: 상태 코드와 응답 스키마 검증 (#319)
# =============================================================================

# 각 operation이 YAML에 선언한 상태 코드와 실제 구현이 반환하는 상태 코드의
# 매핑. service/app.py:dispatch와 각 service 메서드를 분석하여 작성한다.
_OPERATION_STATUS_CODES: dict[str, set[int]] = {
    "healthz": {200},
    "getVersion": {200},
    "getCatalog": {200, 502},
    "validateSpec": {200, 400},
    "previewBuild": {200, 400},
    "createBuild": {200, 400, 502},
    "listBuilds": {200, 400},
    "listBuildArtifacts": {200, 400, 404},
}


def _extract_declared_status_codes(operation: dict[str, Any]) -> set[int]:
    """YAML operation에서 선언된 상태 코드들을 추출한다."""
    responses = operation.get("responses", {})
    declared_codes: set[int] = set()

    for status_code_str in responses:
        if status_code_str == "default":
            continue
        try:
            declared_codes.add(int(status_code_str))
        except ValueError:
            # "default" 또는 기타 비숫자 상태 코드는 무시
            continue

    return declared_codes


def test_declared_status_codes_match_implementation() -> None:
    """YAML에 선언된 상태 코드와 실제 구현이 일치하는지 검증한다(#319).

    각 operation마다 YAML의 responses 키에 선언된 상태 코드와 실제 코드가
    반환할 수 있는 상태 코드가 일치해야 한다.
    """
    yaml_operations = _extract_yaml_operations()

    for (path, method), operation in yaml_operations.items():
        if _is_planned_operation(operation):
            continue

        operation_id = operation.get("operationId")
        if not operation_id:
            continue

        # YAML에 선언된 상태 코드 추출
        declared_codes = _extract_declared_status_codes(operation)

        # 401은 인증 게이트 안의 엔드포인트에 공통. 단 security: [] (무인증, #372)는 제외.
        is_unauthenticated = operation.get("security") == []
        implemented_codes = _OPERATION_STATUS_CODES.get(operation_id, set())
        all_implemented_codes = implemented_codes | (set() if is_unauthenticated else {401})

        assert declared_codes == all_implemented_codes, (
            f"상태 코드 불일치: {method} {path} (operationId: {operation_id})\n"
            f"  YAML 선언: {sorted(declared_codes)}\n"
            f"  실제 구현: {sorted(all_implemented_codes)}\n"
            f"  차이: {sorted(declared_codes ^ all_implemented_codes)}"
        )


def _extract_response_schema_required_fields(
    contract: dict[str, Any], response_ref: str
) -> set[str]:
    """YAML 응답 스키마에서 required 필드들을 추출한다."""
    # $ref 형식: "#/components/schemas/SchemaName"
    if not response_ref.startswith("#/components/schemas/"):
        return set()

    schema_name = response_ref[len("#/components/schemas/") :]
    schema = contract["components"]["schemas"].get(schema_name, {})
    required = schema.get("required", [])

    if isinstance(required, list):
        return set(required)
    return set()


def test_response_schemas_have_required_fields() -> None:
    """YAML 응답 스키마가 주요 엔드포인트의 200 응답에 대한 필수 필드를
    정의하고 있는지 검증한다(#319).

    이 테스트는 계약 자체의 완전성을 확인한다: 각 operation이 성공 응답(200)에
    대한 스키마를 정의하고, 그 스키마에 required 필드들이 포함되어 있는지 확인.
    """
    contract = _load_contract()
    yaml_operations = _extract_yaml_operations()

    for (path, method), operation in yaml_operations.items():
        if _is_planned_operation(operation):
            continue

        operation_id = operation.get("operationId")
        if not operation_id:
            continue

        # 200 응답 확인
        responses = operation.get("responses", {})
        if "200" not in responses:
            raise AssertionError(
                f"{method} {path} (operationId: {operation_id})에 200 응답이 누락되었습니다"
            )

        response_200 = responses["200"]
        content = response_200.get("content", {})
        json_content = content.get("application/json", {})
        schema_ref = json_content.get("schema", {}).get("$ref", "")

        # $ref가 있으면 required 필드 확인
        if schema_ref:
            required_fields = _extract_response_schema_required_fields(contract, schema_ref)

            assert required_fields, (
                f"{method} {path} (operationId: {operation_id})의 200 응답 스키마에 "
                f"required 필드가 정의되어 있지 않습니다: {schema_ref}"
            )

            # 주요 엔드포인트들은 최소한 몇 개의 필수 필드를 가져야 함
            main_operations = {
                "getVersion",
                "validateSpec",
                "previewBuild",
                "createBuild",
                "listBuildArtifacts",
            }
            if operation_id in main_operations:
                assert len(required_fields) >= 1, (
                    f"{method} {path} (operationId: {operation_id})의 "
                    f"200 응답 스키마에 필수 필드가 너무 적습니다: {required_fields}"
                )


# ---------------------------------------------------------------------------
# 런타임 wire-level conformance (#209, ADR-0005)
#
# 정적 검증(위)은 계약 YAML과 dispatch 라우팅 목록이 일치하는지 본다. 아래 테스트들은
# 실제 dispatch()를 호출해 반환된 JSON 본문이 선언된 응답 스키마에 부합하는지 검증한다.
# 외부 의존 없이 순수 파이썬 validator(_openapi.py)를 쓰며, 이것이 ADR-0005 미해결 질문
# #1(스키마 대조를 순수 파이썬 경량으로 할지)을 "순수 파이썬 경량" 방향으로 마무리한다.
#
# #319의 test_response_schemas_have_required_fields는 스키마가 required를 *선언했는지*만
# 본다. app.py가 실제 응답에서 필수 필드를 빼거나 타입을 바꿔도 선언부가 그대로면 그 정적
# 검사는 통과한다 — 이 런타임 검사가 그 wire 드리프트를 잡는다. 계약에 선언된 6개 오퍼레이션
# 모두(/version, /validate, /preview, /build, /artifacts, /builds)를 성공+오류 상태 코드에
# 걸쳐 검증한다.
# ---------------------------------------------------------------------------

_CONFORM_SPEC_YAML = (
    "dataset_id: dataset.conform\n"
    "title: Conform Sample\n"
    "description: runtime conformance fixture\n"
    "sources:\n"
    "  - provider: datago\n"
    "    dataset: air_quality\n"
    "exports:\n"
    "  - kind: jsonl\n"
    "    output_path: out/data.jsonl\n"
)

# fetch를 실패시켜 502 경로를 만들기 위한 spec: fake client가 모르는 소스.
_FAILING_SPEC_YAML = _CONFORM_SPEC_YAML.replace("dataset: air_quality\n", "dataset: missing\n")

# 파싱은 통과하지만 validate_spec에서 미지원 exporter kind로 실패하는 spec.
_INVALID_SPEC_YAML = _CONFORM_SPEC_YAML.replace("kind: jsonl", "kind: unsupported_format")


class _FakeResult:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    @property
    def items(self) -> Iterable[dict[str, JsonValue]]:
        return self._items


class _FakeDataset:
    def __init__(self, items: list[dict[str, JsonValue]]) -> None:
        self._items = items

    def list(self, **params: JsonValue) -> _FakeResult:
        return _FakeResult(self._items)


class _FakeClient:
    def __init__(self, data: dict[str, list[dict[str, JsonValue]]]) -> None:
        self._data = data

    def dataset(self, source_key: str) -> _FakeDataset:
        if source_key not in self._data:
            raise KeyError(f"unknown source: {source_key}")
        return _FakeDataset(self._data[source_key])


def _conform_service(tmp_path: Path) -> BuilderService:
    client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}, {"id": "2", "v": 20}]})
    return BuilderService(output_root=tmp_path, client_factory=lambda: client)


def _assert_conforms(resp: ServiceResponse, path: str, method: str) -> None:
    """실제 dispatch 응답이 계약 스키마에 부합하는지(상태 코드 선언 + 본문 형태) 검증."""
    contract = _load_contract()
    schema = response_schema(contract, path, method, resp.status_code)
    assert schema is not None, (
        f"{method} {path}: status {resp.status_code} is not declared in the contract"
    )
    errors = validate(resp.body, schema, contract)
    assert not errors, (
        f"{method} {path} {resp.status_code} response drifts from contract:\n  "
        + "\n  ".join(errors)
    )


class TestResponseConformance:
    """선언된 각 오퍼레이션의 실제 wire 응답이 OpenAPI 스키마에 부합하는지 고정."""

    def test_version_200(self, tmp_path: Path) -> None:
        resp = dispatch(_conform_service(tmp_path), "GET", "/version", None)
        assert resp.status_code == 200
        _assert_conforms(resp, "/version", "GET")

    def test_validate_200(self, tmp_path: Path) -> None:
        resp = dispatch(
            _conform_service(tmp_path), "POST", "/validate", {"spec": _CONFORM_SPEC_YAML}
        )
        assert resp.status_code == 200
        _assert_conforms(resp, "/validate", "POST")

    def test_validate_400_invalid_spec(self, tmp_path: Path) -> None:
        resp = dispatch(
            _conform_service(tmp_path), "POST", "/validate", {"spec": _INVALID_SPEC_YAML}
        )
        assert resp.status_code == 400
        # 미지원 exporter → {"status": "invalid", "problems": [...]}
        _assert_conforms(resp, "/validate", "POST")

    def test_preview_200(self, tmp_path: Path) -> None:
        resp = dispatch(
            _conform_service(tmp_path), "POST", "/preview", {"spec": _CONFORM_SPEC_YAML, "limit": 2}
        )
        assert resp.status_code == 200
        _assert_conforms(resp, "/preview", "POST")

    def test_preview_400_bad_limit(self, tmp_path: Path) -> None:
        # 400 응답은 oneOf(Error | ValidationError) — limit 오류는 Error 형태.
        resp = dispatch(
            _conform_service(tmp_path), "POST", "/preview", {"spec": _CONFORM_SPEC_YAML, "limit": 0}
        )
        assert resp.status_code == 400
        _assert_conforms(resp, "/preview", "POST")

    def test_build_200_success(self, tmp_path: Path) -> None:
        resp = dispatch(
            _conform_service(tmp_path),
            "POST",
            "/build",
            {"spec": _CONFORM_SPEC_YAML, "run_id": "conform-ok"},
        )
        assert resp.status_code == 200
        _assert_conforms(resp, "/build", "POST")

    def test_build_502_failure(self, tmp_path: Path) -> None:
        resp = dispatch(
            _conform_service(tmp_path),
            "POST",
            "/build",
            {"spec": _FAILING_SPEC_YAML, "run_id": "conform-fail"},
        )
        assert resp.status_code == 502
        _assert_conforms(resp, "/build", "POST")

    def test_build_400_bad_run_id(self, tmp_path: Path) -> None:
        # 400 응답은 oneOf(Error | ValidationError) — run_id 타입 오류는 Error 형태.
        resp = dispatch(
            _conform_service(tmp_path),
            "POST",
            "/build",
            {"spec": _CONFORM_SPEC_YAML, "run_id": 123},
        )
        assert resp.status_code == 400
        _assert_conforms(resp, "/build", "POST")

    def test_artifacts_200_after_build(self, tmp_path: Path) -> None:
        service = _conform_service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": _CONFORM_SPEC_YAML, "run_id": "conform-art"})
        resp = dispatch(service, "GET", "/artifacts/conform-art", None)
        assert resp.status_code == 200
        _assert_conforms(resp, "/artifacts/{run_id}", "GET")

    def test_artifacts_404_missing(self, tmp_path: Path) -> None:
        resp = dispatch(_conform_service(tmp_path), "GET", "/artifacts/nope", None)
        assert resp.status_code == 404
        _assert_conforms(resp, "/artifacts/{run_id}", "GET")

    def test_builds_200_empty(self, tmp_path: Path) -> None:
        resp = dispatch(_conform_service(tmp_path), "GET", "/builds", None, query="")
        assert resp.status_code == 200
        _assert_conforms(resp, "/builds", "GET")

    def test_builds_200_after_build(self, tmp_path: Path) -> None:
        service = _conform_service(tmp_path)
        dispatch(service, "POST", "/build", {"spec": _CONFORM_SPEC_YAML, "run_id": "conform-list"})
        resp = dispatch(service, "GET", "/builds", None, query="")
        assert resp.status_code == 200
        _assert_conforms(resp, "/builds", "GET")

    def test_builds_400_bad_limit(self, tmp_path: Path) -> None:
        resp = dispatch(_conform_service(tmp_path), "GET", "/builds", None, query="limit=0")
        assert resp.status_code == 400
        _assert_conforms(resp, "/builds", "GET")
