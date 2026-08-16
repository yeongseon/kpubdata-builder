"""uploads.store: SQLite 업로드 저장소의 owner 격리·크기 상한 검증 (#498)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder.spec.models import UPLOAD_ID_PATTERN
from kpubdata_builder.uploads import SQLiteUploadRepository
from kpubdata_builder.uploads.store import generate_upload_id


def _repo(tmp_path: Path, *, max_bytes: int = 1024) -> SQLiteUploadRepository:
    return SQLiteUploadRepository(tmp_path / "uploads.sqlite3", max_bytes=max_bytes)


def test_generate_upload_id_matches_contract_pattern() -> None:
    for _ in range(20):
        upload_id = generate_upload_id()
        assert UPLOAD_ID_PATTERN.match(upload_id)


def test_generate_upload_id_is_unique() -> None:
    ids = {generate_upload_id() for _ in range(50)}
    assert len(ids) == 50


def test_put_then_get_metadata_and_content_round_trips(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    metadata = repo.put(
        "owner-1", content=b"a,b\n1,2\n", format="csv", encoding="utf-8", original_filename="t.csv"
    )

    assert UPLOAD_ID_PATTERN.match(metadata.upload_id)
    assert metadata.format == "csv"
    assert metadata.encoding == "utf-8"
    assert metadata.size_bytes == len(b"a,b\n1,2\n")
    assert metadata.original_filename == "t.csv"
    assert metadata.created_at

    fetched_metadata = repo.get_metadata("owner-1", metadata.upload_id)
    assert fetched_metadata == metadata

    content = repo.get_content("owner-1", metadata.upload_id)
    assert content == b"a,b\n1,2\n"


def test_get_metadata_and_content_isolated_by_owner(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    metadata = repo.put(
        "owner-1", content=b"data", format="csv", encoding="utf-8", original_filename=None
    )

    # 다른 owner는 존재 자체를 모른다 — None(=404)만 볼 수 있다(fail-closed).
    assert repo.get_metadata("owner-2", metadata.upload_id) is None
    assert repo.get_content("owner-2", metadata.upload_id) is None
    assert repo.delete("owner-2", metadata.upload_id) is False

    # 진짜 owner는 정상 접근 가능하다.
    assert repo.get_metadata("owner-1", metadata.upload_id) is not None


def test_get_metadata_returns_none_for_unknown_id(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert repo.get_metadata("owner-1", "upl_" + "0" * 32) is None


def test_delete_removes_upload_and_is_idempotent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    metadata = repo.put(
        "owner-1", content=b"data", format="csv", encoding="utf-8", original_filename=None
    )

    assert repo.delete("owner-1", metadata.upload_id) is True
    assert repo.get_metadata("owner-1", metadata.upload_id) is None
    # 두 번째 삭제는 아무것도 지울 게 없어 False.
    assert repo.delete("owner-1", metadata.upload_id) is False


def test_list_for_owner_returns_only_that_owners_uploads(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    m1 = repo.put("owner-1", content=b"1", format="csv", encoding="utf-8", original_filename=None)
    repo.put("owner-2", content=b"2", format="csv", encoding="utf-8", original_filename=None)
    m3 = repo.put("owner-1", content=b"3", format="json", encoding="utf-8", original_filename=None)

    owner_1_uploads = repo.list_for_owner("owner-1")

    assert {m.upload_id for m in owner_1_uploads} == {m1.upload_id, m3.upload_id}


def test_put_rejects_empty_content(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(ValueError, match="empty"):
        repo.put("owner-1", content=b"", format="csv", encoding="utf-8", original_filename=None)


def test_put_rejects_content_over_max_bytes(tmp_path: Path) -> None:
    repo = _repo(tmp_path, max_bytes=10)
    with pytest.raises(ValueError, match="exceeds max size"):
        repo.put(
            "owner-1", content=b"x" * 11, format="csv", encoding="utf-8", original_filename=None
        )


def test_put_rejects_unsupported_format(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(ValueError, match="format"):
        repo.put(
            "owner-1", content=b"data", format="xlsx", encoding="utf-8", original_filename=None
        )


def test_put_rejects_missing_owner_id(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    with pytest.raises(ValueError, match="owner_id"):
        repo.put("", content=b"data", format="csv", encoding="utf-8", original_filename=None)


def test_put_sanitizes_display_filename_to_basename(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    metadata = repo.put(
        "owner-1",
        content=b"data",
        format="csv",
        encoding="utf-8",
        original_filename="../../etc/passwd",
    )

    # basename만 남는다 — 이 값은 표시 전용이며 파일시스템 경로로 쓰이지 않는다.
    assert metadata.original_filename == "passwd"


def test_put_treats_blank_filename_as_none(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    metadata = repo.put(
        "owner-1", content=b"data", format="csv", encoding="utf-8", original_filename="   "
    )

    assert metadata.original_filename is None


def test_repository_persists_across_reopen(tmp_path: Path) -> None:
    db_path = tmp_path / "uploads.sqlite3"
    first = SQLiteUploadRepository(db_path, max_bytes=1024)
    metadata = first.put(
        "owner-1", content=b"data", format="csv", encoding="utf-8", original_filename=None
    )

    second = SQLiteUploadRepository(db_path, max_bytes=1024)
    assert second.get_content("owner-1", metadata.upload_id) == b"data"
