"""Silver 단계(#46): tabularize → validate → summarize → preview → persist 검증."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import cast

import polars as pl
import pytest

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
        from kpubdata_builder.errors import TabularError

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
    records_to_dataframe() → cast_columns()만 호출하고 validate_table()은
    테이블을 아예 건드리지 않으므로 오늘은 이 불변조건이 성립하지만, 향후 누군가
    Silver에 dedup/filter/reorder를 추가하면 이 테스트가 깨져서 pipeline/preview.py
    의 alignment 가정을 재검토하라는 신호를 준다.
    """

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
        from kpubdata_builder.errors import TabularError

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
