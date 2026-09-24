"""CUBRID 백엔드 계약/통합 테스트 (ADR 0016).

기본 스위트에서 제외된다(`-m 'not cubrid'`). 실행: ``pytest -m cubrid`` (sqlalchemy 필요).

engine fixture 는 ``KPUBDATA_BUILDER_CUBRID_URL`` 이 설정되면 **실 CUBRID** 에 붙고,
없으면 in-memory SQLite 엔진으로 SQLAlchemy Core 로직을 검증한다(dialect 독립적). 실 CUBRID
통합은 CUBRID 컨테이너를 띄운 전용 CI 잡(.github/workflows/cubrid.yml)에서 URL 을 주입해 돌린다.

이 폴백에는 함정이 하나 있다(#587): 전용 CI 잡에서 URL 주입이 빠지면 fixture 가 조용히
SQLite 로 내려앉고, **CUBRID dialect 를 한 줄도 건드리지 않은 채 초록으로 통과한다**. 그래서
``KPUBDATA_BUILDER_REQUIRE_REAL_CUBRID=1`` 을 주면 폴백을 금지한다 — CI 잡이 이 값을 켜므로,
URL 이 사라지면 조용히 통과하는 대신 잡이 실패한다.

테스트는 자기 소유 key(unique run_id/owner_id)만 단언해 공유 CUBRID 에서도 안전하다.
"""

from __future__ import annotations

import base64
import os

import pytest

pytest.importorskip("sqlalchemy")

from sqlalchemy import create_engine  # noqa: E402

from kpubdata_builder.credentials.crypto import AesGcmCredentialCipher  # noqa: E402
from kpubdata_builder.credentials.store_cubrid import CubridCredentialRepository  # noqa: E402
from kpubdata_builder.store.build_index import BuildEntry  # noqa: E402
from kpubdata_builder.store.build_index_cubrid import CubridBuildIndex  # noqa: E402

pytestmark = pytest.mark.cubrid


_REQUIRE_REAL_ENV = "KPUBDATA_BUILDER_REQUIRE_REAL_CUBRID"


def _require_real_cubrid() -> bool:
    """전용 CI 잡인지 여부. 참이면 SQLite 폴백을 금지한다 (#587)."""
    return os.environ.get(_REQUIRE_REAL_ENV, "").strip().lower() in ("1", "true", "yes")


@pytest.fixture
def engine():  # type: ignore[no-untyped-def]
    url = os.environ.get("KPUBDATA_BUILDER_CUBRID_URL")
    if not url and _require_real_cubrid():
        raise AssertionError(
            f"{_REQUIRE_REAL_ENV} is set but KPUBDATA_BUILDER_CUBRID_URL is empty — this run "
            "would silently fall back to in-memory SQLite and prove nothing about the CUBRID "
            "dialect. Set the URL or unset the guard."
        )
    if url:
        eng = create_engine(url, pool_pre_ping=True, future=True)
    else:
        eng = create_engine("sqlite:///:memory:", future=True)
    yield eng
    eng.dispose()


def test_ci_job_runs_against_a_real_cubrid_engine(engine) -> None:  # type: ignore[no-untyped-def]
    """전용 CI 잡에서 실제로 CUBRID dialect 위에서 돌고 있는지 확인한다 (#587).

    로컬(가드 미설정)에서는 SQLite 폴백이 정상이므로 skip 한다.
    """
    if not _require_real_cubrid():
        pytest.skip(f"{_REQUIRE_REAL_ENV} not set — SQLite fallback is expected locally")
    assert engine.dialect.name == "cubrid", (
        f"expected the cubrid dialect, got {engine.dialect.name!r} — the contract tests are "
        "not exercising CUBRID"
    )
    # 드라이버까지 확인한다: bare cubrid:// 는 legacy C-extension 으로 풀린다(ADR 0016).
    assert engine.dialect.driver == "pycubrid", (
        f"expected the pycubrid driver, got {engine.dialect.driver!r}"
    )


