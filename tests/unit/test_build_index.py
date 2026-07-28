"""SQLite 빌드 인덱스 테스트 (#309, ADR 0003)."""

from __future__ import annotations

import json
from pathlib import Path

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
            json.dumps({
                "run_id": "run1",
                "status": "ok",
                "started_at": "2025-01-01T10:00:00Z",
                "finished_at": "2025-01-01T10:05:00Z",
            })
        )

        (tmp_path / "run2").mkdir()
        (tmp_path / "run2" / "manifest.json").write_text(
            json.dumps({
                "run_id": "run2",
                "status": "failed",
                "errors": ["test error"],
                "started_at": "2025-01-01T11:00:00Z",
                "finished_at": "2025-01-01T11:05:00Z",
            })
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
            json.dumps({
                "run_id": "new",
                "status": "ok",
                "started_at": "2025-01-01T11:00:00Z",
                "finished_at": "2025-01-01T11:05:00Z",
            })
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
            json.dumps({
                "status": "ok",
                "started_at": "2025-01-01T10:00:00Z",
                "finished_at": "2025-01-01T10:05:00Z",
            })
        )

        (tmp_path / "bad").mkdir()
        (tmp_path / "bad" / "manifest.json").write_text("invalid json")

        count = rebuild_index(tmp_path)
        assert count == 1

        index = BuildIndex(tmp_path)
        builds = index.list_builds()
        assert len(builds) == 1
        assert builds[0].run_id == "good"
