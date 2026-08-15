"""SQLite 빌드 인덱스 테스트 (#309, ADR 0003)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from kpubdata_builder.store import SCHEMA_VERSION, BuildIndex, rebuild_index


class TestBuildIndex:
    """BuildIndex 단위 테스트."""

    def test_init_creates_database(self, tmp_path: Path) -> None:
        """초기화 시 데이터베이스와 스키마가 생성된다."""
        index = BuildIndex(tmp_path)
        assert (tmp_path / "_builds.sqlite").exists()

        # 스키마 버전 확인
        cur = index._conn.execute("SELECT version FROM schema_version")
        assert cur.fetchone()[0] == SCHEMA_VERSION

        # builds 테이블 확인
        cur = index._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='builds'"
        )
        assert cur.fetchone() is not None

    def test_insert_and_retrieve(self, tmp_path: Path) -> None:
        """엔트리 삽입 후 조회가 가능하다."""
        index = BuildIndex(tmp_path)

        index.insert_or_replace(
            run_id="test1",
            status="ok",
            started_at="2025-01-01T10:00:00Z",
            finished_at="2025-01-01T10:05:00Z",
        )

        entry = index.get("test1")
        assert entry is not None
        assert entry.run_id == "test1"
        assert entry.status == "ok"
        assert entry.started_at == "2025-01-01T10:00:00Z"
        assert entry.finished_at == "2025-01-01T10:05:00Z"

    def test_insert_or_replace_updates_existing(self, tmp_path: Path) -> None:
        """insert_or_replace는 기존 엔트리를 갱신한다."""
        index = BuildIndex(tmp_path)

        index.insert_or_replace(
            run_id="test1",
            status="ok",
            started_at="2025-01-01T10:00:00Z",
            finished_at="2025-01-01T10:05:00Z",
        )

        # 상태 변경 후 재삽입
        index.insert_or_replace(
            run_id="test1",
            status="failed",
            started_at="2025-01-01T10:00:00Z",
            finished_at="2025-01-01T10:05:00Z",
            error="test error",
        )

        entry = index.get("test1")
        assert entry is not None
        assert entry.status == "failed"
        assert entry.error == "test error"

    def test_list_builds_orders_by_finished_at_desc(self, tmp_path: Path) -> None:
        """list_builds는 finished_at 기준 내림차순으로 반환한다."""
        index = BuildIndex(tmp_path)

        index.insert_or_replace(
            run_id="old",
            status="ok",
            started_at="2025-01-01T09:00:00Z",
            finished_at="2025-01-01T09:05:00Z",
        )
        index.insert_or_replace(
            run_id="new",
            status="ok",
            started_at="2025-01-01T11:00:00Z",
            finished_at="2025-01-01T11:05:00Z",
        )
        index.insert_or_replace(
            run_id="mid",
            status="ok",
            started_at="2025-01-01T10:00:00Z",
            finished_at="2025-01-01T10:05:00Z",
        )

        builds = index.list_builds()
        assert len(builds) == 3
        assert builds[0].run_id == "new"
        assert builds[1].run_id == "mid"
        assert builds[2].run_id == "old"

    def test_list_builds_respects_limit(self, tmp_path: Path) -> None:
        """list_builds는 limit 매개변수를 존중한다."""
        index = BuildIndex(tmp_path)

        for i in range(5):
            index.insert_or_replace(
                run_id=f"run{i}",
                status="ok",
                started_at="2025-01-01T10:00:00Z",
                finished_at=f"2025-01-01T1{i}:00:00Z",
            )

        builds = index.list_builds(limit=3)
        assert len(builds) == 3

    def test_delete_removes_entry(self, tmp_path: Path) -> None:
        """delete는 엔트리를 삭제한다."""
        index = BuildIndex(tmp_path)

        index.insert_or_replace(
            run_id="test1",
            status="ok",
            started_at="2025-01-01T10:00:00Z",
            finished_at="2025-01-01T10:05:00Z",
        )

        index.delete("test1")
        assert index.get("test1") is None

    def test_get_returns_none_for_missing(self, tmp_path: Path) -> None:
        """get는 미존재 엔트리에 대해 None을 반환한다."""
        index = BuildIndex(tmp_path)
        assert index.get("nonexistent") is None

    def test_schema_version_upgrade_recreates_table(self, tmp_path: Path) -> None:
        """스키마 버전이 변경되면 테이블이 재생성된다."""
        # 첫 번째 인덱스 생성
        index1 = BuildIndex(tmp_path)
        index1.insert_or_replace(
            run_id="old",
            status="ok",
            started_at="2025-01-01T10:00:00Z",
            finished_at="2025-01-01T10:05:00Z",
        )
        index1.close()

        # 스키마 버전을 조작하여 업그레이드 시뮬레이션
        import sqlite3

        conn = sqlite3.connect(tmp_path / "_builds.sqlite")
        conn.execute("UPDATE schema_version SET version = 0")
        conn.commit()
        conn.close()

        # 새 인덱스 (스키마 재생성)
        index2 = BuildIndex(tmp_path)

        # 이전 데이터는 삭제되어야 함
        assert index2.get("old") is None

        # 새 버전 확인
        cur = index2._conn.execute("SELECT version FROM schema_version")
        assert cur.fetchone()[0] == SCHEMA_VERSION


class TestBuildIndexDatasetId:
    """dataset_id 파생 컬럼과 조회 (#488)."""

    def test_insert_and_retrieve_dataset_id(self, tmp_path: Path) -> None:
        index = BuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="run1",
            status="ok",
            started_at="2025-01-01T10:00:00Z",
            finished_at="2025-01-01T10:05:00Z",
            dataset_id="dataset.sample",
        )
        entry = index.get("run1")
        assert entry is not None
        assert entry.dataset_id == "dataset.sample"

    def test_dataset_id_defaults_to_none(self, tmp_path: Path) -> None:
        index = BuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="legacy",
            status="ok",
            started_at="2025-01-01T10:00:00Z",
            finished_at="2025-01-01T10:05:00Z",
        )
        entry = index.get("legacy")
        assert entry is not None
        assert entry.dataset_id is None

    def test_list_by_dataset_filters_and_orders(self, tmp_path: Path) -> None:
        index = BuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="a-old",
            status="ok",
            started_at="2025-01-01T09:00:00Z",
            finished_at="2025-01-01T09:05:00Z",
            dataset_id="dataset.a",
        )
        index.insert_or_replace(
            run_id="a-new",
            status="ok",
            started_at="2025-01-01T11:00:00Z",
            finished_at="2025-01-01T11:05:00Z",
            dataset_id="dataset.a",
        )
        index.insert_or_replace(
            run_id="b-only",
            status="ok",
            started_at="2025-01-01T10:00:00Z",
            finished_at="2025-01-01T10:05:00Z",
            dataset_id="dataset.b",
        )

        results = index.list_by_dataset("dataset.a")
        assert [r.run_id for r in results] == ["a-new", "a-old"]

        assert index.list_by_dataset("dataset.unknown") == []

    def test_unbounded_dataset_query_returns_more_than_500_rows(self, tmp_path: Path) -> None:
        index = BuildIndex(tmp_path)
        for number in range(505):
            index.insert_or_replace(
                run_id=f"run-{number:03d}",
                status="ok",
                started_at="2025-01-01T00:00:00Z",
                finished_at=f"2025-01-01T00:{number // 60:02d}:{number % 60:02d}Z",
                dataset_id="dataset.bulk",
            )

        assert len(index.list_by_dataset("dataset.bulk", limit=None)) == 505
        assert len(index.list_builds(limit=None)) == 505

    def test_list_by_dataset_respects_limit(self, tmp_path: Path) -> None:
        index = BuildIndex(tmp_path)
        for i in range(5):
            index.insert_or_replace(
                run_id=f"run{i}",
                status="ok",
                started_at="2025-01-01T10:00:00Z",
                finished_at=f"2025-01-01T1{i}:00:00Z",
                dataset_id="dataset.many",
            )
        assert len(index.list_by_dataset("dataset.many", limit=2)) == 2

    def test_list_builds_includes_dataset_id(self, tmp_path: Path) -> None:
        index = BuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="run1",
            status="ok",
            started_at="2025-01-01T10:00:00Z",
            finished_at="2025-01-01T10:05:00Z",
            dataset_id="dataset.sample",
        )
        builds = index.list_builds()
        assert builds[0].dataset_id == "dataset.sample"


