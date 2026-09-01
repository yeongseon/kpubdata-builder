"""Dataset Quality History 집계 로직 (#486).

``GET /datasets/{dataset_id}/quality/history``가 쓰는 순수 조회 로직을 담는다.
dataset→run 조회는 #488의 ``datasets`` 모듈 helper(``BuilderService._dataset_records_for``
등)를 그대로 재사용한다 — 새로운 dataset grouping/index를 만들지 않는다. 이
모듈은 각 run의 manifest에서 읽은 ``quality_results``를 pass/warn/fail 집계로
정리하는 것만 담당한다.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import Literal

from ..spec import JsonValue
from .datasets import RunRecord

Availability = Literal["available", "partial", "unavailable"]

# 최근 quality aggregate가 지원하는 유일한 window (monitoring #516과 같은 어휘).
QUALITY_SUMMARY_WINDOW_SECONDS = 24 * 3600


def _parse_iso_utc(value: str | None) -> datetime | None:
    """ISO 8601 문자열을 UTC ``datetime``으로 파싱한다. 실패/None이면 None.

    ``monitoring._parse_iso_utc``와 같은 엄격한 규칙이다 — naive local time 비교를
    피하려고 tz 정보가 없으면 UTC로 간주한다. monitoring이 quality를 import하는
    방향이므로 역참조를 만들지 않도록 여기에 동일 helper를 둔다.
    """
    if not value:
        return None
    try:
        text = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _validated_rows(manifest: dict[str, object]) -> int | None:
    """run의 validated_rows를 manifest.row_counts 합계로 계산한다 (#486).

    각 QualityCheckResult.evaluated_rows를 합치면 같은 Silver 행을 rule 수만큼
    중복 계산하게 되므로 쓰지 않는다. 대신 #488이 이미 정의한 row_counts
    semantics(소스별 Silver row_count의 합)를 그대로 재사용한다. multi-source
    dataset은 소스별 합계이며, 임의로 첫 source만 선택하지 않는다.
    """
    raw = manifest.get("row_counts")
    if not isinstance(raw, dict):
        return None
    total = 0
    found = False
    for value in raw.values():
        if isinstance(value, int) and not isinstance(value, bool):
            total += value
            found = True
    return total if found else None


def summarize_run_quality(record: RunRecord, manifest: dict[str, object]) -> dict[str, JsonValue]:
    """단일 run의 quality_results를 pass/warn/fail 집계로 요약한다 (#486).

    legacy run(manifest에 quality_results 필드 자체가 없음)은 evaluated_checks=0,
    rule_pass_rate=None으로 표현한다 — 미평가를 "전부 PASS"로 해석하지 않는다.
    partial/failed run도 structured quality_results가 있으면 그대로 집계에
    포함한다(정책적으로 history에서 제외하지 않는다).
    """
    raw_quality = manifest.get("quality_results")
    pass_count = warn_count = fail_count = 0
    if isinstance(raw_quality, dict):
        for source_results in raw_quality.values():
            if not isinstance(source_results, list):
                continue
            for entry in source_results:
                if not isinstance(entry, dict):
                    continue
                status = entry.get("status")
                if status == "pass":
                    pass_count += 1
                elif status == "warn":
                    warn_count += 1
                elif status == "fail":
                    fail_count += 1

    evaluated_checks = pass_count + warn_count + fail_count
    rule_pass_rate = (pass_count / evaluated_checks) if evaluated_checks > 0 else None

    return {
        "run_id": record.run_id,
        "timestamp": record.finished_at or record.started_at,
        "status": record.status,
        "pass_count": pass_count,
        "warn_count": warn_count,
        "fail_count": fail_count,
        "evaluated_checks": evaluated_checks,
        "rule_pass_rate": rule_pass_rate,
        "validated_rows": _validated_rows(manifest),
    }


def quality_availability(
    manifest: dict[str, object], known_sources: tuple[str, ...]
) -> tuple[Availability, int]:
    """``GET /builds/{run_id}/quality``의 availability/evaluated_checks를 판정한다 (#514).

    빈 ``{"quality_results": {}, "schema_drift": {}}``만으로는 "평가했지만 0건"과
    "애초에 계산된 적이 없음"을 구분할 수 없었다 — 이 함수가 그 구분을 담당한다.

    - ``unavailable``: 결과가 전혀 없다. quality_results 필드 자체가 없는 legacy
      run(#486 이전)뿐 아니라, 필드는 있지만(``quality_results: {}``) known
      source 중 어느 하나도 커버하지 못하는 새 run도 포함한다 — manifest writer는
      quality가 하나도 계산되지 않았어도 빈 ``{}``를 항상 기록하므로, 모든
      source가 quality 단계 진입 전에 실패한 run에서 실제로 발생한다.
    - ``partial``: quality_results가 있고, 이 run이 실제로 시도한 source
      (``stages.known_source_keys``) 중 일부만 커버한다 — 예: multi-source run에서
      한 source의 Silver만 실패해 quality 평가가 돌지 않은 경우.
    - ``available``: known source를 모두 커버한다(known_sources가 비어 있는
      legacy manifest도 포함). evaluated_checks==0일 수 있다 — 평가된 rule이
      하나도 없는 것과 결과 자체가 없는 것을 구분하기 위해 별도로 available로
      취급한다.
    """
    raw_quality = manifest.get("quality_results")
    if not isinstance(raw_quality, dict):
        return "unavailable", 0

    evaluated_checks = 0
    for source_results in raw_quality.values():
        if not isinstance(source_results, list):
            continue
        for entry in source_results:
            if isinstance(entry, dict) and entry.get("status") in ("pass", "warn", "fail"):
                evaluated_checks += 1

    known = set(known_sources)
    if known:
        covered = known & raw_quality.keys()
        if not covered:
            return "unavailable", evaluated_checks
        if covered != known:
            return "partial", evaluated_checks
    return "available", evaluated_checks


def aggregate_quality_window(
    entries: Iterable[tuple[RunRecord, dict[str, object] | None]],
    *,
    now: datetime,
    window_seconds: int = QUALITY_SUMMARY_WINDOW_SECONDS,
) -> dict[str, JsonValue]:
    """``GET /quality/summary``의 PASS/WARN/FAIL run 수 aggregate (#486 후속, additive).

    ``entries``는 이미 principal 접근 필터(#505 ownership)를 통과한 canonical run
    (``RunRecord``)과 그 manifest다. 여기서는 시간창 필터와 run 단위 집계만 한다.

    - 시간 기준: canonical run timestamp(``finished_at``, 없으면 ``started_at``)를
      UTC로 파싱해 ``(now - window, now]`` 안에 드는 run만 센다. 파싱 불가능한
      timestamp(legacy/malformed)는 창에 놓을 수 없으므로 제외한다 — naive local
      time으로 비교하지 않는다.
    - ``total_runs``: 창 안의 run 수(status 무관).
    - ``evaluated_runs``: 그 중 structured quality가 실제로 평가된
      (``evaluated_checks > 0``) run 수. unavailable/0-check run은 제외한다 —
      미평가를 PASS로 세지 않는다.
    - ``warn_runs``: evaluated run 중 WARN 결과가 하나 이상인 run 수. 같은 run에
      WARN check가 여러 개여도 run은 1로 센다. Studio Home의 "QUALITY WARN (24H)"
      KPI가 이 값이다.
    - ``fail_runs``: evaluated run 중 FAIL 결과가 하나 이상인 run 수. WARN 요건과
      독립이므로 ``warn_runs``와 겹칠 수 있다(WARN·FAIL이 함께 있는 run).
    - ``pass_runs``: evaluated run 중 WARN·FAIL이 하나도 없는 run 수.
      ``pass_runs + (WARN 또는 FAIL이 있는 run 수) == evaluated_runs``.
    """
    lower = now - timedelta(seconds=window_seconds)
    total = evaluated = pass_runs = warn_runs = fail_runs = 0
    for record, manifest in entries:
        timestamp = _parse_iso_utc(record.finished_at or record.started_at)
        if timestamp is None or not (lower < timestamp <= now):
            continue
        total += 1
        if manifest is None:
            continue
        summary = summarize_run_quality(record, manifest)
        checks = summary["evaluated_checks"]
        if not isinstance(checks, int) or checks <= 0:
            continue
        evaluated += 1
        warn = summary["warn_count"]
        fail = summary["fail_count"]
        has_warn = isinstance(warn, int) and warn > 0
        has_fail = isinstance(fail, int) and fail > 0
        if has_warn:
            warn_runs += 1
        if has_fail:
            fail_runs += 1
        if not has_warn and not has_fail:
            pass_runs += 1
    return {
        "total_runs": total,
        "evaluated_runs": evaluated,
        "pass_runs": pass_runs,
        "warn_runs": warn_runs,
        "fail_runs": fail_runs,
    }


__all__ = [
    "Availability",
    "QUALITY_SUMMARY_WINDOW_SECONDS",
    "aggregate_quality_window",
    "quality_availability",
    "summarize_run_quality",
]
