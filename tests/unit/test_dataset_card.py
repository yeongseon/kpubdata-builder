"""데이터셋 카드(#37) 계약과 Markdown 렌더링을 검증한다."""

from __future__ import annotations

from datetime import date, datetime

from kpubdata_builder.stages.gold import (
    DatasetCard,
    build_dataset_card,
    render_dataset_card,
)


def _card() -> DatasetCard:
    return build_dataset_card(
        title="Apartment Trades",
        description="seoul apartment trades",
        sources=("datago.apt_trade",),
        fields=[("id", "String", False), ("amount", "Int64", True)],
        sample_rows=[{"id": "1", "amount": 1000}, {"id": "2", "amount": 2500}],
        license="KOGL Type 1",
        version="2026.05.26",
    )


def test_build_dataset_card_maps_primitive_fields() -> None:
    # (name, type, nullable) 시퀀스가 CardField로 매핑되는지 확인한다.
    card = _card()

    assert [(f.name, f.type, f.nullable) for f in card.fields] == [
        ("id", "String", False),
        ("amount", "Int64", True),
    ]
    assert card.sources == ("datago.apt_trade",)


def test_render_includes_all_contract_sections() -> None:
    # 렌더링된 README가 계약의 모든 섹션을 포함하는지 확인한다.
    text = render_dataset_card(_card())

    assert text.startswith("# Apartment Trades\n")
    assert "seoul apartment trades" in text
    for heading in ("## Sources", "## Schema", "## Sample", "## License", "## Version"):
        assert heading in text
    assert "- datago.apt_trade" in text
    assert "KOGL Type 1" in text
    assert "2026.05.26" in text
    assert text.endswith("\n")


def test_render_schema_and_sample_as_markdown_tables() -> None:
    # 스키마/샘플이 Markdown 표로 렌더링되고 nullable 표기가 정확한지 확인한다.
    text = render_dataset_card(_card())

    assert "| Column | Type | Nullable |" in text
    assert "| id | String | no |" in text
    assert "| amount | Int64 | yes |" in text
    # 샘플 표 헤더와 행.
    assert "| id | amount |" in text
    assert "| 1 | 1000 |" in text


def test_render_escapes_pipe_and_newline_in_cells() -> None:
    # 셀 값의 파이프/개행이 이스케이프되어 표가 깨지지 않는지 확인한다.
    card = build_dataset_card(
        title="t",
        fields=[("v", "String", False)],
        sample_rows=[{"v": "a|b\nc"}],
    )

    text = render_dataset_card(card)

    assert "a\\|b c" in text


def test_render_handles_empty_schema_and_sample() -> None:
    # 스키마/샘플이 없을 때 안내 문구로 안전하게 렌더링되는지 확인한다.
    text = render_dataset_card(build_dataset_card(title="empty"))

    assert "_No schema available._" in text
    assert "_No sample rows available._" in text
    assert "N/A" in text  # license 기본값
    assert "unversioned" in text  # version 기본값


def test_render_serializes_temporal_sample_values() -> None:
    # 샘플 행에 date/datetime이 있어도 크래시 없이 ISO 문자열로 렌더링된다 (#195).
    card = build_dataset_card(
        title="temporal",
        fields=[("d", "Date", False), ("ts", "Datetime", False)],
        sample_rows=[{"d": date(2025, 1, 2), "ts": datetime(2025, 1, 2, 12, 30, 0)}],
    )

    text = render_dataset_card(card)

    assert "2025-01-02" in text
    assert "2025-01-02T12:30:00" in text