class TestRebuildIndex:
    """rebuild_index 함수 테스트."""

    def test_rebuild_from_empty_directory(self, tmp_path: Path) -> None:
        """빈 디렉터리에서는 빈 인덱스가 생성된다."""
        count = rebuild_index(tmp_path)
        assert count == 0

        index = BuildIndex(tmp_path)
        assert index.list_builds() == []

    def test_rebuild_scans_manifest_files(self, tmp_path: Path) -> None:
        """파일시스템의 manifest.json을 스캔하여 인덱스를 재구축한다."""
        # 가짜 manifest 파일들 생성
        (tmp_path / "run1").mkdir()
        (tmp_path / "run1" / "manifest.json").write_text(
            json.dumps(
                {
                    "run_id": "run1",
                    "status": "ok",
                    "started_at": "2025-01-01T10:00:00Z",
                    "finished_at": "2025-01-01T10:05:00Z",
                }
            )
        )

        (tmp_path / "run2").mkdir()
        (tmp_path / "run2" / "manifest.json").write_text(
            json.dumps(
                {
                    "run_id": "run2",
                    "status": "failed",
                    "errors": ["test error"],
                    "started_at": "2025-01-01T11:00:00Z",
                    "finished_at": "2025-01-01T11:05:00Z",
                }
            )
        )

        # manifest 없는 디렉터리
        (tmp_path / "no-manifest").mkdir()

        count = rebuild_index(tmp_path)
        assert count == 2

        index = BuildIndex(tmp_path)
        builds = index.list_builds()
        assert len(builds) == 2

        # run_id로 정렬 확인 (finished_at DESC)
        assert builds[0].run_id == "run2"
        assert builds[1].run_id == "run1"

    def test_rebuild_indexes_digest_from_snapshot_bytes(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text(
            json.dumps({"started_at": "a", "finished_at": "b"}), encoding="utf-8"
        )
        payload = b"dataset_id: d\n"
        (run_dir / "buildspec.yaml").write_bytes(payload)

        assert rebuild_index(tmp_path) == 1

        entry = BuildIndex(tmp_path).get("run1")
        assert entry is not None
        assert entry.spec_digest == f"sha256:{hashlib.sha256(payload).hexdigest()}"

    def test_rebuild_keeps_legacy_snapshot_digest_null(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "legacy"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text("{}", encoding="utf-8")

        assert rebuild_index(tmp_path) == 1
        entry = BuildIndex(tmp_path).get("legacy")
        assert entry is not None
        assert entry.spec_digest is None

    def test_rebuild_restores_dataset_id_from_snapshot(self, tmp_path: Path) -> None:
        """rebuild_index가 buildspec.yaml에서 dataset_id를 안전하게 복원한다 (#488)."""
        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text(
            json.dumps({"started_at": "a", "finished_at": "b"}), encoding="utf-8"
        )
        (run_dir / "buildspec.yaml").write_bytes(b"dataset_id: dataset.restored\ntitle: t\n")

        assert rebuild_index(tmp_path) == 1
        entry = BuildIndex(tmp_path).get("run1")
        assert entry is not None
        assert entry.dataset_id == "dataset.restored"

    def test_rebuild_leaves_dataset_id_null_for_legacy_run(self, tmp_path: Path) -> None:
        """snapshot이 없는 legacy run의 dataset_id는 추측하지 않는다 (#488)."""
        run_dir = tmp_path / "legacy"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text("{}", encoding="utf-8")

        assert rebuild_index(tmp_path) == 1
        entry = BuildIndex(tmp_path).get("legacy")
        assert entry is not None
        assert entry.dataset_id is None

    def test_rebuild_leaves_dataset_id_null_for_corrupt_snapshot(self, tmp_path: Path) -> None:
        """snapshot이 있어도 dataset_id를 읽거나 파싱할 수 없으면 None으로 남긴다 (#488)."""
        run_dir = tmp_path / "corrupt"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
        (run_dir / "buildspec.yaml").write_bytes(b"\xff\xfe\x00")

        assert rebuild_index(tmp_path) == 1
        entry = BuildIndex(tmp_path).get("corrupt")
        assert entry is not None
        assert entry.dataset_id is None

    def test_rebuild_skips_symlinked_snapshot(self, tmp_path: Path) -> None:
        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        (run_dir / "manifest.json").write_text(
            json.dumps({"started_at": "a", "finished_at": "b"}), encoding="utf-8"
        )
        outside = tmp_path / "outside.yaml"
        outside.write_bytes(b"dataset_id: evil\n")
        (run_dir / "buildspec.yaml").symlink_to(outside)

        assert rebuild_index(tmp_path) == 1

        entry = BuildIndex(tmp_path).get("run1")
        assert entry is not None
        assert entry.spec_digest is None
        assert entry.dataset_id is None

    def test_rebuild_isolates_empty_corrupt_and_unreadable_snapshots(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payloads: dict[str, bytes | None] = {
            "normal": b"dataset_id: normal\n",
            "empty": b"",
            "invalid-utf8": b"\xff\xfe\x00",
            "unreadable": b"dataset_id: unreadable\n",
        }
        for run_id, payload in payloads.items():
            run_dir = tmp_path / run_id
            run_dir.mkdir()
            (run_dir / "manifest.json").write_text("{}", encoding="utf-8")
            if payload is not None:
                (run_dir / "buildspec.yaml").write_bytes(payload)

        original_read_bytes = Path.read_bytes

        def read_bytes_or_fail(path: Path) -> bytes:
            if path == tmp_path / "unreadable" / "buildspec.yaml":
                raise PermissionError("snapshot unreadable")
            return original_read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", read_bytes_or_fail)

        assert rebuild_index(tmp_path) == len(payloads)

        index = BuildIndex(tmp_path)
        normal = index.get("normal")
        empty = index.get("empty")
        invalid = index.get("invalid-utf8")
        unreadable = index.get("unreadable")
        assert normal is not None
        assert empty is not None
        assert invalid is not None
        assert unreadable is not None
        assert normal.spec_digest == f"sha256:{hashlib.sha256(payloads['normal']).hexdigest()}"
        assert empty.spec_digest == f"sha256:{hashlib.sha256(b'').hexdigest()}"
        assert invalid.spec_digest == (
            f"sha256:{hashlib.sha256(payloads['invalid-utf8']).hexdigest()}"
        )
        assert unreadable.spec_digest is None

    def test_rebuild_replaces_existing_index(self, tmp_path: Path) -> None:
        """rebuild는 기존 인덱스를 삭제하고 다시 생성한다."""
        # 먼저 인덱스 생성
        index = BuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="old",
            status="ok",
            started_at="2025-01-01T09:00:00Z",
            finished_at="2025-01-01T09:05:00Z",
        )
        index.close()

        # manifest 파일 생성
        (tmp_path / "new").mkdir()
        (tmp_path / "new" / "manifest.json").write_text(
            json.dumps(
                {
                    "run_id": "new",
                    "status": "ok",
                    "started_at": "2025-01-01T11:00:00Z",
                    "finished_at": "2025-01-01T11:05:00Z",
                }
            )
        )

        count = rebuild_index(tmp_path)
        assert count == 1

        index = BuildIndex(tmp_path)
        builds = index.list_builds()
        assert len(builds) == 1
        assert builds[0].run_id == "new"
        assert index.get("old") is None

    def test_rebuild_skips_malformed_manifest(self, tmp_path: Path) -> None:
        """손상된 manifest는 건너뛴다."""
        (tmp_path / "good").mkdir()
        (tmp_path / "good" / "manifest.json").write_text(
            json.dumps(
                {
                    "status": "ok",
                    "started_at": "2025-01-01T10:00:00Z",
                    "finished_at": "2025-01-01T10:05:00Z",
                }
            )
        )

        (tmp_path / "bad").mkdir()
        (tmp_path / "bad" / "manifest.json").write_text("invalid json")
        (tmp_path / "binary").mkdir()
        (tmp_path / "binary" / "manifest.json").write_bytes(b"\xff\xfe\x00binary")

        count = rebuild_index(tmp_path)
        assert count == 1

        index = BuildIndex(tmp_path)
        builds = index.list_builds()
        assert len(builds) == 1
        assert builds[0].run_id == "good"

    def test_rebuild_leaves_no_tmp_or_bak_after_success(self, tmp_path: Path) -> None:
        """정상 재구축 후에는 .tmp/.bak 잔여 파일이 남지 않는다 (#366)."""
        index = BuildIndex(tmp_path)
        index.close()

        rebuild_index(tmp_path)

        assert (tmp_path / "_builds.sqlite").exists()
        assert not (tmp_path / "_builds.sqlite.tmp").exists()
        assert not (tmp_path / "_builds.sqlite.bak").exists()

    def test_rebuild_cleans_up_stale_tmp_file(self, tmp_path: Path) -> None:
        """이전 실행이 중단되어 남은 .tmp 파일이 있어도 재구축은 정상 동작한다 (#366)."""
        stale_tmp = tmp_path / "_builds.sqlite.tmp"
        stale_tmp.write_text("stale garbage")

        count = rebuild_index(tmp_path)

        assert count == 0
        assert not stale_tmp.exists()

    def test_rebuild_restores_backup_when_swap_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """.tmp -> 원본 교체가 실패하면 기존 인덱스를 백업에서 복원한다 (#366)."""
        index = BuildIndex(tmp_path)
        index.insert_or_replace(
            run_id="old",
            status="ok",
            started_at="2025-01-01T09:00:00Z",
            finished_at="2025-01-01T09:05:00Z",
        )
        index.close()

        index_path = tmp_path / "_builds.sqlite"
        tmp_index_path = tmp_path / "_builds.sqlite.tmp"
        original_rename = Path.rename

        def flaky_rename(self: Path, target: Path) -> Path:
            if self == tmp_index_path:
                raise OSError("simulated rename failure")
            return original_rename(self, target)

        monkeypatch.setattr(Path, "rename", flaky_rename)

        with pytest.raises(OSError):
            rebuild_index(tmp_path)

        monkeypatch.undo()

        assert index_path.exists()
        assert not (tmp_path / "_builds.sqlite.bak").exists()

        restored = BuildIndex(tmp_path)
        assert restored.get("old") is not None
