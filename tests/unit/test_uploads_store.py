"""uploads.store: SQLite 업로드 저장소의 owner 격리·크기 상한 검증 (#498)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder.spec.models import UPLOAD_ID_PATTERN
from kpubdata_builder.uploads import SQLiteUploadRepository
from kpubdata_builder.uploads.store import UploadContentCorrupted, generate_upload_id


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


class TestLargePayloadsSpillToFiles:
    """큰 payload 를 SQLite BLOB 이 아니라 파일로 쓴다 (#622).

    SQLite 의 기본 ``SQLITE_MAX_LENGTH`` 는 약 953 MiB 다. 실제로 945.6 MiB 는
    통과하고 1,444 MiB 는 ``string or blob too big`` 으로 실패한 사례가 있었다.
    문제는 한계의 크기가 아니라 **업로드 상한이 SQLite 의 단일 value 한계라는
    구현 세부로 우연히 정해졌다** 는 점이다.
    """

    THRESHOLD = 1024

    def _repo(self, tmp_path: Path) -> SQLiteUploadRepository:
        return SQLiteUploadRepository(
            tmp_path / "uploads.sqlite",
            max_bytes=10 * 1024 * 1024,
            spill_threshold_bytes=self.THRESHOLD,
        )

    @staticmethod
    def _blob_dir(tmp_path: Path) -> Path:
        return tmp_path / "uploads.sqlite.blobs"

    def test_a_small_upload_still_lives_in_the_database(self, tmp_path: Path) -> None:
        # 기존 동작이 회귀하지 않아야 한다 — 작은 업로드는 예전 경로 그대로다.
        repo = self._repo(tmp_path)

        meta = repo.put(
            "owner",
            content=b"a,b\n1,2\n",
            format="csv",
            encoding="utf-8",
            original_filename="s.csv",
        )

        assert repo.get_content("owner", meta.upload_id) == b"a,b\n1,2\n"
        assert not self._blob_dir(tmp_path).exists() or not list(
            self._blob_dir(tmp_path).glob("*.bin")
        )

    def test_a_large_upload_round_trips_through_a_file(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        content = b"x" * (self.THRESHOLD * 5)

        meta = repo.put(
            "owner", content=content, format="csv", encoding="utf-8", original_filename="b.csv"
        )

        assert repo.get_content("owner", meta.upload_id) == content
        assert len(list(self._blob_dir(tmp_path).glob("*.bin"))) == 1
        assert repo.get_metadata("owner", meta.upload_id).size_bytes == len(content)

    def test_another_owner_cannot_read_a_spilled_payload(self, tmp_path: Path) -> None:
        # 파일로 나갔다고 해서 소유권 게이트가 느슨해지면 안 된다.
        repo = self._repo(tmp_path)
        meta = repo.put(
            "owner",
            content=b"x" * (self.THRESHOLD * 5),
            format="csv",
            encoding="utf-8",
            original_filename="b.csv",
        )

        assert repo.get_content("intruder", meta.upload_id) is None

    def test_delete_removes_the_file_too(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        meta = repo.put(
            "owner",
            content=b"x" * (self.THRESHOLD * 5),
            format="csv",
            encoding="utf-8",
            original_filename="b.csv",
        )

        assert repo.delete("owner", meta.upload_id) is True
        assert list(self._blob_dir(tmp_path).glob("*.bin")) == []

    def test_a_tampered_payload_is_refused(self, tmp_path: Path) -> None:
        # 조용히 다른 바이트를 돌려주면, 그것으로 만든 Bronze 가 무엇이었는지
        # 아무도 알 수 없다.
        repo = self._repo(tmp_path)
        meta = repo.put(
            "owner",
            content=b"y" * (self.THRESHOLD * 5),
            format="csv",
            encoding="utf-8",
            original_filename="c.csv",
        )
        next(iter(self._blob_dir(tmp_path).glob("*.bin"))).write_bytes(b"z" * (self.THRESHOLD * 5))

        with pytest.raises(UploadContentCorrupted):
            repo.get_content("owner", meta.upload_id)

    def test_a_missing_payload_file_is_refused(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path)
        meta = repo.put(
            "owner",
            content=b"y" * (self.THRESHOLD * 5),
            format="csv",
            encoding="utf-8",
            original_filename="c.csv",
        )
        next(iter(self._blob_dir(tmp_path).glob("*.bin"))).unlink()

        with pytest.raises(UploadContentCorrupted):
            repo.get_content("owner", meta.upload_id)

    def test_the_max_size_policy_still_applies(self, tmp_path: Path) -> None:
        # 상한이 없어야 한다는 이야기가 아니다 — 정책으로 정해져야 한다는 것이다.
        repo = SQLiteUploadRepository(
            tmp_path / "uploads.sqlite", max_bytes=2048, spill_threshold_bytes=self.THRESHOLD
        )

        with pytest.raises(ValueError, match="exceeds max size"):
            repo.put(
                "owner",
                content=b"x" * 4096,
                format="csv",
                encoding="utf-8",
                original_filename="too-big.csv",
            )

    def test_an_existing_database_gains_the_new_columns(self, tmp_path: Path) -> None:
        # 기존 배포의 DB 를 열었을 때 마이그레이션이 조용히 돌아야 한다.
        db = tmp_path / "uploads.sqlite"
        first = SQLiteUploadRepository(db, spill_threshold_bytes=self.THRESHOLD)
        meta = first.put(
            "owner",
            content=b"a,b\n1,2\n",
            format="csv",
            encoding="utf-8",
            original_filename="s.csv",
        )

        second = SQLiteUploadRepository(db, spill_threshold_bytes=self.THRESHOLD)

        assert second.get_content("owner", meta.upload_id) == b"a,b\n1,2\n"