def test_build_index_crud_and_ordering(engine) -> None:  # type: ignore[no-untyped-def]
    idx = CubridBuildIndex(engine)
    idx.insert_or_replace(
        "cbx-1",
        "ok",
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:01:00Z",
        spec_digest="d1",
        created_by="dev:local",
        dataset_id="cbx-ds",
        owner_id="oidc:a",
    )
    got = idx.get("cbx-1")
    assert got is not None and got.status == "ok" and got.dataset_id == "cbx-ds"

    # 전체 행 교체(sqlite INSERT OR REPLACE 와 동일 시맨틱)
    idx.insert_or_replace("cbx-1", "failed", None, "2026-01-01T00:02:00Z", error="boom")
    got = idx.get("cbx-1")
    assert got is not None and got.status == "failed" and got.error == "boom"
    assert got.dataset_id is None  # 미전달 필드는 교체로 비워진다

    idx.insert_or_replace("cbx-2", "ok", None, "2026-01-02T00:00:00Z", dataset_id="cbx-ds")
    by_ds = {e.run_id for e in idx.list_by_dataset("cbx-ds")}
    assert by_ds == {"cbx-2"}  # cbx-1 은 교체로 dataset_id 소실

    idx.delete("cbx-1")
    idx.delete("cbx-2")
    assert idx.get("cbx-1") is None and idx.get("cbx-2") is None


def test_build_index_rebuild(engine) -> None:  # type: ignore[no-untyped-def]
    idx = CubridBuildIndex(engine)
    n = idx.rebuild(
        [
            BuildEntry("cbx-r1", "ok", "s", "f1", "dg", None, "dev:local", "cbx-ds2", "oidc:x"),
            BuildEntry("cbx-r2", "failed", "s", "f2", None, "err", "dev:local", None, None),
        ]
    )
    assert n == 2
    got = idx.get("cbx-r1")
    assert got is not None and got.dataset_id == "cbx-ds2"
    # rebuild 는 truncate 하므로 이전 run 은 사라진다(정본 manifest 에서 재구축).
    assert idx.get("cbx-1") is None


def test_build_index_monitoring_queries(engine) -> None:  # type: ignore[no-untyped-def]
    """upstream monitoring(#516/#527) 메서드가 실 CUBRID 에서 동작하는지 검증."""
    idx = CubridBuildIndex(engine)
    idx.rebuild(
        [
            BuildEntry(
                "cbm-1", "ok", "s", "2026-01-01T00:00:00Z", "d", None, "dev:local", "ds", "oidc:me"
            ),
            BuildEntry(
                "cbm-2",
                "failed",
                "s",
                "2026-01-02T00:00:00Z",
                None,
                "e",
                "dev:local",
                "ds",
                "oidc:other",
            ),
            BuildEntry("cbm-3", "ok", "s", "2026-01-03T00:00:00Z", "d", None, "svc", "ds", None),
        ]
    )
    # list_between: [start, end) 오름차순, end 이상은 제외
    between = idx.list_between("2026-01-01T00:00:00Z", "2026-01-03T00:00:00Z")
    assert [e.run_id for e in between] == ["cbm-1", "cbm-2"]
    # latest_successful_finished_at: 성공(ok) 중 최신
    assert idx.latest_successful_finished_at() == "2026-01-03T00:00:00Z"
    # list_recent_owned: owner_id 매치
    owned = idx.list_recent_owned(limit=10, principal_owner_id="oidc:me", principal_label="oidc:x")
    assert {e.run_id for e in owned} == {"cbm-1"}
    # principal owner_id 없으면 created_by 폴백
    owned2 = idx.list_recent_owned(limit=10, principal_owner_id=None, principal_label="svc")
    assert {e.run_id for e in owned2} == {"cbm-3"}


