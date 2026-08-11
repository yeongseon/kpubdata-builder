"""PII 스캐너 단위 테스트 (#441, QG-1).

패턴(주민번호/휴대전화/이메일/사업자번호), 컬럼명 휴리스틱, 그리고 보안 원칙
(원본 값 미노출)을 검증한다.
"""

from __future__ import annotations

import polars as pl

from kpubdata_builder.stages.silver.pii import scan_pii


class TestScanPiiPatterns:
    """패턴 기반 PII 검출."""

    def test_detects_rrn(self) -> None:
        table = pl.DataFrame({"text": ["900101-1234567", "no pii here"]})
        findings = scan_pii(table)
        kinds = [f.kind for f in findings]
        assert "rrn" in kinds
        rrn = next(f for f in findings if f.kind == "rrn")
        assert rrn.count == 1
        assert rrn.column == "text"

    def test_detects_phone(self) -> None:
        table = pl.DataFrame({"contact": ["010-1234-5678", "x"]})
        findings = scan_pii(table)
        assert "phone" in [f.kind for f in findings]

    def test_detects_email(self) -> None:
        table = pl.DataFrame({"x": ["user@example.com", "y"]})
        findings = scan_pii(table)
        assert "email" in [f.kind for f in findings]

    def test_detects_business_no(self) -> None:
        table = pl.DataFrame({"biz": ["123-45-67890", "z"]})
        findings = scan_pii(table)
        assert "business_no" in [f.kind for f in findings]

    def test_no_pii_returns_empty(self) -> None:
        table = pl.DataFrame({"a": ["normal text", "more text"]})
        assert scan_pii(table) == []

    def test_non_string_columns_skip_pattern_scan(self) -> None:
        """비문자열 컬럼은 패턴 스캔을 건너뛴다 (컬럼명 휴리스틱만 적용)."""
        table = pl.DataFrame({"value": [1, 2, 3]})
        assert scan_pii(table) == []


class TestScanPiiColumnNameHeuristics:
    """컬럼명 휴리스틱 — 공공데이터 축약형(NM/TELNO/ADRES 등)."""

    def test_name_column_flagged(self) -> None:
        table = pl.DataFrame({"OPNR_NM": ["홍길동", "김철수"]})
        assert "name" in [f.kind for f in scan_pii(table)]

    def test_addr_column_flagged(self) -> None:
        table = pl.DataFrame({"ROAD_ADRES": ["서울", "부산"]})
        assert "addr" in [f.kind for f in scan_pii(table)]

    def test_phone_column_flagged(self) -> None:
        table = pl.DataFrame({"TELNO": ["02-123-4567", "x"]})
        assert "phone" in [f.kind for f in scan_pii(table)]


class TestScanPiiSecurityPrinciple:
    """검출 결과에 원본 값이 새어나가지 않아야 한다 (#441 보안 원칙)."""

    def test_no_original_values_in_findings(self) -> None:
        secret = "900101-1234567"
        table = pl.DataFrame({"x": [secret, "clean"]})
        findings = scan_pii(table)
        # findings의 모든 필드(column/kind/count) 직렬화에 원본 값이 없어야
        serialized = " ".join(f"{f.column}|{f.kind}|{f.count}" for f in findings)
        assert secret not in serialized
