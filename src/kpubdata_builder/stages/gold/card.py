"""데이터셋 카드(README) 계약과 Markdown 템플릿 (#37).

이 모듈은 빌드 산출물에 자동 생성되는 dataset card의 데이터 계약(DatasetCard)과,
이를 사람이 읽는 Markdown README로 렌더링하는 함수를 정의한다. 카드는 소스,
스키마 요약, 샘플 미리보기, 라이선스, 버전 정보를 담는다.

주요 구성:
    - CardField: 단일 컬럼 요약 (이름/타입/nullable)
    - DatasetCard: 데이터셋 카드 계약
    - build_dataset_card: 원시 입력 → DatasetCard
    - render_dataset_card: DatasetCard → Markdown 문자열
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from ...spec import JsonValue


@dataclass(frozen=True)
class CardField:
    """카드 스키마 섹션의 단일 컬럼.

    속성:
        name: 컬럼명.
        type: 컬럼 타입 문자열.
        nullable: null 허용 여부.
    """

    name: str
    type: str
    nullable: bool


@dataclass(frozen=True)
class DatasetCard:
    """데이터셋 카드(README) 계약.

    속성:
        title: 데이터셋 제목.
        description: 데이터셋 설명.
        sources: provider.dataset 형태 소스 식별자 목록.
        fields: 스키마 요약(컬럼) 목록.
        sample_rows: 미리보기 샘플 행.
        license: 라이선스 표기. 비어 있으면 "N/A"로 렌더링.
        version: 버전 표기. 비어 있으면 "unversioned"로 렌더링.
    """

    title: str
    description: str = ""
    sources: tuple[str, ...] = ()
    fields: tuple[CardField, ...] = ()
    sample_rows: tuple[dict[str, JsonValue], ...] = ()
    license: str = ""
    version: str = ""


def build_dataset_card(
    *,
    title: str,
    description: str = "",
    sources: Iterable[str] = (),
    fields: Iterable[tuple[str, str, bool]] = (),
    sample_rows: Iterable[Mapping[str, JsonValue]] = (),
    license: str = "",
    version: str = "",
) -> DatasetCard:
    """원시 입력으로부터 DatasetCard를 조립한다.

    매개변수:
        title: 데이터셋 제목.
        description: 데이터셋 설명.
        sources: provider.dataset 소스 식별자.
        fields: (name, type, nullable) 컬럼 시퀀스.
        sample_rows: 미리보기 샘플 행.
        license: 라이선스 표기.
        version: 버전 표기.

    반환값:
        DatasetCard: 렌더링 가능한 카드 계약.
    """
    return DatasetCard(
        title=title,
        description=description,
        sources=tuple(sources),
        fields=tuple(CardField(name=n, type=t, nullable=nl) for n, t, nl in fields),
        sample_rows=tuple(dict(row) for row in sample_rows),
        license=license,
        version=version,
    )


def _cell(value: object) -> str:
    """Markdown 표 셀로 안전한 문자열을 만든다(파이프/개행 이스케이프).

    sample_rows는 선언상 JsonValue지만, Silver 프리뷰에서 온 행은 date/datetime
    같은 시간적 Python 객체를 포함할 수 있으므로 object를 받아 폭넓게 처리한다.
    """
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (str, int, float)):
        text = str(value)
    elif isinstance(value, (date, datetime)):
        # 시간적 Python 객체는 직접 JSON 인코딩하면 실패하므로 Silver 직렬화기와
        # 동일하게 ISO 8601 문자열로 변환한다 (#195).
        text = value.isoformat()
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _render_schema(fields: Sequence[CardField]) -> list[str]:
    """스키마 요약 섹션을 렌더링한다."""
    lines = ["## Schema", ""]
    if not fields:
        lines.append("_No schema available._")
        return lines
    lines.append("| Column | Type | Nullable |")
    lines.append("| --- | --- | --- |")
    for column in fields:
        nullable = "yes" if column.nullable else "no"
        lines.append(f"| {_cell(column.name)} | {_cell(column.type)} | {nullable} |")
    return lines


def _render_sample(card: DatasetCard) -> list[str]:
    """샘플 미리보기 섹션을 렌더링한다."""
    lines = ["## Sample", ""]
    columns = [column.name for column in card.fields]
    if not columns or not card.sample_rows:
        lines.append("_No sample rows available._")
        return lines
    lines.append("| " + " | ".join(_cell(name) for name in columns) + " |")
    lines.append("| " + " | ".join("---" for _ in columns) + " |")
    for row in card.sample_rows:
        lines.append("| " + " | ".join(_cell(row.get(name)) for name in columns) + " |")
    return lines


def render_dataset_card(card: DatasetCard) -> str:
    """DatasetCard를 Markdown README 문자열로 렌더링한다.

    매개변수:
        card: 렌더링할 데이터셋 카드.

    반환값:
        str: 마지막 줄바꿈을 포함한 Markdown 문서.
    """
    lines: list[str] = [f"# {card.title}", ""]
    if card.description:
        lines += [card.description, ""]

    lines += ["## Sources", ""]
    if card.sources:
        lines += [f"- {source}" for source in card.sources]
    else:
        lines.append("_No sources recorded._")
    lines.append("")

    lines += _render_schema(card.fields)
    lines.append("")
    lines += _render_sample(card)
    lines.append("")

    lines += ["## License", "", card.license or "N/A", ""]
    lines += ["## Version", "", card.version or "unversioned"]
    return "\n".join(lines) + "\n"


__all__ = [
    "CardField",
    "DatasetCard",
    "build_dataset_card",
    "render_dataset_card",
]
