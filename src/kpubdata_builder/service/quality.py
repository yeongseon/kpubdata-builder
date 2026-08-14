"""Dataset Quality History 집계 로직 (#486).

``GET /datasets/{dataset_id}/quality/history``가 쓰는 순수 조회 로직을 담는다.
dataset→run 조회는 #488의 ``datasets`` 모듈 helper(``BuilderService._dataset_records_for``
등)를 그대로 재사용한다 — 새로운 dataset grouping/index를 만들지 않는다. 이
모듈은 각 run의 manifest에서 읽은 ``quality_results``를 pass/warn/fail 집계로
정리하는 것만 담당한다.
"""

from __future__ import annotations

from ..spec import JsonValue
from .datasets import RunRecord


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
        if isinstance(value, int):
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


__all__ = ["summarize_run_quality"]