def test_build_index_write_failure_is_swallowed(engine) -> None:  # type: ignore[no-untyped-def]
    """ADR 0003 규칙4: 인덱스 쓰기 실패가 예외로 전파되면 안 된다."""
    idx = CubridBuildIndex(engine)

    class _BoomEngine:
        def begin(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("cubrid unavailable")

    idx._engine = _BoomEngine()  # type: ignore[assignment]
    # 예외 없이 조용히 삼켜져야 한다.
    idx.insert_or_replace("cbx-x", "ok", None, None)
    idx.delete("cbx-x")


def test_credential_roundtrip_and_aad_binding(engine) -> None:  # type: ignore[no-untyped-def]
    cipher = AesGcmCredentialCipher.from_base64(base64.b64encode(os.urandom(32)).decode())
    repo = CubridCredentialRepository(engine, cipher)
    owner = "oidc:cbx-owner"

    assert repo.get_metadata(owner, "datago").configured is False
    m = repo.put(owner, "DataGo", "secret-key")
    assert m.configured and m.masked == "********" and m.provider == "datago"
    assert repo.get_secret(owner, "datago") == "secret-key"
    # AAD 는 owner+provider 에 바인딩 — 다른 owner 는 복호 불가(행 없음 → None)
    assert repo.get_secret("oidc:cbx-other", "datago") is None
    # upsert(rotate)
    repo.put(owner, "datago", "rotated")
    assert repo.get_secret(owner, "datago") == "rotated"
    assert "datago" in repo.list_configured_providers(owner)
    assert repo.delete(owner, "datago") is True
    assert repo.delete(owner, "datago") is False


def test_credential_rejects_empty_owner_and_credential(engine) -> None:  # type: ignore[no-untyped-def]
    cipher = AesGcmCredentialCipher.from_base64(base64.b64encode(os.urandom(32)).decode())
    repo = CubridCredentialRepository(engine, cipher)
    with pytest.raises(ValueError):
        repo.put("", "datago", "x")
    with pytest.raises(ValueError):
        repo.put("oidc:cbx-owner", "datago", "   ")


def test_artifact_store_manifest_authoritative(tmp_path, engine) -> None:  # type: ignore[no-untyped-def]
    from kpubdata_builder.store.artifacts.cubrid import CubridArtifactStore
    from kpubdata_builder.store.artifacts.local import LocalArtifactStore

    store = CubridArtifactStore(tmp_path, engine)
    manifest = {"build_id": "cbx-run", "created_by": "dev:local", "errors": []}
    store.put_manifest("cbx-run", manifest)

    # CUBRID 정본 + FS 미러
    assert store.get_manifest("cbx-run") == manifest
    assert LocalArtifactStore(tmp_path).get_manifest("cbx-run") == manifest
    assert store.run_dir("cbx-run") == tmp_path / "cbx-run"

    # FS 미러 손상 시 CUBRID 정본이 이긴다
    (tmp_path / "cbx-run" / "manifest.json").write_text("{ broken", encoding="utf-8")
    assert store.get_manifest("cbx-run") == manifest

    # CUBRID 행 없이 FS 만 있는 run → FS 폴백
    LocalArtifactStore(tmp_path).put_manifest("cbx-fsonly", {"build_id": "cbx-fsonly"})
    assert store.get_manifest("cbx-fsonly") == {"build_id": "cbx-fsonly"}

    ids = set(store.list_run_ids())
    assert {"cbx-run", "cbx-fsonly"} <= ids


# 기동 게이트의 "정상 연결" 검증은 tests/cubrid/test_cubrid_fail_closed.py 로 옮겼다.
# validate_storage_config() 가 실 커넥션까지 열게 되면서(#587) 이 자리의 버전은
# (a) 그 파일의 테스트에 그대로 포섭되고, (b) 전역 Engine 을 폐기하지 않아 뒤따르는
# 테스트로 새어 나갔다. 폐기 fixture 를 갖춘 쪽으로 단일화한다.


def test_startup_validation_refuses_a_bare_cubrid_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """드라이버를 생략한 URL 은 legacy C-extension 으로 풀린다 — 기동 전에 막아야 한다.

    이 경로가 열려 있으면 기동은 조용히 성공하고 **첫 쿼리에서** ``ImportError:
    Could not import CUBRIDdb`` 로 죽는다(ADR 0016).
    """
    from kpubdata_builder.store.backend import cubrid_url

    monkeypatch.setenv("KPUBDATA_BUILDER_STORAGE_BACKEND", "cubrid")
    monkeypatch.setenv("KPUBDATA_BUILDER_CUBRID_URL", "cubrid://dba:@127.0.0.1:33000/kpubdata")
    # 드라이버 생략은 거부가 아니라 정규화 대상이다 — pycubrid 로 고쳐서 돌려준다.
    assert cubrid_url().startswith("cubrid+pycubrid://")


@pytest.mark.parametrize("driver", ["cubriddb", "cubrid", "aiopycubrid"])
def test_startup_validation_refuses_unsupported_drivers(
    driver: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[cubrid] extra 가 설치하지 않는 드라이버로는 기동을 허용하지 않는다."""
    from kpubdata_builder.store.backend import cubrid_url

    monkeypatch.setenv("KPUBDATA_BUILDER_STORAGE_BACKEND", "cubrid")
    monkeypatch.setenv(
        "KPUBDATA_BUILDER_CUBRID_URL", f"cubrid+{driver}://dba:@127.0.0.1:33000/kpubdata"
    )
    with pytest.raises(RuntimeError):
        cubrid_url()
