"""두 빌드 매니페스트를 비교하는 diff 도구 (#16).

이 모듈은 두 BuildManifest를 받아 소스/레코드 수/산출물/오류·경고의 변화를
구조화된 차이 목록으로 반환한다. 빌드 간 무엇이 추가·삭제·변경되었는지 한눈에
파악할 수 있게 한다.

주요 구성:
    - DiffItem: 단일 변경 항목
    - BuildDiff: 비교 결과
    - compare_manifests: 두 BuildManifest 비교
"""

from __future__ import annotations

from dataclasses import dataclass

from .manifest import BuildManifest

ADDED = "added"
REMOVED = "removed"
MODIFIED = "modified"


@dataclass(frozen=True)
class DiffItem:
    """두 빌드 사이의 단일 변경 항목.

    속성:
        field: 변경된 항목 식별자 (예: "row_count:datago.apt_trade").
        old_value: 이전 값 (추가된 경우 빈 문자열).
        new_value: 새 값 (삭제된 경우 빈 문자열).
        change_type: "added" | "removed" | "modified".
    """

    field: str
    old_value: str
    new_value: str
    change_type: str


@dataclass(frozen=True)
class BuildDiff:
    """두 BuildManifest 비교 결과.

    속성:
        manifest_a: 기준(이전) 빌드 ID.
        manifest_b: 비교(이후) 빌드 ID.
        diffs: 변경 항목 목록 (결정적 순서).
        summary: 사람이 읽는 한 줄 요약.
    """

    manifest_a: str
    manifest_b: str
    diffs: tuple[DiffItem, ...]
    summary: str

    @property
    def changed(self) -> bool:
        """변경 항목이 하나라도 있으면 True."""
        return bool(self.diffs)


def _diff_set(field_prefix: str, before: tuple[str, ...], after: tuple[str, ...]) -> list[DiffItem]:
    """집합 형태 필드(소스/산출물)의 추가·삭제를 비교한다."""
    before_set, after_set = set(before), set(after)
    items = [
        DiffItem(field=f"{field_prefix}:{value}", old_value="", new_value=value, change_type=ADDED)
        for value in sorted(after_set - before_set)
    ]
    items += [
        DiffItem(
            field=f"{field_prefix}:{value}", old_value=value, new_value="", change_type=REMOVED
        )
        for value in sorted(before_set - after_set)
    ]
    return items


def _diff_row_counts(before: dict[str, int], after: dict[str, int]) -> list[DiffItem]:
    """소스별 레코드 수의 추가·삭제·변경을 비교한다."""
    items: list[DiffItem] = []
    for key in sorted(set(before) | set(after)):
        in_before, in_after = key in before, key in after
        if in_before and in_after:
            if before[key] != after[key]:
                items.append(
                    DiffItem(
                        field=f"row_count:{key}",
                        old_value=str(before[key]),
                        new_value=str(after[key]),
                        change_type=MODIFIED,
                    )
                )
        elif in_after:
            items.append(
                DiffItem(
                    field=f"row_count:{key}",
                    old_value="",
                    new_value=str(after[key]),
                    change_type=ADDED,
                )
            )
        else:
            items.append(
                DiffItem(
                    field=f"row_count:{key}",
                    old_value=str(before[key]),
                    new_value="",
                    change_type=REMOVED,
                )
            )
    return items


def compare_manifests(a: BuildManifest, b: BuildManifest) -> BuildDiff:
    """두 BuildManifest를 비교해 BuildDiff를 만든다.

    매개변수:
        a: 기준(이전) 매니페스트.
        b: 비교(이후) 매니페스트.

    반환값:
        BuildDiff: 결정적 순서의 변경 목록과 한 줄 요약.
    """
    diffs: list[DiffItem] = []
    diffs += _diff_set("source", a.inputs, b.inputs)
    diffs += _diff_row_counts(a.row_counts, b.row_counts)
    diffs += _diff_set("output", a.outputs, b.outputs)
    diffs += _diff_set("error", a.errors, b.errors)
    diffs += _diff_set("warning", a.warnings, b.warnings)

    if diffs:
        added = sum(1 for item in diffs if item.change_type == ADDED)
        removed = sum(1 for item in diffs if item.change_type == REMOVED)
        modified = sum(1 for item in diffs if item.change_type == MODIFIED)
        summary = f"{len(diffs)} change(s): {added} added, {removed} removed, {modified} modified"
    else:
        summary = "no changes"

    return BuildDiff(
        manifest_a=a.build_id, manifest_b=b.build_id, diffs=tuple(diffs), summary=summary
    )


__all__ = ["ADDED", "MODIFIED", "REMOVED", "BuildDiff", "DiffItem", "compare_manifests"]
