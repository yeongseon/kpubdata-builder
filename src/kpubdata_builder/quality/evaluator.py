"""Quality/Schema 구조화 evaluator (#486).

Preview와 Build가 공유하는 단일 순수 함수 ``evaluate_quality``를 제공한다. 같은
SilverDataset과 같은 설정(policy/required_columns/column_dtypes)에 대해서는
Preview든 Build든 항상 동일한 PASS/WARN/FAIL 결과를 반환한다 — 두 경로가 서로
다른 판정 구현을 갖지 않는다.

평가하는 것:
    - schema 계약: required column 존재 여부, 선언된 dtype 일치 여부.
    - QualityPolicy: max_duplicate_rate, max_null_ratio(컬럼별), min_rows,
      range(#486 typed rule), compare_columns(#486 typed rule, 제한된 operator만).

원칙 (이슈 #486):
    - rule 미설정 또는 평가 불가능(컬럼 없음, denominator 0 등)이면 그 check는
      결과 목록에서 제외한다 — PASS로 가장하지 않는다.
    - threshold 비교는 결정적이다. LLM/AI 해석은 이 모듈에 관여하지 않는다.
    - range/compare_columns는 Polars의 vectorized 연산만 사용한다(자유형 eval 금지).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import polars as pl

from ..spec.models import CompareColumnsRule, JsonValue, QualityPolicy, RangeRule
from ..stages.silver.models import SilverDataset
from ..tabular.polars_helpers import DtypeSpec, _resolve_dtype
from .models import QualityCheckResult, QualityStatus

_COMPARE_OPERATORS: dict[str, Callable[[pl.Expr, pl.Expr], pl.Expr]] = {
    "eq": lambda left, right: left == right,
    "ne": lambda left, right: left != right,
    "gt": lambda left, right: left > right,
    "gte": lambda left, right: left >= right,
    "lt": lambda left, right: left < right,
    "lte": lambda left, right: left <= right,
}


def _severity_status(violated: bool, severity: str) -> QualityStatus:
    if not violated:
        return "pass"
    return "fail" if severity == "fail" else "warn"


def _range_threshold(rule: RangeRule) -> dict[str, JsonValue]:
    """range 규칙의 원래 경계를 결과에 손실 없이 보존한다."""
    return {"min": rule.min, "max": rule.max}


def _compare_threshold(rule: CompareColumnsRule) -> dict[str, JsonValue]:
    """컬럼 비교의 연산자와 오른쪽 컬럼을 결과에 보존한다."""
    return {"operator": rule.operator, "right_column": rule.right}


def _schema_results(
    table: pl.DataFrame,
    *,
    source_key: str,
    required_columns: Sequence[str],
    column_dtypes: Mapping[str, DtypeSpec] | None,
) -> list[QualityCheckResult]:
    """required column/dtype 계약을 QualityCheckResult로 변환한다.

    required column이 없어서 dtype을 검사할 수 없으면 dtype check 자체를
    건너뛴다 — "required FAIL + dtype PASS" 같은 모순을 만들지 않는다.
    """
    results: list[QualityCheckResult] = []
    columns = set(table.columns)
    for col in required_columns:
        present = col in columns
        results.append(
            QualityCheckResult(
                source_key=source_key,
                category="schema",
                rule="required_column",
                column=col,
                status="pass" if present else "fail",
                actual=present,
                threshold=True,
            )
        )
    for col, expected_spec in (column_dtypes or {}).items():
        if col not in columns:
            continue  # 컬럼 자체가 없어 dtype을 검사할 수 없다 — PASS를 만들지 않는다.
        expected = _resolve_dtype(expected_spec)
        actual_dtype = table.schema[col]
        results.append(
            QualityCheckResult(
                source_key=source_key,
                category="schema",
                rule="dtype",
                column=col,
                status="pass" if actual_dtype == expected else "fail",
                actual=str(actual_dtype),
                threshold=str(expected),
            )
        )
    return results


def _duplicate_result(
    silver: SilverDataset, policy: QualityPolicy, *, source_key: str
) -> QualityCheckResult | None:
    if policy.max_duplicate_rate is None:
        return None
    actual = silver.statistics.duplicate_rate
    violated = actual > policy.max_duplicate_rate
    return QualityCheckResult(
        source_key=source_key,
        category="duplicate",
        rule="max_duplicate_rate",
        column=None,
        status=_severity_status(violated, policy.max_duplicate_rate_severity),
        actual=actual,
        threshold=policy.max_duplicate_rate,
        # 정확한 중복 행 수가 현재 통계(duplicate_rate만 보유)에 없다 — rate로부터
        # 임의 정수를 역산하지 않는다(#486).
        affected_rows=None,
        evaluated_rows=silver.statistics.row_count,
    )


def _null_ratio_results(
    silver: SilverDataset, policy: QualityPolicy, *, source_key: str
) -> list[QualityCheckResult]:
    results: list[QualityCheckResult] = []
    row_count = silver.statistics.row_count
    for col, max_ratio in policy.max_null_ratio.items():
        if row_count == 0:
            continue  # denominator 0 — ratio를 억지로 0/PASS로 만들지 않는다.
        null_count = silver.statistics.null_counts.get(col)
        if null_count is None:
            continue  # 통계에 없는 컬럼(존재하지 않음) — 평가 불가.
        actual = null_count / row_count
        severity = policy.max_null_ratio_severity.get(col, "warn")
        violated = actual > max_ratio
        results.append(
            QualityCheckResult(
                source_key=source_key,
                category="missing",
                rule="max_null_ratio",
                column=col,
                status=_severity_status(violated, severity),
                actual=actual,
                threshold=max_ratio,
                affected_rows=null_count,
                evaluated_rows=row_count,
            )
        )
    return results


def _min_rows_result(
    silver: SilverDataset, policy: QualityPolicy, *, source_key: str
) -> QualityCheckResult | None:
    if policy.min_rows is None:
        return None
    actual = silver.statistics.row_count
    violated = actual < policy.min_rows
    return QualityCheckResult(
        source_key=source_key,
        category="row_count",
        rule="min_rows",
        column=None,
        status=_severity_status(violated, policy.min_rows_severity),
        actual=actual,
        threshold=policy.min_rows,
    )


def _range_result(
    table: pl.DataFrame, rule: RangeRule, *, source_key: str
) -> QualityCheckResult | None:
    if rule.column not in table.columns:
        return None
    non_null = table.filter(pl.col(rule.column).is_not_null())
    evaluated_rows = non_null.height
    if evaluated_rows == 0:
        return None
    conditions: list[pl.Expr] = []
    if rule.min is not None:
        conditions.append(pl.col(rule.column) >= rule.min)
    if rule.max is not None:
        conditions.append(pl.col(rule.column) <= rule.max)
    if not conditions:
        # min/max 둘 다 없음 — 검사할 경계가 없다. validate_spec이 이 구성을 이미
        # 거부하지만(empty_range_rule), evaluator는 그 호출에 의존하지 않는다.
        return None
    condition = conditions[0]
    for extra in conditions[1:]:
        condition = condition & extra
    try:
        passing = non_null.filter(condition).height
    except pl.exceptions.PolarsError:
        return QualityCheckResult(
            source_key=source_key,
            category="range",
            rule="range",
            column=rule.column,
            status=_severity_status(True, rule.severity),
            actual=None,
            threshold=_range_threshold(rule),
            affected_rows=None,
            evaluated_rows=None,
            detail=(
                f"column dtype {table.schema[rule.column]} cannot be compared with numeric range"
            ),
        )
    affected_rows = evaluated_rows - passing
    return QualityCheckResult(
        source_key=source_key,
        category="range",
        rule="range",
        column=rule.column,
        status=_severity_status(affected_rows > 0, rule.severity),
        actual=affected_rows / evaluated_rows,
        threshold=_range_threshold(rule),
        affected_rows=affected_rows,
        evaluated_rows=evaluated_rows,
    )


def _compare_columns_result(
    table: pl.DataFrame, rule: CompareColumnsRule, *, source_key: str
) -> QualityCheckResult | None:
    if rule.left not in table.columns or rule.right not in table.columns:
        return None
    both = table.filter(pl.col(rule.left).is_not_null() & pl.col(rule.right).is_not_null())
    evaluated_rows = both.height
    if evaluated_rows == 0:
        return None
    comparator = _COMPARE_OPERATORS[rule.operator]
    try:
        satisfied = both.filter(comparator(pl.col(rule.left), pl.col(rule.right))).height
    except pl.exceptions.PolarsError:
        return QualityCheckResult(
            source_key=source_key,
            category="compare_columns",
            rule="compare_columns",
            column=f"{rule.left},{rule.right}",
            status=_severity_status(True, rule.severity),
            actual=None,
            threshold=_compare_threshold(rule),
            affected_rows=None,
            evaluated_rows=None,
            detail=(
                f"column dtypes {table.schema[rule.left]} and {table.schema[rule.right]} "
                f"cannot be compared with operator {rule.operator}"
            ),
        )
    affected_rows = evaluated_rows - satisfied
    return QualityCheckResult(
        source_key=source_key,
        category="compare_columns",
        rule="compare_columns",
        column=f"{rule.left},{rule.right}",
        status=_severity_status(affected_rows > 0, rule.severity),
        actual=affected_rows / evaluated_rows,
        threshold=_compare_threshold(rule),
        affected_rows=affected_rows,
        evaluated_rows=evaluated_rows,
    )


def evaluate_quality(
    silver: SilverDataset,
    policy: QualityPolicy | None,
    *,
    source_key: str,
    required_columns: Sequence[str] = (),
    column_dtypes: Mapping[str, DtypeSpec] | None = None,
) -> tuple[QualityCheckResult, ...]:
    """SilverDataset에 schema 계약 + QualityPolicy를 평가한다 (#486).

    Preview/Build 공통 진입점 — 순수 함수(부작용 없음)이므로 동일 입력에는 항상
    동일 결과를 반환한다. 평가 불가능한 rule은 결과에서 제외된다(PASS로
    가장하지 않는다).

    매개변수:
        silver: 평가 대상 Silver 산출물.
        policy: 적용할 QualityPolicy. None이면 schema 계약만 평가한다(#446 하위 호환).
        source_key: 결과에 실릴 소스 식별자.
        required_columns: silver 생성 시 사용한 것과 동일한 필수 컬럼 목록.
        column_dtypes: silver 생성 시 사용한 것과 동일한 컬럼별 기대 dtype.

    반환값:
        QualityCheckResult 튜플. 실제로 평가된 check만 담는다(PASS 포함).
    """
    results: list[QualityCheckResult] = _schema_results(
        silver.table,
        source_key=source_key,
        required_columns=required_columns,
        column_dtypes=column_dtypes,
    )
    if policy is None:
        return tuple(results)

    dup = _duplicate_result(silver, policy, source_key=source_key)
    if dup is not None:
        results.append(dup)
    results.extend(_null_ratio_results(silver, policy, source_key=source_key))
    min_rows_result = _min_rows_result(silver, policy, source_key=source_key)
    if min_rows_result is not None:
        results.append(min_rows_result)
    for range_rule in policy.range:
        range_result = _range_result(silver.table, range_rule, source_key=source_key)
        if range_result is not None:
            results.append(range_result)
    for compare_rule in policy.compare_columns:
        compare_result = _compare_columns_result(silver.table, compare_rule, source_key=source_key)
        if compare_result is not None:
            results.append(compare_result)
    return tuple(results)


__all__ = ["evaluate_quality"]
