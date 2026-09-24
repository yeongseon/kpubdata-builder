"""Integration tests for the verify module.

These tests verify that the verify engine correctly interacts with
real kpubdata spec definitions — no mocking of kpubdata internals.

Prerequisite: verify module must be available (feat/verify-command branch or merged).
"""

from __future__ import annotations

import pytest
from kpubdata.core.spec import SpecDefinition, discover_specs, find_spec

try:
    from kpubdata_builder.verify.models import CheckName, DatasetStatus, VerifyResult
    from kpubdata_builder.verify.runner import _check_endpoint, _fill_skipped, verify_dataset
    from kpubdata_builder.verify.schema_hash import schema_diff, schema_hash

    _HAS_VERIFY = True
except ImportError:
    _HAS_VERIFY = False

pytestmark = pytest.mark.skipif(not _HAS_VERIFY, reason="verify module not available")


class TestVerifyWithRealSpecs:
    """Verify engine integration with real kpubdata spec definitions.

    ``network`` 로 표시된 두 테스트는 실제 data.go.kr 을 호출한다. 기본 실행에서
    제외한다 — 제3자 서비스가 응답하지 않거나 러너 위치에 따라 다른 상태 코드를
    주면, 우리 코드와 무관하게 CI 가 빨개진다. 어떤 상태 코드를 reachable 로 볼지
    같은 규칙은 ``tests/unit/test_verify_runner.py`` 가 fake 로 남김없이 고정한다.
    실제 호출이 필요하면 ``pytest -m network`` 로 실행한다.
    """

    def test_discover_specs_returns_nonempty(self) -> None:
        specs = discover_specs()
        assert len(specs) > 0
        assert all(isinstance(s, SpecDefinition) for s in specs)

    def test_find_spec_apt_trade(self) -> None:
        spec = find_spec("datago.apt_trade")
        assert spec is not None
        assert spec.id == "datago.apt_trade"
        assert spec.endpoint.base_url != ""
        assert spec.auth.type == "query_param"

    @pytest.mark.network
    def test_check_endpoint_returns_a_verdict_for_a_real_spec(self) -> None:
        """실제 spec으로 endpoint 검사가 판정을 낸다. (network)

        살아있는 제3자 endpoint의 응답을 단언하지 않는다 — data.go.kr이 러너 위치나
        시점에 따라 다른 상태 코드를 주면 이 테스트가 코드와 무관하게 깨진다. 어떤
        상태 코드를 reachable로 볼지는 fake로 고정한 단위 테스트
        (``tests/unit/test_verify_runner.py``)가 남김없이 다룬다. 여기서는 실제
        spec을 읽어 검사가 판정과 근거를 낸다는 것만 확인한다.
        """
        spec = find_spec("datago.apt_trade")
        assert spec is not None
        result = _check_endpoint(spec)
        assert result.name == CheckName.ENDPOINT
        assert isinstance(result.passed, bool)
        # 실패했다면 왜인지 말해야 한다 — 통과한 경우에는 latency가 기록된다.
        assert result.passed or result.detail

    @pytest.mark.network
    def test_verify_without_key_returns_non_healthy(self) -> None:
        """API 키 없이 verify하면 HEALTHY가 아닌 상태가 된다."""
        spec = find_spec("datago.apt_trade")
        assert spec is not None
        result = verify_dataset(spec, api_key="INVALID_TEST_KEY_12345", page_size=1)
        assert isinstance(result, VerifyResult)
        assert result.dataset_id == "datago.apt_trade"
        assert result.status != DatasetStatus.HEALTHY

    def test_fill_skipped_fills_remaining(self) -> None:
        from kpubdata_builder.verify.models import CheckResult

        result = VerifyResult(dataset_id="test.skip", status=DatasetStatus.BROKEN_ENDPOINT)
        result.checks.append(CheckResult(CheckName.ENDPOINT, passed=False, detail="unreachable"))
        _fill_skipped(result, after=CheckName.ENDPOINT)
        skipped = [c for c in result.checks if c.detail == "skipped"]
        assert len(skipped) == 5

    def test_result_to_dict_json_serializable(self) -> None:
        import json

        spec = find_spec("datago.apt_trade")
        assert spec is not None
        result = verify_dataset(spec, api_key="INVALID_KEY", page_size=1)
        serialized = json.dumps(result.to_dict(), ensure_ascii=False)
        assert "datago.apt_trade" in serialized

    def test_format_report_readable(self) -> None:
        spec = find_spec("datago.apt_trade")
        assert spec is not None
        result = verify_dataset(spec, api_key="INVALID_KEY", page_size=1)
        report = result.format_report()
        assert "datago.apt_trade" in report


class TestSchemaHashIntegration:
    def test_hash_with_realistic_records(self) -> None:
        items = [
            {"aptNm": "래미안", "dealAmount": "95,000", "floor": 12, "area": 84.97},
            {"aptNm": "자이", "dealAmount": "88,000", "floor": 8, "area": 59.96},
        ]
        h1 = schema_hash(items)
        h2 = schema_hash(items)
        assert h1 == h2 and len(h1) == 16

    def test_diff_detects_field_addition(self) -> None:
        old = [{"aptNm": "래미안", "floor": 12}]
        new = [{"aptNm": "래미안", "floor": 12, "floorType": "고층"}]
        diff = schema_diff(old, new)
        assert diff["added"] == ["floorType"]

    def test_diff_detects_removal(self) -> None:
        old = [{"aptNm": "래미안", "oldField": "x", "floor": 12}]
        new = [{"aptNm": "래미안", "floor": 12}]
        diff = schema_diff(old, new)
        assert diff["removed"] == ["oldField"]
