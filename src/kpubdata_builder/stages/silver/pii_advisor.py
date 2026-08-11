"""PII 오탐 문맥 판정 보조 (AI-2, #447).

QG-1(scan_pii)이 검출한 PII 후보 컬럼에 대해 LLM이 오탐 여부 의견을 제시한다.
LLM은 게이트가 아니다 — 정규식이 차단 권한을 갖고, LLM은 "오탐으로 보인다"는
의견만 낸다. 최종 해제는 사람이 allow_columns 에 명시한다.

**절대 금지**: LLM이 "괜찮다"고 해서 통과시키는 것. 놓치면 개인정보가 공개된다.

원본 데이터 값은 프롬프트에 포함하지 않는다 — 컬럼명과 종류(kind)만.
데이터 샘플이 외부로 나가는 것이 아니므로 스크러빙이 불필요하다 (#447).
"""

from __future__ import annotations

from dataclasses import dataclass

from .pii import PiiFinding


@dataclass(frozen=True)
class PiiAdvisoryResult:
    """LLM 오탐 판정 결과.

    속성:
        likely_false_positives: 오탐으로 의심되는 컬럼명 목록 (LLM 의견).
        raw_response: LLM 원본 응답 (UI 힌트용).
    """

    likely_false_positives: tuple[str, ...]
    raw_response: str


def build_pii_advisory_prompt(findings: list[PiiFinding]) -> str:
    """PII 검출 후보를 LLM에 전달해 오탐 여부를 묻는 프롬프트를 구성한다 (#447).

    원본 값은 포함하지 않는다 — 컬럼명과 종류(kind)만. 정규식은 과탐지한다
    (담당부서명이 인명으로 잡히는 식) — LLM이 문맥으로 오탐을 줄여준다.
    """
    if not findings:
        return ""

    findings_text = "\n".join(
        f"  - 컬럼 {f.column!r}: 종류={f.kind}, 매칭 수={f.count}"
        for f in findings
        if f.column is not None
    )

    return f"""한국 공공데이터에서 정규식으로 검출된 PII 후보 컬럼 목록이다.
정규식은 과탐지할 수 있다 — 담당부서명이 인명으로 잡히거나, 코드값이
주민번호 패턴과 우연히 일치하는 식이다.

각 컬럼이 실제 PII인지 오탐인지 판정해라. 컬럼명의 의미(한국어 축약어 등)를
근거로 판단한다.

PII 후보:
{findings_text}

출력 형식: JSON 객체
{{
  "likely_false_positives": ["컬럼명1", "컬럼명2"]
}}

주의: 이 판정은 참고용이다. 최종 결정은 사용자가 한다."""


__all__ = ["PiiAdvisoryResult", "build_pii_advisory_prompt"]
