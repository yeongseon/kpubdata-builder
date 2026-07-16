"""Snapshot-aware build 지원(#15)을 검증한다."""

from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder.snapshot import (
    BuildSnapshot,
    compute_records_checksum,
    has_data_changed,
    load_snapshot,
    save_snapshot,
)


def test_checksum_is_reproducible_and_order_independent() -> None:
    a = compute_records_checksum([{"id": "1", "amount": 1000}])
    b = compute_records_checksum([{"amount": 1000, "id": "1"}])

    assert a == b
    assert a.startswith("sha256:")
    assert a != compute_records_checksum([{"id": "2", "amount": 1000}])


def test_checksum_is_record_order_independent() -> None:
    """행(레코드) 순서가 달라도 같은 데이터면 동일 체크섬이어야 한다 (#165)."""
    records = [{"id": "1", "amount": 1000}, {"id": "2", "amount": 2000}]
    reversed_records = list(reversed(records))

    assert compute_records_checksum(records) == compute_records_checksum(reversed_records)
    # 데이터 자체가 달라지면 여전히 달라야 한다.
    assert compute_records_checksum(records) != compute_records_checksum(
        [{"id": "1", "amount": 1000}, {"id": "3", "amount": 2000}]
    )


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    snapshot = BuildSnapshot(
        dataset_id="seoul_apt_trade",
        built_at="2026-05-26T01:00:00+00:00",
        data_checksum="sha256:abc",
        record_count=10,
        source_params={"LAWD_CD": "11680"},
    )

    path = save_snapshot(snapshot, root=tmp_path)

    assert path == tmp_path / ".kpubdata-builder/snapshots/seoul_apt_trade/snapshot.json"
    assert load_snapshot("seoul_apt_trade", root=tmp_path) == snapshot


def test_load_missing_snapshot_returns_none(tmp_path: Path) -> None:
    assert load_snapshot("never_built", root=tmp_path) is None


def test_load_corrupt_snapshot_returns_none(tmp_path: Path) -> None:
    # 잘린/비정상 JSON은 증분 빌드를 깨뜨리지 않고 "없음"으로 안전 저하 (#194).
    path = tmp_path / ".kpubdata-builder/snapshots/seoul_apt_trade/snapshot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text('{"dataset_id": "seoul_apt_trade", trunc', encoding="utf-8")

    assert load_snapshot("seoul_apt_trade", root=tmp_path) is None


def test_load_snapshot_missing_required_keys_returns_none(tmp_path: Path) -> None:
    # 형태가 어긋난(필수 키 누락) 스냅샷도 KeyError 대신 None으로 저하 (#194).
    path = tmp_path / ".kpubdata-builder/snapshots/seoul_apt_trade/snapshot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text('{"dataset_id": "seoul_apt_trade"}', encoding="utf-8")

    assert load_snapshot("seoul_apt_trade", root=tmp_path) is None


def test_load_snapshot_non_object_returns_none(tmp_path: Path) -> None:
    path = tmp_path / ".kpubdata-builder/snapshots/seoul_apt_trade/snapshot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text("[1, 2, 3]", encoding="utf-8")

    assert load_snapshot("seoul_apt_trade", root=tmp_path) is None


def test_save_rejects_unsafe_dataset_id(tmp_path: Path) -> None:
    snapshot = BuildSnapshot(dataset_id="../escape", built_at="t", data_checksum="sha256:x")

    with pytest.raises(ValueError, match="dataset_id"):
        save_snapshot(snapshot, root=tmp_path)


def test_has_data_changed_true_for_first_build() -> None:
    assert has_data_changed([{"id": "1"}], {}, None) is True


def test_has_data_changed_false_when_unchanged() -> None:
    records = [{"id": "1", "v": 10}]
    params = {"page": 1}
    snapshot = BuildSnapshot(
        dataset_id="d",
        built_at="t",
        data_checksum=compute_records_checksum(records),
        source_params=dict(params),
    )

    assert has_data_changed(records, params, snapshot) is False


def test_has_data_changed_true_when_records_differ() -> None:
    snapshot = BuildSnapshot(
        dataset_id="d",
        built_at="t",
        data_checksum=compute_records_checksum([{"id": "1"}]),
        source_params={},
    )

    assert has_data_changed([{"id": "2"}], {}, snapshot) is True


def test_has_data_changed_true_when_params_differ() -> None:
    records = [{"id": "1"}]
    snapshot = BuildSnapshot(
        dataset_id="d",
        built_at="t",
        data_checksum=compute_records_checksum(records),
        source_params={"page": 1},
    )

    assert has_data_changed(records, {"page": 2}, snapshot) is True
