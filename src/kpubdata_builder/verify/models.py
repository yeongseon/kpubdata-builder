"""Verification result models."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone


class DatasetStatus(str, enum.Enum):
    """Dataset health status after verification."""

    HEALTHY = "HEALTHY"
    NEEDS_APPLICATION = "NEEDS_APPLICATION"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    INVALID_KEY = "INVALID_KEY"
    RATE_LIMITED = "RATE_LIMITED"
    BROKEN_ENDPOINT = "BROKEN_ENDPOINT"
    SCHEMA_CHANGED = "SCHEMA_CHANGED"
    SKIPPED = "SKIPPED"


class CheckName(str, enum.Enum):
    """Individual verification check names."""

    ENDPOINT = "endpoint"
    AUTH = "auth"
    RESPONSE = "response"
    PARSER = "parser"
    PAGINATION = "pagination"
    SCHEMA = "schema"


@dataclass(slots=True)
class CheckResult:
    """Result of a single verification check.

    ``skipped`` is distinct from a failure. When an early stage fails, the later
    stages never run — reporting those as ``failed`` makes one auth problem look
    like six broken stages, and machine consumers cannot tell the two apart.
    """

    name: CheckName
    passed: bool
    detail: str = ""
    latency_ms: float | None = None
    skipped: bool = False

    @property
    def state(self) -> str:
        """The check's outcome as it appears in serialized output."""
        if self.skipped:
            return "skipped"
        return "passed" if self.passed else "failed"


@dataclass(slots=True)
class VerifyResult:
    """Complete verification result for one dataset."""

    dataset_id: str
    status: DatasetStatus
    checks: list[CheckResult] = field(default_factory=list)
    records_tested: int = 0
    total_latency_ms: float = 0.0
    schema_hash: str | None = None
    previous_schema_hash: str | None = None
    verified_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.status == DatasetStatus.HEALTHY

    def to_dict(self) -> dict[str, object]:
        """Serialize to a machine-readable dict."""
        checks_list: list[dict[str, object]] = []
        for c in self.checks:
            entry: dict[str, object] = {
                "name": c.name.value,
                "status": c.state,
            }
            if c.detail:
                entry["detail"] = c.detail
            if c.latency_ms is not None:
                entry["latency_ms"] = round(c.latency_ms, 1)
            checks_list.append(entry)

        result: dict[str, object] = {
            "dataset": self.dataset_id,
            "status": self.status.value,
            "checks": checks_list,
            "records_tested": self.records_tested,
            "total_latency_ms": round(self.total_latency_ms, 1),
            "verified_at": self.verified_at.strftime("%Y-%m-%d"),
        }
        if self.schema_hash:
            result["schema_hash"] = self.schema_hash
        if self.previous_schema_hash and self.previous_schema_hash != self.schema_hash:
            result["previous_schema_hash"] = self.previous_schema_hash
        if self.error:
            result["error"] = self.error
        return result

    def format_report(self) -> str:
        """Format a human-readable verification report."""
        lines = [
            "",
            "KPubData Verification",
            "",
            f"  Dataset        {self.dataset_id}",
        ]
        for check in self.checks:
            label = check.name.value.capitalize().ljust(14)
            icon = "skip" if check.skipped else ("pass" if check.passed else "FAIL")
            line = f"  {label} {icon}"
            if check.detail:
                line += f"  {check.detail}"
            lines.append(line)

        lines.append("")
        lines.append(f"  Records tested {self.records_tested}")
        lines.append(f"  Latency        {self.total_latency_ms:.0f} ms")
        lines.append(f"  Verified       {self.verified_at.strftime('%Y-%m-%d')}")
        lines.append("")
        lines.append(f"  Result: {self.status.value}")
        lines.append("")
        return "\n".join(lines)
