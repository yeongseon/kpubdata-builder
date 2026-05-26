"""정적 데이터 카탈로그 페이지 생성기 (#42).

빌드 매니페스트를 기반으로 브랜드 배포용 정적 HTML 카탈로그 페이지를 만든다.
각 데이터셋의 제목·설명·레코드 수·소스 수·산출물 목록을 카드로 나열한다.
모든 동적 텍스트는 HTML 이스케이프되어 안전하게 삽입된다.

주요 구성:
    - CatalogEntry: 카탈로그 한 항목
    - catalog_entry_from_manifest: BuildManifest → CatalogEntry
    - render_catalog_html: 항목 목록 → 완성된 HTML 문서
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html import escape

from .manifest import BuildManifest


@dataclass(frozen=True)
class CatalogEntry:
    """카탈로그 페이지의 단일 데이터셋 항목.

    속성:
        dataset_id: 데이터셋 식별자.
        title: 표시 제목.
        description: 설명.
        record_count: 총 레코드 수.
        source_count: 소스 수.
        outputs: 산출물 경로 목록.
    """

    dataset_id: str
    title: str
    description: str = ""
    record_count: int = 0
    source_count: int = 0
    outputs: tuple[str, ...] = field(default_factory=tuple)


def catalog_entry_from_manifest(
    manifest: BuildManifest,
    *,
    dataset_id: str,
    title: str,
    description: str = "",
) -> CatalogEntry:
    """BuildManifest에서 카탈로그 항목을 파생한다.

    레코드 수는 row_counts 합, 소스 수는 inputs 길이로 계산한다.

    매개변수:
        manifest: 통계를 가져올 빌드 매니페스트.
        dataset_id: 데이터셋 식별자.
        title: 표시 제목.
        description: 설명.

    반환값:
        CatalogEntry: 렌더링 가능한 카탈로그 항목.
    """
    return CatalogEntry(
        dataset_id=dataset_id,
        title=title,
        description=description,
        record_count=sum(manifest.row_counts.values()),
        source_count=len(manifest.inputs),
        outputs=tuple(manifest.outputs),
    )


def _render_entry(entry: CatalogEntry) -> list[str]:
    """단일 카탈로그 항목을 HTML 카드로 렌더링한다."""
    lines = [
        '    <article class="dataset-card">',
        f"      <h2>{escape(entry.title)}</h2>",
        f'      <p class="dataset-id">{escape(entry.dataset_id)}</p>',
    ]
    if entry.description:
        lines.append(f"      <p>{escape(entry.description)}</p>")
    lines += [
        '      <ul class="stats">',
        f"        <li>Records: {entry.record_count}</li>",
        f"        <li>Sources: {entry.source_count}</li>",
        f"        <li>Outputs: {len(entry.outputs)}</li>",
        "      </ul>",
    ]
    if entry.outputs:
        lines.append('      <ul class="outputs">')
        lines += [f"        <li>{escape(path)}</li>" for path in entry.outputs]
        lines.append("      </ul>")
    lines.append("    </article>")
    return lines


def render_catalog_html(entries: list[CatalogEntry], *, site_title: str = "Data Catalog") -> str:
    """카탈로그 항목 목록을 완성된 정적 HTML 문서로 렌더링한다.

    매개변수:
        entries: 표시할 카탈로그 항목 (주어진 순서를 보존).
        site_title: 페이지 제목.

    반환값:
        str: 마지막 줄바꿈을 포함한 HTML 문서.
    """
    head = [
        "<!DOCTYPE html>",
        '<html lang="ko">',
        "<head>",
        '  <meta charset="utf-8">',
        f"  <title>{escape(site_title)}</title>",
        "</head>",
        "<body>",
        f"  <h1>{escape(site_title)}</h1>",
        f'  <p class="count">{len(entries)} dataset(s)</p>',
        '  <main class="catalog">',
    ]
    body: list[str] = []
    if entries:
        for entry in entries:
            body += _render_entry(entry)
    else:
        body.append("    <p>No datasets available.</p>")
    tail = ["  </main>", "</body>", "</html>"]
    return "\n".join(head + body + tail) + "\n"


__all__ = ["CatalogEntry", "catalog_entry_from_manifest", "render_catalog_html"]
