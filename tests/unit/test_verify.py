"""Tests for the verify module."""

from __future__ import annotations

from kpubdata_builder.verify.models import CheckName, CheckResult, DatasetStatus, VerifyResult
from kpubdata_builder.verify.schema_hash import schema_diff, schema_hash


class TestSchemaHash:
    """schema_hash and schema_diff tests."""

    def test_stable_hash(self) -> None:
        items = [{"name": "a", "value": 1}]
        h1 = schema_hash(items)
        h2 = schema_hash(items)
        assert h1 == h2
        assert len(h1) == 16

    def test_different_values_same_hash(self) -> None:
        items1 = [{"name": "alice", "value": 100}]
        items2 = [{"name": "bob", "value": 200}]
        assert schema_hash(items1) == schema_hash(items2)

    def test_different_fields_different_hash(self) -> None:
        items1 = [{"name": "a", "value": 1}]
        items2 = [{"name": "a", "value": 1, "extra": "x"}]
        assert schema_hash(items1) != schema_hash(items2)

    def test_type_change_different_hash(self) -> None:
        items1 = [{"value": 1}]
        items2 = [{"value": "1"}]
        assert schema_hash(items1) != schema_hash(items2)

    def test_empty_items(self) -> None:
        h = schema_hash([])
        assert isinstance(h, str)
        assert len(h) == 16

    def test_merges_keys_across_items(self) -> None:
        items = [{"a": 1}, {"b": "x"}]
        h = schema_hash(items)
        assert h == schema_hash([{"a": 1, "b": "x"}])


class TestSchemaDiff:
    """schema_diff tests."""

    def test_no_diff(self) -> None:
        items = [{"name": "a", "value": 1}]
        diff = schema_diff(items, items)
        assert diff == {"added": [], "removed": [], "type_changed": []}

    def test_added_field(self) -> None:
        old = [{"name": "a"}]
        new = [{"name": "a", "floor_type": "B1"}]
        diff = schema_diff(old, new)
        assert diff["added"] == ["floor_type"]
        assert diff["removed"] == []

    def test_removed_field(self) -> None:
        old = [{"name": "a", "old_field": 1}]
        new = [{"name": "a"}]
        diff = schema_diff(old, new)
        assert diff["removed"] == ["old_field"]

    def test_type_changed(self) -> None:
        old = [{"value": 1}]
        new = [{"value": "1"}]
        diff = schema_diff(old, new)
        assert diff["type_changed"] == ["value"]


class TestCheckResult:
    def test_passed(self) -> None:
        c = CheckResult(CheckName.ENDPOINT, passed=True)
        assert c.passed
        assert c.detail == ""

    def test_failed_with_detail(self) -> None:
        c = CheckResult(CheckName.AUTH, passed=False, detail="403 forbidden")
        assert not c.passed
        assert "403" in c.detail


class TestVerifyResult:
    def test_healthy_result(self) -> None:
        r = VerifyResult(dataset_id="datago.apt_trade", status=DatasetStatus.HEALTHY)
        assert r.passed
        assert r.status == DatasetStatus.HEALTHY

    def test_to_dict(self) -> None:
        r = VerifyResult(
            dataset_id="datago.apt_trade",
            status=DatasetStatus.HEALTHY,
            checks=[
                CheckResult(CheckName.ENDPOINT, passed=True, latency_ms=50.0),
                CheckResult(CheckName.AUTH, passed=True),
            ],
            records_tested=10,
            total_latency_ms=412.3,
            schema_hash="abc123def456",
        )
        d = r.to_dict()
        assert d["dataset"] == "datago.apt_trade"
        assert d["status"] == "HEALTHY"
        assert len(d["checks"]) == 2
        assert d["checks"][0]["status"] == "passed"
        assert d["records_tested"] == 10
        assert d["schema_hash"] == "abc123def456"

    def test_to_dict_schema_changed(self) -> None:
        r = VerifyResult(
            dataset_id="datago.apt_trade",
            status=DatasetStatus.SCHEMA_CHANGED,
            schema_hash="new123",
            previous_schema_hash="old456",
        )
        d = r.to_dict()
        assert d["previous_schema_hash"] == "old456"

    def test_format_report(self) -> None:
        r = VerifyResult(
            dataset_id="datago.apt_trade",
            status=DatasetStatus.HEALTHY,
            checks=[
                CheckResult(CheckName.ENDPOINT, passed=True),
                CheckResult(CheckName.AUTH, passed=True),
            ],
            records_tested=10,
            total_latency_ms=412.0,
        )
        report = r.format_report()
        assert "datago.apt_trade" in report
        assert "HEALTHY" in report
        assert "10" in report

    def test_needs_application_not_passed(self) -> None:
        r = VerifyResult(
            dataset_id="datago.rh_trade",
            status=DatasetStatus.NEEDS_APPLICATION,
        )
        assert not r.passed


class TestDatasetStatus:
    def test_all_statuses(self) -> None:
        expected = {
            "HEALTHY", "NEEDS_APPLICATION", "WAITING_APPROVAL",
            "INVALID_KEY", "RATE_LIMITED", "BROKEN_ENDPOINT",
            "SCHEMA_CHANGED", "SKIPPED",
        }
        actual = {s.value for s in DatasetStatus}
        assert actual == expected
