"""Silver 단계(#46): tabularize → validate → summarize → preview → persist 검증."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import polars as pl
import pytest

from kpubdata_builder.errors import TabularError
from kpubdata_builder.spec import ColumnNullTokens as CNT
from kpubdata_builder.spec import JsonValue
from kpubdata_builder.stages.bronze.models import BronzeArtifact, utc_now
from kpubdata_builder.stages.silver import (
    SilverDataset,
    ValidationResult,
    build_silver_dataset,
    normalize_table,
    persist_silver_dataset,
)
from kpubdata_builder.tabular import (
    PreviewSlice,
    SchemaInfo,
    TableStatistics,
    compute_statistics,
    generate_preview,
    infer_schema,
)


def _bronze(
    records: tuple[Mapping[str, JsonValue], ...], *, source_key: str = "datago.apt_trade"
) -> BronzeArtifact:
    normalized_records = tuple(dict(record) for record in records)
    return BronzeArtifact(
        source_key=source_key,
        raw_records=normalized_records,
        fetched_at=utc_now(),
    )


class TestBuildSilverDataset:
    def test_produces_dataset_with_table_schema_stats_preview(self) -> None:
        bronze = _bronze(
            (
                {"id": "1", "amount": 1000, "district": "강남구"},
                {"id": "2", "amount": 2500, "district": "서초구"},
            )
        )

        dataset = build_silver_dataset(bronze)

        assert isinstance(dataset, SilverDataset)
        assert isinstance(dataset.table, pl.DataFrame)
        assert dataset.table.shape == (2, 3)
        assert isinstance(dataset.schema, SchemaInfo)
        assert [c.name for c in dataset.schema.columns] == ["id", "amount", "district"]
        assert isinstance(dataset.statistics, TableStatistics)
        assert dataset.statistics.row_count == 2
        assert isinstance(dataset.preview, PreviewSlice)
        assert dataset.source_bronze == "datago.apt_trade"

    def test_validation_passes_when_required_columns_present(self) -> None:
        bronze = _bronze(({"id": "1", "amount": 1000},))

        dataset = build_silver_dataset(bronze, required_columns=("id", "amount"))

        assert isinstance(dataset.validation, ValidationResult)
        assert dataset.validation.ok is True
        assert dataset.validation.problems == ()

    def test_validation_fails_when_required_column_missing(self) -> None:
        bronze = _bronze(({"id": "1"},))

        dataset = build_silver_dataset(bronze, required_columns=("id", "amount"))

        assert dataset.validation.ok is False
        # ValidationProblem 객체로 바뀔었으므로 message 필드를 확인 (#261)
        assert any("amount" in problem.message for problem in dataset.validation.problems)

    def test_validation_passes_when_dtype_matches(self) -> None:
        bronze = _bronze(({"id": "1", "amount": 1000},))

        dataset = build_silver_dataset(
            bronze, casts={"amount": "int"}, column_dtypes={"amount": "int"}
        )

        assert dataset.validation.ok is True
        assert dataset.validation.problems == ()

    def test_validation_fails_when_dtype_mismatches(self) -> None:
        bronze = _bronze(({"id": "1", "amount": 1000},))

        # amount는 바로 읽으면 Int64; Float64를 요구하면 실패해야 한다
        dataset = build_silver_dataset(bronze, column_dtypes={"amount": "float"})

        assert dataset.validation.ok is False
        assert any("amount" in p.message for p in dataset.validation.problems)

    def test_validation_reports_missing_column_for_dtype_spec(self) -> None:
        bronze = _bronze(({"id": "1"},))

        # 'amount' 코럼이 없으면 dtype 검증 실패 메시지를 포함해야 한다
        dataset = build_silver_dataset(bronze, column_dtypes={"amount": "int"})

        assert dataset.validation.ok is False
        assert any("amount" in p.message for p in dataset.validation.problems)

    def test_preview_respects_limit(self) -> None:
        records = tuple({"n": i} for i in range(10))
        dataset = build_silver_dataset(_bronze(records), preview_limit=3)

        assert dataset.preview.total_rows == 10
        assert len(dataset.preview.rows) == 3

    def test_optional_casts_apply_declared_dtypes(self) -> None:
        bronze = _bronze(({"id": "1", "amount": "1000"}, {"id": "2", "amount": "2500"}))

        dataset = build_silver_dataset(bronze, casts={"amount": "int"})

        assert dataset.table.schema["amount"] == pl.Int64

    def test_cast_data_loss_raises_instead_of_silently_nulling(self) -> None:
        # 선언된 캐스팅이 값을 null로 떨어뜨리면 조용히 묻지 않고 TabularError로 실패 (#188).
        bronze = _bronze(({"id": "1", "amount": "1000"}, {"id": "2", "amount": "oops"}))

        with pytest.raises(TabularError, match="data loss"):
            _ = build_silver_dataset(bronze, casts={"amount": "int"})

    def test_rejects_negative_preview_limit(self) -> None:
        # 음수 preview_limit은 df.head(-1)로 새지 않도록 일찍 거부한다 (#190).
        bronze = _bronze(({"id": "1"},))

        with pytest.raises(ValueError, match="preview_limit"):
            _ = build_silver_dataset(bronze, preview_limit=-1)


class TestRowPreservingInvariant:
    """Bronze→Silver가 행을 filter/dedup/reorder하지 않는다는 불변조건을 고정한다 (#497).

    pipeline.preview의 Source↔Silver diff는 ``bronze.raw_records[i]``와
    ``silver.table``의 i번째 행이 항상 같은 논리적 행이라는 이 불변조건에
    의존한다(diff_available 판정의 실제 근거). normalize_table()이
    records_to_dataframe() 다음 컬럼 단위 연산만 호출하고 validate_table()은
    테이블을 아예 건드리지 않으므로 오늘은 이 불변조건이 성립하지만, 향후 누군가
    Silver에 dedup/filter/reorder를 추가하면 이 테스트가 깨져서 pipeline/preview.py
    의 alignment 가정을 재검토하라는 신호를 준다.
    """

    def test_coalesce_drops_columns_not_rows(self) -> None:
        # #620 — coalesce는 후보 *컬럼*을 소비한다. 행을 건드리기 시작하면 preview의
        # Source↔Silver alignment가 조용히 어긋난다.
        records = tuple(
            {"a": str(i) if i % 2 == 0 else None, "b": None if i % 2 == 0 else str(i)}
            for i in range(20)
        )
        bronze = _bronze(records)

        dataset = build_silver_dataset(bronze, coalesce={"merged": ("a", "b")})

        assert dataset.table.height == len(records)
        assert dataset.table["merged"].to_list() == [str(i) for i in range(20)]

    def test_row_count_is_preserved(self) -> None:
        records = tuple({"id": str(i), "amount": i * 100} for i in range(50))
        bronze = _bronze(records)

        dataset = build_silver_dataset(bronze, casts={"amount": "float"})

        assert dataset.table.height == len(records)
        assert dataset.statistics.row_count == len(records)

    def test_row_order_is_preserved_across_normalize_and_validate(self) -> None:
        # id를 원본 순서의 지문으로 써서, 캐스팅/검증을 거쳐도 순서가 바뀌지
        # 않는지 확인한다 — 값이 바뀌어도(#497 diff의 목적) 행의 *위치*는 원본과
        # 1:1로 대응해야 한다.
        records = tuple({"id": str(i), "amount": str(i * 100)} for i in range(20))
        bronze = _bronze(records)

        dataset = build_silver_dataset(
            bronze,
            casts={"amount": "int"},
            required_columns=("id", "amount"),
            column_dtypes={"amount": "int"},
        )

        assert dataset.table["id"].to_list() == [r["id"] for r in records]
        assert dataset.validation.ok is True

    def test_row_order_is_preserved_without_declared_casts(self) -> None:
        # casts가 전혀 없어도(가장 흔한 preview 경로) 순서 보존은 동일하게 성립한다.
        records = tuple({"id": str(i), "label": chr(ord("a") + i)} for i in range(10))
        bronze = _bronze(records)

        dataset = build_silver_dataset(bronze)

        assert dataset.table["id"].to_list() == [r["id"] for r in records]


class TestPersistSilverDataset:
    def test_writes_parquet_and_json_sidecars(self, tmp_path: Path) -> None:
        bronze = _bronze(
            (
                {"id": "1", "amount": 1000},
                {"id": "2", "amount": 2500},
            )
        )
        dataset = build_silver_dataset(bronze, required_columns=("id",))

        result = persist_silver_dataset(dataset, output_root=tmp_path, run_id="run1")

        assert result.table_path.exists()
        assert result.schema_path.exists()
        assert result.stats_path.exists()
        assert result.preview_path.exists()
        assert result.validation_path.exists()

        # parquet round-trip
        assert pl.read_parquet(result.table_path).to_dicts() == dataset.table.to_dicts()

        # json sidecars are well-formed and reflect the dataset
        stats = cast(
            dict[str, JsonValue], json.loads(result.stats_path.read_text(encoding="utf-8"))
        )
        assert stats["row_count"] == 2
        validation = cast(
            dict[str, JsonValue], json.loads(result.validation_path.read_text(encoding="utf-8"))
        )
        assert validation["ok"] is True

    def test_rejects_unsafe_run_id(self, tmp_path: Path) -> None:
        dataset = build_silver_dataset(_bronze(({"id": "1"},)))

        with pytest.raises(ValueError, match="run_id"):
            _ = persist_silver_dataset(dataset, output_root=tmp_path, run_id="../escape")

    def test_serializes_date_values_as_iso_strings(self, tmp_path: Path) -> None:
        # Date/Datetime으로 캐스팅된 컬럼이 preview에 들어가도 persist가 깨지지 않고
        # ISO 문자열로 직렬화되는지 검증한다 (#93 review).
        bronze = _bronze(({"d": "2025-01-01"}, {"d": "2025-01-02"}))
        dataset = build_silver_dataset(bronze, casts={"d": "date"})

        result = persist_silver_dataset(dataset, output_root=tmp_path, run_id="run1")

        preview = cast(
            dict[str, JsonValue], json.loads(result.preview_path.read_text(encoding="utf-8"))
        )
        rows = cast(list[dict[str, JsonValue]], preview["rows"])
        assert rows[0]["d"] == "2025-01-01"

    def test_serializes_naive_datetime_values_as_iso_strings(self, tmp_path: Path) -> None:
        # naive datetime으로 캐스팅된 컬럼이 preview에서 offset 없는 ISO 문자열로
        # 직렬화되는지 검증한다 (#97 datetime regression).
        bronze = _bronze(({"ts": "2025-01-01T12:30:00"}, {"ts": "2025-01-02T08:00:00"}))
        dataset = build_silver_dataset(bronze, casts={"ts": "datetime"})

        result = persist_silver_dataset(dataset, output_root=tmp_path, run_id="run1")

        preview = cast(
            dict[str, JsonValue], json.loads(result.preview_path.read_text(encoding="utf-8"))
        )
        rows = cast(list[dict[str, JsonValue]], preview["rows"])
        assert rows[0]["ts"] == "2025-01-01T12:30:00"
        assert rows[1]["ts"] == "2025-01-02T08:00:00"

    def test_serializes_timezone_aware_datetime_values_as_iso_strings(self, tmp_path: Path) -> None:
        # timezone-aware datetime 컬럼은 UTC로 정규화된 뒤 +00:00 offset을 포함한 ISO
        # 문자열로 직렬화된다(서로 다른 입력 tz가 동일 UTC 시각으로 수렴). cast map은
        # naive Datetime만 만들므로 aware 테이블을 직접 구성한다 (#97 datetime regression).
        kst = timezone(timedelta(hours=9))
        table = pl.DataFrame(
            {
                "ts": [
                    datetime(2025, 1, 1, 12, 30, tzinfo=timezone.utc),
                    datetime(2025, 1, 2, 17, 0, tzinfo=kst),
                ]
            }
        )
        assert table.schema["ts"].time_zone is not None
        dataset = SilverDataset(
            table=table,
            schema=infer_schema(table),
            statistics=compute_statistics(table),
            preview=generate_preview(table),
            validation=ValidationResult(ok=True),
            source_bronze="datago.apt_trade",
        )

        result = persist_silver_dataset(dataset, output_root=tmp_path, run_id="run1")

        preview = cast(
            dict[str, JsonValue], json.loads(result.preview_path.read_text(encoding="utf-8"))
        )
        rows = cast(list[dict[str, JsonValue]], preview["rows"])
        # 두 입력이 UTC로 정규화되어 +00:00 offset을 포함한다(KST 17:00 == UTC 08:00).
        assert rows[0]["ts"] == "2025-01-01T12:30:00+00:00"
        assert rows[1]["ts"] == "2025-01-02T08:00:00+00:00"

    def test_serializes_datetime_with_microseconds(self, tmp_path: Path) -> None:
        # microseconds를 가진 datetime이 잘리지 않고 ISO 소수 초까지 직렬화되는지 검증한다.
        bronze = _bronze(({"ts": "2025-01-01T12:30:00.123456"},))
        dataset = build_silver_dataset(bronze, casts={"ts": "datetime"})

        result = persist_silver_dataset(dataset, output_root=tmp_path, run_id="run1")

        preview = cast(
            dict[str, JsonValue], json.loads(result.preview_path.read_text(encoding="utf-8"))
        )
        rows = cast(list[dict[str, JsonValue]], preview["rows"])
        assert rows[0]["ts"] == "2025-01-01T12:30:00.123456"


class TestColumnRename:
    """원 API 필드명을 canonical 컬럼명으로 바꾼다 (#611).

    논문 실험의 Silver 계층은 ``sggCd`` 같은 원 필드명을 ``district_code`` 로
    바꾼 canonical dataset이어야 한다. 지금까지 BuildSpec 경로에는 rename 수단이
    없어 배포용 스크립트(scripts/pipeline/transform.py)에만 존재했다.
    """

    def test_renames_declared_columns(self) -> None:
        bronze = _bronze(({"sggCd": "11110", "aptNm": "은마"},))

        table = normalize_table(bronze, rename={"sggCd": "district_code", "aptNm": "apt_name"})

        assert table.columns == ["district_code", "apt_name"]

    def test_missing_source_column_surfaces_as_tabular_error(self) -> None:
        # R2(source evolution)는 상류 필드가 사라진 상황을 schema breakage로 세야
        # 한다. polars의 ColumnNotFoundError가 그대로 새어 나가면 어떤 컬럼이
        # 사라졌는지 호출자가 읽을 수 없다.

        bronze = _bronze(({"sggCd": "11110"},))

        with pytest.raises(TabularError) as exc:
            normalize_table(bronze, rename={"aptNm": "apt_name"})

        assert "aptNm" in str(exc.value)


class TestFormattedNumericCast:
    """천단위 구분자가 섞인 금액 문자열을 숫자로 캐스팅한다 (#611).

    ``dealAmount: int`` 를 선언하면 ``"120,000"`` 이 전부 null이 되어 #188의
    data-loss 가드가 빌드를 실패시킨다. 원천 공공데이터가 금액을 이 형식으로
    주므로, 선언으로 표현할 수단이 없으면 Silver 빌드 자체가 성립하지 않는다.
    """

    def test_comma_separated_decimal_casts_to_float(self) -> None:
        # 면적·금액 컬럼은 1,000 이상일 때만 구분자가 붙는 경우가 있다. 같은 컬럼
        # 안에서 표기가 갈리므로, 구분자를 처리하지 않으면 큰 값만 결측이 되어
        # 평균이 아래로 왜곡된다.
        bronze = _bronze(({"area": "84.5"}, {"area": "2,436.26"}))

        table = normalize_table(bronze, casts={"area": "float_comma"})

        assert table["area"].to_list() == [84.5, 2436.26]

    def test_comma_separated_amount_casts_to_integer(self) -> None:
        bronze = _bronze(({"dealAmount": "120,000"}, {"dealAmount": "82,500"}))

        table = normalize_table(bronze, casts={"dealAmount": "int_comma"})

        assert table["dealAmount"].to_list() == [120000, 82500]


class TestDerivedColumns:
    """기존 컬럼에서 새 컬럼을 만든다 (#611).

    원천 데이터는 거래일을 연/월/일 세 컬럼으로 분리해 주고, 데이터셋 간 조인에
    쓸 키도 제공하지 않는다. 둘 다 선언으로 표현할 수 없으면 Silver가 canonical
    dataset이 되지 못한다.
    """

    def test_date_parts_compose_a_date_column(self) -> None:
        from kpubdata_builder.spec import DerivedColumn

        bronze = _bronze(({"dealYear": "2026", "dealMonth": "9", "dealDay": "8"},))

        table = normalize_table(
            bronze,
            derived=(
                DerivedColumn(
                    name="deal_date",
                    kind="date_parts",
                    columns=("dealYear", "dealMonth", "dealDay"),
                ),
            ),
        )

        assert table["deal_date"].to_list() == [date(2026, 9, 8)]

    @pytest.mark.parametrize(
        ("month", "day"),
        [("13", "1"), ("2", "30"), ("0", "5")],
    )
    def test_date_parts_that_form_no_date_fail_instead_of_turning_null(
        self, month: str, day: str
    ) -> None:
        # 세 조각이 모두 있는데 날짜가 되지 않으면 값이 사라진 것이다. cast가 같은
        # 손실을 내면 #188이 빌드를 세우는데, 파생 규칙에서만 조용히 null이 되면
        # 월/일이 뒤바뀐 원천이 required 날짜의 절반을 잃고도 통과한다.
        from kpubdata_builder.spec import DerivedColumn

        bronze = _bronze(({"dealYear": "2026", "dealMonth": month, "dealDay": day},))

        with pytest.raises(TabularError, match="data loss"):
            normalize_table(
                bronze,
                derived=(
                    DerivedColumn(
                        name="deal_date",
                        kind="date_parts",
                        columns=("dealYear", "dealMonth", "dealDay"),
                    ),
                ),
            )

    def test_date_parts_with_a_missing_part_stay_null_without_failing(self) -> None:
        # 조각이 이미 없던 행은 규칙이 잃은 것이 아니다 — cast audit과 같은 기준
        # (null 증가만 손실로 센다).
        from kpubdata_builder.spec import DerivedColumn

        bronze = _bronze(
            (
                {"dealYear": "2026", "dealMonth": "9", "dealDay": "8"},
                {"dealYear": "2026", "dealMonth": None, "dealDay": "8"},
            )
        )

        table = normalize_table(
            bronze,
            derived=(
                DerivedColumn(
                    name="deal_date",
                    kind="date_parts",
                    columns=("dealYear", "dealMonth", "dealDay"),
                ),
            ),
        )

        assert table["deal_date"].to_list() == [date(2026, 9, 8), None]

    def test_date_parts_output_named_after_an_input_is_rejected(self) -> None:
        # name이 입력 컬럼 중 하나이면 with_columns가 그 원천 컬럼을 소리 없이
        # 덮어쓴다. 게다가 덮어쓴 뒤에는 잘못된 날짜인 행에서 조각 자체가 null로
        # 보여, 손실 감사가 잡으려던 것을 놓친다. 선언 오류로 거부한다.
        from kpubdata_builder.spec import DerivedColumn

        bronze = _bronze(({"dealYear": "2026", "dealMonth": "30", "dealDay": "2"},))

        with pytest.raises(TabularError, match="overwrite"):
            normalize_table(
                bronze,
                derived=(
                    DerivedColumn(
                        name="dealMonth",
                        kind="date_parts",
                        columns=("dealYear", "dealMonth", "dealDay"),
                    ),
                ),
            )

    def test_derived_name_must_not_overwrite_an_unrelated_column(self) -> None:
        from kpubdata_builder.spec import DerivedColumn

        bronze = _bronze(({"a": "x", "b": "y", "k": "ORIGINAL"},))

        with pytest.raises(TabularError, match="overwrite"):
            normalize_table(
                bronze, derived=(DerivedColumn(name="k", kind="join_key", columns=("a", "b")),)
            )

    def test_rename_target_must_not_collide_with_an_untouched_column(self) -> None:
        # Polars DuplicateError가 아니라 spec 용어의 TabularError로 실패한다.
        bronze = _bronze(({"sggCd": "11110", "district_code": "already"},))

        with pytest.raises(TabularError, match="collide"):
            normalize_table(bronze, rename={"sggCd": "district_code"})

    def test_rename_swap_is_allowed(self) -> None:
        # 서로 바꾸는 rename은 충돌이 아니다 — 두 원 컬럼 모두 rename 대상이다.
        bronze = _bronze(({"a": 1, "b": 2},))

        table = normalize_table(bronze, rename={"a": "b", "b": "a"})

        assert table.columns == ["b", "a"]

    def test_join_key_concatenates_columns_into_one(self) -> None:
        # T3(매매×전월세)는 4개 키로 조인해야 하는데 composition의 equi-join은
        # 단일 컬럼만 받는다. Silver에서 복합키를 만들어 두면 compose.py를
        # 건드리지 않고 같은 조인을 표현할 수 있다.
        from kpubdata_builder.spec import DerivedColumn

        bronze = _bronze(({"district_code": "11110", "year_month": "202609"},))

        table = normalize_table(
            bronze,
            derived=(
                DerivedColumn(
                    name="join_key",
                    kind="join_key",
                    columns=("district_code", "year_month"),
                ),
            ),
        )

        assert table["join_key"].to_list() == ["11110|202609"]

    def test_join_key_separator_in_value_does_not_collide(self) -> None:
        # 구분자를 그냥 이어 붙이면 ("a|b", "c")와 ("a", "b|c")가 같은 키가 되어
        # 무관한 행이 조인된다. 구성 요소를 이스케이프해 인코딩을 단사로 유지한다.
        from kpubdata_builder.spec import DerivedColumn

        bronze = _bronze(
            (
                {"left": "a|b", "right": "c"},
                {"left": "a", "right": "b|c"},
            )
        )

        table = normalize_table(
            bronze,
            derived=(DerivedColumn(name="join_key", kind="join_key", columns=("left", "right")),),
        )

        keys = table["join_key"].to_list()
        assert keys[0] != keys[1]
        assert keys == ["a\\|b|c", "a|b\\|c"]

    def test_join_key_escape_character_in_value_does_not_collide(self) -> None:
        # 이스케이프 문자 자체도 값에 나타날 수 있다. 두 배로 늘리지 않으면
        # ("a\\", "b")와 ("a", "\\b")가 다시 같은 키로 뭉친다.
        from kpubdata_builder.spec import DerivedColumn

        bronze = _bronze(
            (
                {"left": "a\\", "right": "b"},
                {"left": "a", "right": "\\b"},
            )
        )

        table = normalize_table(
            bronze,
            derived=(DerivedColumn(name="join_key", kind="join_key", columns=("left", "right")),),
        )

        keys = table["join_key"].to_list()
        assert keys[0] != keys[1]


class TestSchemaContractReachesNormalization:
    """BuildSpec의 rename/derived 선언이 실제 Silver 테이블에 도달한다 (#611).

    normalize_table이 기능을 갖고 있어도 build_silver_dataset이 넘겨주지 않으면
    선언은 아무 효과가 없다. 그 경계를 고정한다.
    """

    def test_build_silver_dataset_applies_rename_and_derived(self) -> None:
        from kpubdata_builder.spec import DerivedColumn

        bronze = _bronze(
            ({"sggCd": "11110", "dealYear": "2026", "dealMonth": "9", "dealDay": "8"},)
        )

        dataset = build_silver_dataset(
            bronze,
            rename={"sggCd": "district_code"},
            derived=(
                DerivedColumn(
                    name="deal_date",
                    kind="date_parts",
                    columns=("dealYear", "dealMonth", "dealDay"),
                ),
            ),
        )

        assert "district_code" in dataset.table.columns
        assert dataset.table["deal_date"].to_list() == [date(2026, 9, 8)]


class TestSourceTypeDeclaration:
    """원천 컬럼을 어떤 타입으로 읽을지 선언한다 (#611 후속).

    국토부 실거래가는 같은 컬럼을 레코드마다 다른 타입으로 준다 — ``jibun``은
    대부분 문자열이지만 일부 레코드에서 정수다. records_to_dataframe()은 조용한
    강제변환을 거부하고 TabularError를 던지므로(#187), 선언 없이는 Silver 빌드가
    성립하지 않는다. 거부를 없애는 것이 아니라, 선언된 컬럼만 허용한다.
    """

    def test_mixed_type_column_without_declaration_still_fails(self) -> None:
        from kpubdata_builder.errors import TabularError

        bronze = _bronze(({"jibun": "702"}, {"jibun": 69}))

        with pytest.raises(TabularError, match="heterogeneous"):
            normalize_table(bronze)

    def test_declared_column_is_read_as_text(self) -> None:
        bronze = _bronze(({"jibun": "702"}, {"jibun": 69}))

        table = normalize_table(bronze, read_as={"jibun": "str"})

        assert table["jibun"].to_list() == ["702", "69"]

    def test_declaration_preserves_nulls(self) -> None:
        # aptDong은 65%가 null이다. 선언이 null을 "None" 문자열로 만들면
        # 결측률 측정이 통째로 망가진다.
        bronze = _bronze(({"aptDong": "105"}, {"aptDong": 205}, {"aptDong": None}))

        table = normalize_table(bronze, read_as={"aptDong": "str"})

        assert table["aptDong"].to_list() == ["105", "205", None]


class TestNullTokenNormalization:
    """결측을 나타내는 원천 표기를 null로 바꾼다 (#611 후속).

    결측을 빈 문자열과 None 두 가지로 표기하는 소스가 있다. 빈 문자열을 그대로
    두고 숫자 캐스팅을 선언하면 #188의 data-loss 가드가 빌드를 실패시킨다 — 값이
    null로 떨어졌기 때문인데, 그 값은 애초에 데이터가 아니라 "없음"의 표기였다.

    결측을 지우는 것이 아니라 *하나의 표기로 모으는* 것이다. 몇 개가 결측인지는
    그대로 남아 품질 지표가 센다.
    """

    def test_empty_string_blocks_a_numeric_cast_without_declaration(self) -> None:
        from kpubdata_builder.errors import TabularError

        bronze = _bronze(({"area": "84.5"}, {"area": ""}))

        with pytest.raises(TabularError, match="data loss"):
            normalize_table(bronze, casts={"area": "float"})

    def test_declared_null_token_becomes_null_before_casting(self) -> None:
        bronze = _bronze(({"area": "84.5"}, {"area": ""}))

        table = normalize_table(bronze, casts={"area": "float"}, null_tokens=("",))

        assert table["area"].to_list() == [84.5, None]

    def test_null_tokens_do_not_touch_undeclared_values(self) -> None:
        # "-"를 결측으로 선언하지 않았다면 그대로 둔다. 무엇을 결측으로 볼지는
        # 데이터셋마다 다르고, builder가 임의로 정하면 측정 대상이 오염된다.
        bronze = _bronze(({"grade": "-"}, {"grade": "A"}))

        table = normalize_table(bronze, null_tokens=("",))

        assert table["grade"].to_list() == ["-", "A"]

    def test_native_numeric_next_to_a_null_token_is_not_rejected(self) -> None:
        # JSON/공공 API 레코드는 84.5를 네이티브 숫자로, 결측을 ""로 준다. 선언이
        # 테이블 생성 *뒤* 에 적용되면 이질 타입 가드(#187)가 먼저 걸려, 올바른
        # null_tokens 선언이 무관한 read_as 선언 없이는 통하지 않는다.
        bronze = _bronze(({"area": 84.5}, {"area": ""}))

        table = normalize_table(bronze, null_tokens=("",))

        assert table["area"].to_list() == [84.5, None]

    def test_native_numeric_next_to_a_null_token_casts_cleanly(self) -> None:
        bronze = _bronze(({"area": 84.5}, {"area": ""}))

        table = normalize_table(bronze, casts={"area": "float"}, null_tokens=("",))

        assert table["area"].to_list() == [84.5, None]


class TestColumnNullTokens:
    """같은 의미의 결측이 컬럼마다 다르게 표기되는 원천 (#623).

    전역 선언만으로는 **다른 컬럼의 의미를 바꾸지 않고** 그것을 표현할 수 없다.
    """

    def test_column_tokens_add_to_the_global_ones(self) -> None:
        bronze = _bronze(({"gender": "", "station": ""}, {"gender": "TOKEN", "station": "x"}))

        table = normalize_table(
            bronze, null_tokens=("TOKEN",), column_null_tokens={"gender": CNT(tokens=("",))}
        )

        # gender는 둘 다 결측, station의 빈 문자열은 값으로 남는다.
        assert table["gender"].to_list() == [None, None]
        assert table["station"].to_list() == ["", "x"]

    def test_column_tokens_do_not_replace_the_global_ones(self) -> None:
        """덮어쓰게 하면 토큰 하나를 더하려다 전역 토큰을 잃는 사고가 조용히 난다."""
        bronze = _bronze(({"gender": "TOKEN"}, {"gender": ""}, {"gender": "F"}))

        table = normalize_table(
            bronze, null_tokens=("TOKEN",), column_null_tokens={"gender": CNT(tokens=("",))}
        )

        assert table["gender"].to_list() == [None, None, "F"]

    def test_works_without_any_global_tokens(self) -> None:
        bronze = _bronze(({"gender": ""}, {"station": ""}))

        table = normalize_table(bronze, column_null_tokens={"gender": CNT(tokens=("",))})

        assert table["gender"].to_list() == [None, None]
        assert table["station"].to_list() == [None, ""]

    def test_absent_column_fails(self) -> None:
        """오타가 조용한 무동작이 되면 결측이 값으로 남은 채 지표가 세지 않는다."""
        bronze = _bronze(({"gender": ""},))

        with pytest.raises(TabularError, match="absent from the source"):
            normalize_table(bronze, column_null_tokens={"gendr": CNT(tokens=("",))})

    def test_absent_column_is_ignored_when_declared_optional(self) -> None:
        """결측 표기를 적어 둔 것이 그 컬럼이 반드시 있어야 한다는 주장은 아니다.

        원천이 진화해 컬럼이 사라질 수 있고, 그 부재 자체는 계약을 깨지 않는다.
        """
        bronze = _bronze(({"other": "x"},))

        table = normalize_table(
            bronze, column_null_tokens={"gender": CNT(tokens=("",), on_absent="ignore")}
        )

        assert table.columns == ["other"]

    def test_optional_column_still_gets_its_tokens_when_present(self) -> None:
        bronze = _bronze(({"gender": ""}, {"gender": "F"}))

        table = normalize_table(
            bronze, column_null_tokens={"gender": CNT(tokens=("",), on_absent="ignore")}
        )

        assert table["gender"].to_list() == [None, "F"]

    def test_shorthand_default_is_error(self) -> None:
        bronze = _bronze(({"other": "x"},))

        with pytest.raises(TabularError, match="on_absent: ignore"):
            normalize_table(bronze, column_null_tokens={"gender": CNT(tokens=("",))})

    def test_non_string_column_fails(self) -> None:
        bronze = _bronze(({"use_count": 1},))

        with pytest.raises(TabularError, match="non-string"):
            normalize_table(bronze, column_null_tokens={"use_count": CNT(tokens=("0",))})

    def test_all_null_column_is_allowed(self) -> None:
        """세대가 섞인 스냅샷에서 '컬럼은 있는데 값이 전부 없음'은 정상이다."""
        bronze = _bronze(({"gender": None}, {"gender": None}))

        table = normalize_table(bronze, column_null_tokens={"gender": CNT(tokens=("",))})

        assert table["gender"].to_list() == [None, None]

    def test_runs_before_coalesce(self) -> None:
        """결측이 값으로 남아 있으면 coalesce가 그것을 충돌로 본다."""
        bronze = _bronze(({"a": "", "b": "3"},))

        table = normalize_table(
            bronze, column_null_tokens={"a": CNT(tokens=("",))}, coalesce={"merged": ("a", "b")}
        )

        assert table["merged"].to_list() == ["3"]

    def test_keys_are_pre_rename_names(self) -> None:
        bronze = _bronze(({"성별": ""},))

        table = normalize_table(
            bronze, column_null_tokens={"성별": CNT(tokens=("",))}, rename={"성별": "gender"}
        )

        assert table["gender"].to_list() == [None]


class TestCoalesce:
    """세대별 alias 컬럼을 하나로 모은다 (#620).

    실패 조건이 본체다 — 조용한 first-wins는 세대 경계가 잘못 잡혔다는 유일한
    신호를 삼킨다.
    """

    def test_merges_generation_aliases_into_one_column(self) -> None:
        bronze = _bronze(
            (
                {"이동거리": "1210.0", "이동거리(M)": None},
                {"이동거리": None, "이동거리(M)": "980.0"},
            )
        )

        table = normalize_table(bronze, coalesce={"move_meter": ("이동거리", "이동거리(M)")})

        assert table["move_meter"].to_list() == ["1210.0", "980.0"]

    def test_converged_candidates_are_absorbed_into_the_canonical_column(self) -> None:
        """후보 컬럼은 canonical 컬럼에 흡수되어 사라진다.

        임의의 컬럼 삭제가 아니라 선언된 alias group 안으로 한정된다 — 선언되지
        않은 컬럼은 그대로 남는다.
        """
        bronze = _bronze(({"이동거리": "1210.0", "이동거리(M)": None, "keep": "x"},))

        table = normalize_table(bronze, coalesce={"move_meter": ("이동거리", "이동거리(M)")})

        assert "이동거리" not in table.columns
        assert "이동거리(M)" not in table.columns
        assert table.columns == ["keep", "move_meter"]

    def test_target_colliding_with_an_unrelated_column_fails(self) -> None:
        """alias group 밖의 컬럼을 말없이 덮어쓰면 수렴이 아니라 삭제가 된다."""
        bronze = _bronze(({"a": "1", "b": None, "merged": "keep me"},))

        with pytest.raises(TabularError, match="overwrite an existing column"):
            normalize_table(bronze, coalesce={"merged": ("a", "b")})

    def test_target_may_reuse_a_candidate_name(self) -> None:
        bronze = _bronze(({"a": "1", "b": None}, {"a": None, "b": "2"}))

        table = normalize_table(bronze, coalesce={"a": ("a", "b")})

        assert table["a"].to_list() == ["1", "2"]
        assert "b" not in table.columns

    def test_all_null_row_stays_null(self) -> None:
        bronze = _bronze(({"a": None, "b": None}, {"a": "x", "b": None}))

        table = normalize_table(bronze, coalesce={"merged": ("a", "b")})

        assert table["merged"].to_list() == [None, "x"]

    def test_agreeing_candidates_are_allowed(self) -> None:
        bronze = _bronze(({"a": "3", "b": "3"},))

        table = normalize_table(bronze, coalesce={"merged": ("a", "b")})

        assert table["merged"].to_list() == ["3"]

    def test_disagreeing_candidates_fail(self) -> None:
        bronze = _bronze(({"a": "3", "b": "4"},))

        with pytest.raises(TabularError, match="disagree"):
            normalize_table(bronze, coalesce={"merged": ("a", "b")})

    def test_no_candidate_present_fails(self) -> None:
        """조용히 all-null 컬럼을 만들면 소스가 통째로 빠진 것을 아무도 모른다."""
        bronze = _bronze(({"other": "1"},))

        with pytest.raises(TabularError, match="none of its candidates"):
            normalize_table(bronze, coalesce={"merged": ("a", "b")})

    def test_partial_presence_uses_what_exists(self) -> None:
        bronze = _bronze(({"a": "1"}, {"a": "2"}))

        table = normalize_table(bronze, coalesce={"merged": ("a", "b", "c")})

        assert table["merged"].to_list() == ["1", "2"]

    def test_differing_dtypes_fail(self) -> None:
        bronze = _bronze(({"a": "1", "b": None}, {"a": None, "b": 2}))

        with pytest.raises(TabularError, match="differing dtypes"):
            normalize_table(bronze, coalesce={"merged": ("a", "b")})

    def test_runs_after_null_tokens(self) -> None:
        r"""``\N``이 아직 문자열이면 coalesce가 그것을 값으로 보고 충돌시킨다."""
        bronze = _bronze(({"a": r"\N", "b": "3"},))

        table = normalize_table(bronze, null_tokens=(r"\N",), coalesce={"merged": ("a", "b")})

        assert table["merged"].to_list() == ["3"]

    def test_target_that_is_another_targets_candidate_is_rejected(self) -> None:
        # 각 규칙은 수렴한 후보를 지운다. 그래서 {"a": ["x"], "b": ["a"]}는 a,b
        # 순서에서는 성공하고 b,a 순서에서는 실패한다 — 그런데
        # canonical_spec_mapping()은 키를 정렬해 스냅샷을 쓰므로, 같은 digest의
        # 선언이 원래 빌드와 다르게 동작할 수 있다.
        bronze = _bronze(({"x": "1"},))

        with pytest.raises(TabularError, match="overlapping coalesce groups"):
            normalize_table(bronze, coalesce={"a": ("x",), "b": ("a",)})

    def test_declaration_order_does_not_change_the_rejection(self) -> None:
        bronze = _bronze(({"x": "1"},))

        with pytest.raises(TabularError, match="overlapping coalesce groups"):
            normalize_table(bronze, coalesce={"b": ("a",), "a": ("x",)})

    def test_candidate_shared_by_two_targets_is_rejected(self) -> None:
        bronze = _bronze(({"x": "1", "y": "2"},))

        with pytest.raises(TabularError, match="overlapping coalesce groups"):
            normalize_table(bronze, coalesce={"a": ("x", "y"), "b": ("y",)})

    def test_independent_groups_are_applied_in_a_stable_order(self) -> None:
        # 겹치지 않는 그룹은 어떤 선언 순서로도 같은 결과여야 한다.
        records = ({"old_id": "1", "legacy_name": "seoul"},)

        first = normalize_table(
            _bronze(records), coalesce={"id": ("old_id",), "name": ("legacy_name",)}
        )
        second = normalize_table(
            _bronze(records), coalesce={"name": ("legacy_name",), "id": ("old_id",)}
        )

        assert first.to_dicts() == second.to_dicts()


class TestZfill:
    def test_pads_identifier_to_declared_width(self) -> None:
        bronze = _bronze(({"station": "3"}, {"station": "00003"}, {"station": "102"}))

        table = normalize_table(bronze, zfill={"station": 5})

        assert table["station"].to_list() == ["00003", "00003", "00102"]

    def test_null_stays_null(self) -> None:
        """``00000``으로 채우면 결측이 유효한 식별자가 된다."""
        bronze = _bronze(({"station": None}, {"station": "3"}))

        table = normalize_table(bronze, zfill={"station": 5})

        assert table["station"].to_list() == [None, "00003"]

    def test_value_longer_than_width_fails(self) -> None:
        """계약이 width=5인데 6자리가 오는 것은 drift 신호다."""
        bronze = _bronze(({"station": "123456"},))

        with pytest.raises(TabularError, match="longer than the declared width"):
            normalize_table(bronze, zfill={"station": 5})

    def test_non_string_column_fails(self) -> None:
        bronze = _bronze(({"station": 3},))

        with pytest.raises(TabularError, match="declare read_as"):
            normalize_table(bronze, zfill={"station": 5})

    def test_all_null_column_is_promoted_instead_of_rejected(self) -> None:
        # 값이 전부 null이면 Polars는 pl.Null로 추론한다. read_as로도 풀 수 없고
        # (_apply_read_as는 null을 건드리지 않는다), zfill은 null을 null로 둔다고
        # 약속했으므로 거부할 이유가 없다.
        bronze = _bronze(({"station": None}, {"station": None}))

        table = normalize_table(bronze, zfill={"station": 5})

        assert table["station"].to_list() == [None, None]
        assert table.schema["station"] == pl.Utf8

    def test_all_null_coalesced_alias_can_be_zfilled(self) -> None:
        bronze = _bronze(({"legacy_station": None}, {"legacy_station": None}))

        table = normalize_table(
            bronze, coalesce={"station": ("legacy_station",)}, zfill={"station": 5}
        )

        assert table["station"].to_list() == [None, None]

    def test_absent_column_fails(self) -> None:
        bronze = _bronze(({"other": "1"},))

        with pytest.raises(TabularError, match="absent from the table"):
            normalize_table(bronze, zfill={"station": 5})

    def test_applies_to_renamed_name(self) -> None:
        """선언은 canonical 이름을 가리킨다 — rename 뒤에 적용된다."""
        bronze = _bronze(({"대여소번호": "3"},))

        table = normalize_table(
            bronze, rename={"대여소번호": "station_no"}, zfill={"station_no": 5}
        )

        assert table["station_no"].to_list() == ["00003"]


class TestYearMonthCast:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("2020-01", "2020-01"), ("202207", "2022-07"), ("2024-12", "2024-12")],
    )
    def test_accepts_both_notations(self, raw: str, expected: str) -> None:
        bronze = _bronze(({"ym": raw},))

        table = normalize_table(bronze, casts={"ym": "year_month"})

        assert table["ym"].to_list() == [expected]
        assert table.schema["ym"] == pl.Utf8

    def test_mixed_notations_in_one_column(self) -> None:
        """G2 안에서 형식이 바뀐다 — 단일 포맷 선언으로는 절반이 null이 된다."""
        bronze = _bronze(({"ym": "2022-06"}, {"ym": "202207"}))

        table = normalize_table(bronze, casts={"ym": "year_month"})

        assert table["ym"].to_list() == ["2022-06", "2022-07"]

    @pytest.mark.parametrize("raw", ["20230", "2023-1", "202313", "202200", "2023-13"])
    def test_rejects_malformed_values(self, raw: str) -> None:
        """느슨한 파서는 조용히 틀린 연/월을 만든다."""
        bronze = _bronze(({"ym": raw},))

        with pytest.raises(TabularError, match="year_month"):
            normalize_table(bronze, casts={"ym": "year_month"})

    def test_null_stays_null(self) -> None:
        bronze = _bronze(({"ym": None}, {"ym": "202301"}))

        table = normalize_table(bronze, casts={"ym": "year_month"})

        assert table["ym"].to_list() == [None, "2023-01"]


class TestErrorMessagesCarryNoSourceValues:
    """정규화 실패 메시지에 원천 값을 싣지 않는다 (#441).

    이 메시지들은 manifest 와 ``/builds`` 응답으로 나가는데 PII 스캔은 그보다
    뒤에 돈다. 값을 넣으면 스캔이 보기도 전에 새어나간다. 어느 컬럼에서 몇
    행인지면 고칠 곳을 찾기에 충분하다.
    """

    SECRET = "010-1234-5678"

    def test_zfill_overflow_reports_a_length_not_a_value(self) -> None:
        bronze = _bronze(({"station": self.SECRET},))

        with pytest.raises(TabularError) as exc:
            normalize_table(bronze, zfill={"station": 5})

        assert self.SECRET not in str(exc.value)
        assert "longer than the declared width" in str(exc.value)

    def test_year_month_rejection_reports_a_count_not_a_value(self) -> None:
        bronze = _bronze(({"ym": self.SECRET},))

        with pytest.raises(TabularError) as exc:
            normalize_table(bronze, casts={"ym": "year_month"})

        assert self.SECRET not in str(exc.value)

    def test_coalesce_disagreement_reports_columns_not_rows(self) -> None:
        bronze = _bronze(({"a": self.SECRET, "b": "other"},))

        with pytest.raises(TabularError) as exc:
            normalize_table(bronze, coalesce={"merged": ("a", "b")})

        assert self.SECRET not in str(exc.value)
        assert "disagree" in str(exc.value)

    def test_date_parts_loss_reports_columns_not_rows(self) -> None:
        from kpubdata_builder.spec import DerivedColumn

        bronze = _bronze(({"y": self.SECRET, "m": "13", "d": "40"},))

        with pytest.raises(TabularError) as exc:
            normalize_table(
                bronze,
                derived=(DerivedColumn(name="d8", kind="date_parts", columns=("y", "m", "d")),),
            )

        assert self.SECRET not in str(exc.value)
        assert "data loss" in str(exc.value)
