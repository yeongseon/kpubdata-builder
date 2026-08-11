"""드리프트 원인 해석 (AI-3, #448).

DRIFT-1(detect_drift)이 감지한 드리프트의 원인 가설을 LLM에 요청하는 프롬프트를
구성한다. 감지는 결정적으로 하고 해석만 LLM이 붙인다.

제안은 알림 메시지에 참고로만 표시된다 — 게이트 판정에 영향을 주지 않는다.
"""

from __future__ import annotations

from .drift import DriftFinding


def build_drift_advisory_prompt(findings: list[DriftFinding]) -> str:
    """드리프트 감지 결과를 LLM에 전달해 원인 가설을 요청하는 프롬프트 (#448).

    "컬럼명이 바뀐 상류 스키마 변경" vs "새 지역 코드가 추가된 정상 변화"를
    구분해주면 알림 피로가 줄어든다.
    """
    if not findings:
        return ""

    findings_text = "\n".join(
        f"  - 종류={f.kind}, 컬럼={f.column or '테이블 전체'}, 상세={f.detail}" for f in findings
    )

    return f"""한국 공공데이터 파이프라인에서 드리프트가 감지되었다.
각 드리프트의 원인을 분류하고 설명해라.

드리프트 목록:
{findings_text}

각 드리프트에 대해 원인을 다음 중 하나로 분류해라:
- upstream_schema_change: 상류 API가 스키마를 변경
- data_growth: 정상적인 데이터 증가/감소 (append-only 등)
- api_error: 일시적 API 오류 또는 부분 응답
- unknown: 원인 불명

출력 형식: JSON 배열
[
  {{"kind": "column_added", "column": "...", "cause": "upstream_schema_change", "explanation": "..."}}
]

주의: 이 해석은 참고용이다. 게이트 판정에 영향을 주지 않는다."""


__all__ = ["build_drift_advisory_prompt"]
