"""SQLite 빌드 인덱스 테스트 (#328)."""

from __future__ import annotations

from pathlib import Path

from kpubdata_builder.index import _SCHEMA_VERSION, BuildIndex, initialize_schema


class TestBuildIndexSchema:
    """스키마 생성 및 초기화 테스트."""

    def test_initialize_schema_creates_database(self, tmp_path: Path) -> None:
        db_path = tmp_path / "_builds.sqlite"

        initialize_schema(db_path)

        assert db_path.exists()

    def test_initialize_schema_creates_builds_table(self, tmp_path: Path) -> None:
        import sqlite3

        db_path = tmp_path / "_builds.sqlite"
        initialize_schema(db_path)

        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='builds'")
            tables = cursor.fetchall()

        assert len(tables) == 1
        assert tables[0][0] == "builds"

    def test_builds_table_has_all_columns(self, tmp_path: Path) -> None:
        import sqlite3

        db_path = tmp_path / "_builds.sqlite"
        initialize_schema(db_path)

        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(builds)")
            columns = {row[1] for row in cursor.fetchall()}

        expected_columns = {
            "run_id",
            "status",
            "started_at",
            "finished_at",
            "spec_digest",
            "error",
            "schema_version",
        }
        assert columns == expected_columns

    def test_schema_version_is_set(self, tmp_path: Path) -> None:
        assert _SCHEMA_VERSION == 1

    def test_initialize_schema_applies_pragma_settings(self, tmp_path: Path) -> None:
        import sqlite3

        db_path = tmp_path / "_builds.sqlite"
        initialize_schema(db_path)

        with sqlite3.connect(db_path) as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode")
            journal_mode = cursor.fetchone()[0]
            assert journal_mode == "wal"

            cursor.execute("PRAGMA busy_timeout")
            busy_timeout = cursor.fetchone()[0]
            assert busy_timeout == 5000


class TestBuildIndex:
    """BuildIndex CRUD 테스트."""

    def test_record_build_inserts_row(self, tmp_path: Path) -> None:
        db_path = tmp_path / "_builds.sqlite"
        index = BuildIndex(db_path)

        index.record_build(
            run_id="test-run-1",
            status="completed",
            started_at="2025-01-01T00:00:00Z",
            finished_at="2025-01-01T00:01:00Z",
            spec_digest="abc123",
        )

        build = index.get_build("test-run-1")
        assert build is not None
        assert build["run_id"] == "test-run-1"
        assert build["status"] == "completed"

    def test_record_build_replaces_existing(self, tmp_path: Path) -> None:
        db_path = tmp_path / "_builds.sqlite"
        index = BuildIndex(db_path)

        index.record_build(
            run_id="test-run-1",
            status="completed",
            started_at="2025-01-01T00:00:00Z",
            finished_at="2025-01-01T00:01:00Z",
        )

        index.record_build(
            run_id="test-run-1",
            status="failed",
            started_at="2025-01-01T00:00:00Z",
            finished_at="2025-01-01T00:01:00Z",
            error="test error",
        )

        build = index.get_build("test-run-1")
        assert build["status"] == "failed"
        assert build["error"] == "test error"

    def test_list_builds_returns_newest_first(self, tmp_path: Path) -> None:
        db_path = tmp_path / "_builds.sqlite"
        index = BuildIndex(db_path)

        index.record_build(
            run_id="run-1",
            status="completed",
            started_at="2025-01-01T00:00:00Z",
            finished_at="2025-01-01T00:01:00Z",
        )
        index.record_build(
            run_id="run-2",
            status="completed",
            started_at="2025-01-01T02:00:00Z",
            finished_at="2025-01-01T02:01:00Z",
        )
        index.record_build(
            run_id="run-3",
            status="completed",
            started_at="2025-01-01T01:00:00Z",
            finished_at="2025-01-01T01:01:00Z",
        )

        builds = index.list_builds(limit=10)
        assert len(builds) == 3
        assert builds[0]["run_id"] == "run-2"  # 최신
        assert builds[1]["run_id"] == "run-3"
        assert builds[2]["run_id"] == "run-1"  # 가장 오래된

    def test_list_builds_respects_limit(self, tmp_path: Path) -> None:
        db_path = tmp_path / "_builds.sqlite"
        index = BuildIndex(db_path)

        for i in range(10):
            index.record_build(
                run_id=f"run-{i}",
                status="completed",
                started_at="2025-01-01T00:00:00Z",
                finished_at=f"2025-01-01T00:0{i}:00Z",
            )

        builds = index.list_builds(limit=5)
        assert len(builds) == 5

    def test_list_builds_filters_by_status(self, tmp_path: Path) -> None:
        db_path = tmp_path / "_builds.sqlite"
        index = BuildIndex(db_path)

        index.record_build(
            run_id="run-1",
            status="completed",
            started_at="2025-01-01T00:00:00Z",
            finished_at="2025-01-01T00:01:00Z",
        )
        index.record_build(
            run_id="run-2",
            status="failed",
            started_at="2025-01-01T01:00:00Z",
            finished_at="2025-01-01T01:01:00Z",
            error="test error",
        )
        index.record_build(
            run_id="run-3",
            status="completed",
            started_at="2025-01-01T02:00:00Z",
            finished_at="2025-01-01T02:01:00Z",
        )

        completed_builds = index.list_builds(status="completed")
        assert len(completed_builds) == 2

        failed_builds = index.list_builds(status="failed")
        assert len(failed_builds) == 1
        assert failed_builds[0]["run_id"] == "run-2"

    def test_get_build_returns_none_for_unknown(self, tmp_path: Path) -> None:
        db_path = tmp_path / "_builds.sqlite"
        index = BuildIndex(db_path)

        build = index.get_build("unknown")
        assert build is None
