"""PII(개인정보) 스캐너 (#441, QG-1).

Silver 단계 정제 테이블에서 주민등록번호/휴대전화/이메일/사업자등록번호 패턴과
의심 컬럼명을 결정적으로 검색한다.

보안 원칙 (#441 인수 기준): 검출 내용(원본 값)은 결과에 담지 않고 컬럼명·종류·
건수만 반환한다. 로그/manifest 에 원본 값이 새어나가는 일이 없어야 한다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import polars as pl

# 한국 PII 정규식 — 과탐지 완화를 위해 자릿수/구분자 형태를 요구한다.
# 주민등록번호: YYMMDD-SXXXXXX (S는 1-4, 성별/국적)
_RRN = re.compile(r"\b\d{6}-[1-4]\d{6}\b")
# 휴대전화: 01X(-?)XXXX(-?)XXXX
_PHONE = re.compile(r"\b01[016789]-?\d{3,4}-?\d{4}\b")
# 이메일: 로컬@도메인.tld
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
# 사업자등록번호: 000-00-00000
_BRN = re.compile(r"\b\d{3}-\d{2}-\d{5}\b")

_PATTERNS: dict[str, re.Pattern[str]] = {
    "rrn": _RRN,
    "phone": _PHONE,
    "email": _EMAIL,
    "business_no": _BRN,
}

# 의심 컬럼명 휴리스틱 (대소문자 무시 부분 일치). 공공데이터 축약형 포함.
_SUSPECT_PARTS: dict[str, tuple[str, ...]] = {
    "name": ("NM", "NAME", "성명", "이름"),
    "phone": ("TEL", "TELNO", "PHONE", "HP", "MOBILE", "연락처"),
    "addr": ("ADDR", "ADRES", "주소"),
    "email": ("EMAIL", "이메일"),
    "rrn": ("RRN", "JUMIN", "주민", "SSN"),
}


@dataclass(frozen=True)
class PiiFinding:
    """단일 PII 검출 결과. 원본 값은 절대 담지 않는다 (#441).

    속성:
        column: 매칭된 컬럼명. None이면 테이블 전체 스캔 결과.
        kind: rrn/phone/email/business_no/name/addr 중 하나.
        count: 패턴 매칭 건수. 컬럼명 휴리스틱은 1.
    """

    column: str | None
    kind: str
    count: int


def scan_pii(table: pl.DataFrame) -> list[PiiFinding]:
    """정제 테이블을 PII 패턴 + 컬럼명 휴리스틱으로 스캔한다 (#441).

    원본 값을 결과에 담지 않는다 — 컬럼명·종류·건수만. 호출자는 findings 를
    manifest warnings 등에 기록할 때도 동일하게 원본 값을 적지 않아야 한다.
    """
    findings: list[PiiFinding] = []
    for column_name in table.columns:
        series = table.get_column(column_name)
        # 문자열 컬럼만 패턴 매칭. 비문자열은 컬럼명 휴리스틱만 적용.
        if series.dtype == pl.Utf8:
            non_null = series.drop_nulls()
            for kind, pattern in _PATTERNS.items():
                if non_null.len() == 0:
                    continue
                count = int(non_null.str.contains(pattern.pattern).sum())
                if count > 0:
                    findings.append(PiiFinding(column=column_name, kind=kind, count=count))
        # 컬럼명 휴리스틱 — 한 컬럼당 첫 매칭 종류 하나만.
        upper = column_name.upper()
        for kind, parts in _SUSPECT_PARTS.items():
            if any(part in upper for part in parts):
                findings.append(PiiFinding(column=column_name, kind=kind, count=1))
                break
    return findings


__all__ = ["PiiFinding", "scan_pii"]
