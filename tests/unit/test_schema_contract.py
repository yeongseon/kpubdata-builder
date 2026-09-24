"""소스 스키마 계약 검증 테스트 (#437, VAL-1).

BuildSpec 의 ``sources[].schema`` 선언이 (1) 로더에서 SchemaContract로 파싱되고,
(2) validator 가 unknown dtype/cast 를 거부하며, (3) Silver 검증 게이트가
required/dtype 위반을 잡아내는지 검증한다. 이전까지는 게이트가 존재했지만
통과 조건이 없었다 (orchestrator 가 인자를 안 넘겨 항상 ok).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import cast

import pytest

from kpubdata_builder.errors import ValidationError
from kpubdata_builder.spec import BuildSpec, parse_spec
from kpubdata_builder.spec.validator import validate_spec
from kpubdata_builder.stages.bronze.models import BronzeArtifact, utc_now
from kpubdata_builder.stages.silver.build import build_silver_dataset

_BASE_SOURCE = {"provider": "datago", "dataset": "air_quality"}
_BASE_EXPORTS = [{"kind": "jsonl", "output_path": "o.jsonl"}]


def _spec(sources: list[dict[str, object]]) -> object:
    return parse_spec(
        {
            "dataset_id": "ds",
            "title": "t",
            "description": "d",
            "sources": sources,
            "exports": _BASE_EXPORTS,
        }
    )


class TestSchemaContractParsing:
    """loader._parse_schema → SchemaContract (#437)."""

    def test_parse_required_dtypes_casts(self) -> None:
        spec = _spec(
            [
                {
                    **_BASE_SOURCE,
                    "schema": {
                        "required": ["base_date", "nx"],
                        "dtypes": {"nx": "int64", "base_date": "string"},
                        "casts": {"nx": "int64"},
                    },
                }
            ]
        )
        schema = spec.sources[0].schema
        assert schema is not None
        assert schema.required == ("base_date", "nx")
        assert schema.dtypes == {"nx": "int64", "base_date": "string"}
        assert schema.casts == {"nx": "int64"}

    def test_schema_none_when_not_declared(self) -> None:
        """schema 미선언 시 None — 하위 호환 (#437 인수 기준)."""
        spec = _spec([{**_BASE_SOURCE}])
        assert spec.sources[0].schema is None

    def test_schema_partial_fields(self) -> None:
        """일부 필드만 선언해도 파싱된다 (기본값 빈 컬렉션)."""
        spec = _spec([{**_BASE_SOURCE, "schema": {"required": ["x"]}}])
        schema = spec.sources[0].schema
        assert schema is not None
        assert schema.required == ("x",)
        assert schema.dtypes == {}
        assert schema.casts == {}


class TestSchemaContractValidation:
    """validator._schema_problems — unknown dtype/cast 거부 (#437)."""

    def test_rejects_unknown_dtype(self) -> None:
        spec = _spec([{**_BASE_SOURCE, "schema": {"dtypes": {"nx": "NotARealDtype"}}}])
        with pytest.raises(ValidationError) as exc:
            validate_spec(spec)
        codes = [p.code for p in (exc.value.structured_problems or [])]
        assert "unknown_dtype" in codes

    def test_rejects_unknown_cast(self) -> None:
        spec = _spec([{**_BASE_SOURCE, "schema": {"casts": {"nx": "BogusType"}}}])
        with pytest.raises(ValidationError) as exc:
            validate_spec(spec)
        codes = [p.code for p in (exc.value.structured_problems or [])]
        assert "unknown_cast_dtype" in codes

    def test_accepts_known_dtypes(self) -> None:
        """_NAMED_DTYPES 키(int64/string/float64 등)는 통과."""
        spec = _spec(
            [
                {
                    **_BASE_SOURCE,
                    "schema": {
                        "required": ["x"],
                        "dtypes": {"x": "string"},
                        "casts": {"x": "int64"},
                    },
                }
            ]
        )
        validate_spec(spec)  # 예외 없음


class TestSchemaContractEnforcement:
    """build_silver_dataset 인자 전달 → Silver 검증 게이트 활성화 (#437).

    orchestrator/preview 가 source.schema 를 required_columns/casts/column_dtypes
    로 넘기므로, 이제 게이트가 실제로 동작한다.
    """

    @staticmethod
    def _bronze(records: list[dict[str, object]]) -> BronzeArtifact:
        return BronzeArtifact(
            source_key="test",
            raw_records=records,
            fetch_params={},
            fetched_at=utc_now(),
            provenance=None,
        )

    def test_required_missing_fails_validation(self) -> None:
        """required 컬럼이 실제 테이블에 없으면 검증 실패 (#437)."""
        bronze = self._bronze([{"a": 1}, {"a": 2}])
        silver = build_silver_dataset(bronze, required_columns=("missing_col",))
        assert not silver.validation.ok
        codes = [p.code for p in silver.validation.problems]
        assert "missing_column" in codes

    def test_dtype_mismatch_fails_validation(self) -> None:
        """선언 dtype과 실제가 다르면 검증 실패 (#437)."""
        bronze = self._bronze([{"a": 1}])
        silver = build_silver_dataset(bronze, column_dtypes={"a": "string"})
        assert not silver.validation.ok
        codes = [p.code for p in silver.validation.problems]
        assert "dtype_mismatch" in codes

    def test_matching_contract_passes(self) -> None:
        """계약이 실제와 일치하면 ok=True (양성)."""
        bronze = self._bronze([{"a": 1}, {"a": 2}])
        silver = build_silver_dataset(bronze, required_columns=("a",), column_dtypes={"a": "int64"})
        assert silver.validation.ok

    def test_no_contract_backward_compat(self) -> None:
        """인자 미전달(계약 None) 시 기존 동작 — 항상 ok (하위 호환)."""
        bronze = self._bronze([{"a": 1}])
        silver = build_silver_dataset(bronze)
        assert silver.validation.ok


class _FakeResult:
    def __init__(self, items: list[dict[str, object]]) -> None:
        self._items = items

    @property
    def items(self) -> list[dict[str, object]]:
        return self._items


class _FakeDataset:
    def __init__(self, items: list[dict[str, object]]) -> None:
        self._items = items

    def list(self, **_params: object) -> _FakeResult:
        return _FakeResult(self._items)


class _FakeClient:
    def __init__(self, data: dict[str, list[dict[str, object]]]) -> None:
        self._data = data

    def dataset(self, source_key: str) -> _FakeDataset:
        return _FakeDataset(self._data[source_key])


class TestTransformRulesReachTheBuild:
    """schema.rename/derived 선언이 orchestrator를 거쳐 Silver 산출물에 반영된다 (#611).

    build_silver_dataset이 인자를 받아도 orchestrator가 넘기지 않으면 선언은
    아무 효과가 없다 — #437이 "게이트는 있는데 통과 조건이 없던" 상태와 같은
    실패 양상이다.
    """

    def test_rename_and_derived_appear_in_the_silver_table(self, tmp_path: Path) -> None:
        import polars as pl

        from kpubdata_builder.pipeline import run_build

        spec = cast(
            BuildSpec,
            _spec(
                [
                    {
                        **_BASE_SOURCE,
                        "dataset": "apt_trade",
                        "schema": {
                            "rename": {"sggCd": "district_code", "dealAmount": "deal_amount"},
                            "casts": {"deal_amount": "int_comma"},
                            "derived": [
                                {
                                    "name": "deal_date",
                                    "kind": "date_parts",
                                    "columns": ["dealYear", "dealMonth", "dealDay"],
                                }
                            ],
                        },
                    }
                ]
            ),
        )
        client = _FakeClient(
            {
                "datago.apt_trade": [
                    {
                        "sggCd": "11110",
                        "dealAmount": "120,000",
                        "dealYear": "2026",
                        "dealMonth": "9",
                        "dealDay": "8",
                    }
                ]
            }
        )

        run_build(spec, client=client, output_root=tmp_path, run_id="run1")

        table = pl.read_parquet(tmp_path / "run1" / "silver" / "datago.apt_trade" / "table.parquet")
        assert "district_code" in table.columns
        assert table["deal_amount"].to_list() == [120000]
        assert table["deal_date"].to_list() == [date(2026, 9, 8)]
