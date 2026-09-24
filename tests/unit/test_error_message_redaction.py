"""내부 예외 문자열이 HTTP 응답으로 새지 않는지 검증한다.

예외를 그대로 응답에 넣으면 호출자가 알 필요 없는 것들이 따라 나간다 —
OSError 는 서버의 절대 경로를, upstream 클라이언트의 예외는 요청 URL 을
싣는다. data.go.kr 계열은 API 키를 쿼리 파라미터로 보내므로 그 URL 에는
남의 자격증명이 들어 있다.

진단 정보를 없애자는 게 아니라 **어디로 보낼지**를 정하는 문제다 —
traceback 은 로그로, 응답에는 안정적인 문구만.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pytest

from kpubdata_builder.service import BuilderService, ServiceResponse
from kpubdata_builder.service.jobs import AsyncBuildExecutor
from kpubdata_builder.spec import BUILDSPEC_SNAPSHOT_FILENAME

_SECRET_PATH = "/srv/kpubdata/secrets/production.key"
_SECRET_URL = "https://apis.data.go.kr/service?serviceKey=SUPERSECRETKEY"


class _FakeClient:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def _service(tmp_path: Path) -> BuilderService:
    return BuilderService(output_root=tmp_path, client_factory=lambda **_kwargs: _FakeClient())


class TestSnapshotReadFailure:
    def test_an_unreadable_snapshot_does_not_return_the_server_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        run_dir = tmp_path / "run1"
        run_dir.mkdir()
        (run_dir / BUILDSPEC_SNAPSHOT_FILENAME).write_text("dataset_id: x\n", encoding="utf-8")

        def _boom(self: Path) -> bytes:
            raise OSError(f"[Errno 13] Permission denied: {_SECRET_PATH}")

        monkeypatch.setattr(Path, "read_bytes", _boom)

        with caplog.at_level(logging.ERROR):
            response = _service(tmp_path).spec("run1")

        assert response.status_code == 500
        assert response.body == {"error": "failed to read BuildSpec snapshot"}
        assert _SECRET_PATH not in str(response.body)
        # 사라지는 게 아니라 로그로 간다.
        assert _SECRET_PATH in caplog.text


class TestCatalogFailure:
    def test_an_upstream_failure_does_not_return_the_request_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import kpubdata_builder.service.app as app_module

        def _boom(_client: object) -> Any:
            raise RuntimeError(f"connection failed: {_SECRET_URL}")

        monkeypatch.setattr(app_module, "runtime_provider_catalog", _boom)

        with caplog.at_level(logging.ERROR):
            response = _service(tmp_path).catalog()

        assert response.status_code == 502
        assert response.body == {"error": "catalog unavailable"}
        assert "serviceKey" not in str(response.body)
        assert _SECRET_URL in caplog.text


class TestUnhandledJobFailure:
    def test_an_unexpected_exception_reports_its_type_not_its_message(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``GET /builds/{run_id}`` 가 이 error 를 그대로 싣는다.

        예상하지 못한 예외라 내용이 무엇일지 보장할 수 없으므로, 타입 이름까지만
        내보낸다 — 어느 계층에서 터졌는지는 알려주면서 임의의 내부 문자열을
        노출하지는 않는 선이다.
        """
        executor = AsyncBuildExecutor(max_workers=1)

        def _boom(*_args: object) -> ServiceResponse:
            raise ConnectionRefusedError(f"cannot reach {_SECRET_URL}")

        try:
            with caplog.at_level(logging.ERROR):
                executor.submit(
                    spec_yaml="dataset_id: x\n",
                    run_id="run-boom",
                    created_by="tester",
                    runner=_boom,
                )
                snapshot = _await_terminal(executor, "run-boom")
        finally:
            executor.shutdown()

        assert snapshot.status == "failed"
        assert snapshot.error == "internal error: ConnectionRefusedError"
        assert "serviceKey" not in (snapshot.error or "")
        assert _SECRET_URL in caplog.text


def _await_terminal(executor: AsyncBuildExecutor, run_id: str) -> Any:
    import time

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        snapshot = executor.get(run_id)
        if snapshot is not None and snapshot.status in ("succeeded", "failed", "cancelled"):
            return snapshot
        time.sleep(0.01)
    raise AssertionError(f"job {run_id} never reached a terminal state")
