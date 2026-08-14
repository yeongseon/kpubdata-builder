from __future__ import annotations

from ..manifest import QualityCheckResult, SourceQualityResult
from ..spec.models import QualityPolicy
from ..tabular import TableStatistics


def _check_status(is_pass: bool) -> str:
    return "pass" if is_pass else "warn"


def _overall_status(checks: tuple[QualityCheckResult, ...]) -> str:
    return "warn" if any(check.status == "warn" for check in checks) else "pass"


def evaluate_quality(
    source_key: str, policy: QualityPolicy, statistics: TableStatistics
) -> SourceQualityResult:
    checks: list[QualityCheckResult] = []
    if policy.max_duplicate_rate is not None:
        passed = statistics.duplicate_rate <= policy.max_duplicate_rate
        checks.append(
            QualityCheckResult(
                name="max_duplicate_rate",
                status=_check_status(passed),
                observed=statistics.duplicate_rate,
                threshold=policy.max_duplicate_rate,
                message=(
                    f"중복 행 비율 {statistics.duplicate_rate:.4f} <= 임계 "
                    f"{policy.max_duplicate_rate:.4f}"
                    if passed
                    else f"중복 행 비율 {statistics.duplicate_rate:.4f} > 임계 "
                    f"{policy.max_duplicate_rate:.4f}"
                ),
            )
        )

    if policy.min_rows is not None:
        passed = statistics.row_count >= policy.min_rows
        checks.append(
            QualityCheckResult(
                name="min_rows",
                status=_check_status(passed),
                observed=statistics.row_count,
                threshold=policy.min_rows,
                message=(
                    f"행 수 {statistics.row_count} >= 최소 {policy.min_rows}"
                    if passed
                    else f"행 수 {statistics.row_count} < 최소 {policy.min_rows}"
                ),
            )
        )

    for column, max_ratio in sorted(policy.max_null_ratio.items()):
        observed_nulls = statistics.null_counts.get(column, 0)
        observed_ratio = observed_nulls / statistics.row_count if statistics.row_count > 0 else 0.0
        passed = observed_ratio <= max_ratio
        checks.append(
            QualityCheckResult(
                name="max_null_ratio",
                status=_check_status(passed),
                observed=observed_ratio,
                threshold=max_ratio,
                column=column,
                message=(
                    f"컬럼 {column} null 비율 {observed_ratio:.4f} <= 임계 {max_ratio:.4f}"
                    if passed
                    else f"컬럼 {column} null 비율 {observed_ratio:.4f} > 임계 {max_ratio:.4f}"
                ),
            )
        )

    check_tuple = tuple(checks)
    return SourceQualityResult(
        source_key=source_key,
        status=_overall_status(check_tuple),
        checks=check_tuple,
    )


__all__ = ["evaluate_quality"]
