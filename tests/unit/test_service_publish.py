"""Build publish readiness/실행 HTTP API 검증 (#491).

새 Publisher를 만들지 않고 기존 publishers.PUBLISHER_REGISTRY(huggingface/
kaggle)를 재사용하는 service 계약을 검증한다. 실제 HuggingFace/Kaggle
네트워크 호출은 절대 하지 않는다 — 모든 publisher는 in-memory spy로 대체한다.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
import threading
import types
from pathlib import Path
from typing import cast

import pytest

import kpubdata_builder.service.app as app_module
from kpubdata_builder.errors import PublishError
from kpubdata_builder.publishers.base import BasePublisher, PublishResult
from kpubdata_builder.publishers.huggingface import HuggingFacePublisher
from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.ownership import _OWNERSHIP_ENV
from kpubdata_builder.spec import JsonValue

from ._openapi import response_schema, validate
from .conftest import requires_symlinks
from .test_service import _FakeClient
from .test_service_contract import _load_contract

LICENSED_SPEC_YAML = (
    """
dataset_id: dataset.publish
title: Publish Fixture
description: publish readiness fixture
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: jsonl
    output_path: out/data.jsonl
license: CC-BY-4.0
""".strip()
    + "\n"
)

UNLICENSED_SPEC_YAML = LICENSED_SPEC_YAML.replace("license: CC-BY-4.0\n", "")

# spec loader는 license가 문자열이기만 하면 통과시킨다(spec/loader.py) — 사람이
# 아무것도 선언하지 않은 것과 같은 whitespace-only 값이 여기로 들어올 수 있다
# (#491 지침 4). 새 SPDX allowlist를 만들지 않고 blank 판정만 확인한다.
BLANK_LICENSE_SPEC_YAML = LICENSED_SPEC_YAML.replace("license: CC-BY-4.0\n", 'license: "   "\n')

PII_ALLOW_SPEC_YAML = LICENSED_SPEC_YAML.replace(
    "license: CC-BY-4.0\n", "pii:\n  mode: allow\nlicense: CC-BY-4.0\n"
)

FAILING_SPEC_YAML = LICENSED_SPEC_YAML.replace("dataset: air_quality\n", "dataset: missing\n")


class _SpyPublisher(BasePublisher):
    """실제 네트워크를 호출하지 않는 fake publisher. 호출 인자를 기록한다."""

    def __init__(
        self,
        name: str,
        *,
        expects_directory: bool = False,
        error: Exception | None = None,
    ) -> None:
        self._name = name
        self._expects_directory = expects_directory
        self._error = error
        self.calls: list[tuple[tuple[Path, ...], dict[str, object]]] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def expects_directory(self) -> bool:
        return self._expects_directory

    def publish(
        self, artifact_paths: tuple[Path, ...], *, destination: str, **kwargs: object
    ) -> PublishResult:
        self.calls.append((artifact_paths, {"destination": destination, **kwargs}))
        if self._error is not None:
            raise self._error
        return PublishResult(
            publisher=self._name,
            reference=f"https://example.test/{self._name}/{destination}",
            artifact_count=len(artifact_paths),
        )


class _DeferredPublisher(_SpyPublisher):
    """첫 remote call을 Event로 멈춰 pending receipt 동시성을 결정적으로 검증한다."""

    def __init__(self) -> None:
        super().__init__("huggingface")
        self.entered = threading.Event()
        self.release = threading.Event()

    def publish(
        self, artifact_paths: tuple[Path, ...], *, destination: str, **kwargs: object
    ) -> PublishResult:
        self.calls.append((artifact_paths, {"destination": destination, **kwargs}))
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("test release event was not set")
        return PublishResult(
            publisher=self.name,
            reference=f"https://example.test/huggingface/{destination}",
            artifact_count=len(artifact_paths),
        )


def _service(tmp_path: Path) -> BuilderService:
    client = _FakeClient({"datago.air_quality": [{"id": "1", "v": 10}, {"id": "2", "v": 20}]})
    return BuilderService(output_root=tmp_path, client_factory=lambda: client)


def _build(service: BuilderService, run_id: str, spec_yaml: str) -> ServiceResponse:
    return dispatch(service, "POST", "/build", {"spec": spec_yaml, "run_id": run_id})


def _readiness(
    service: BuilderService, run_id: str, target: str = "huggingface"
) -> ServiceResponse:
    return dispatch(
        service, "GET", f"/builds/{run_id}/publish/readiness", None, query=f"target={target}"
    )


def _publish(
    service: BuilderService,
    run_id: str,
    *,
    target: str = "huggingface",
    destination: str = "kpubdata/air-quality",
    options: dict[str, object] | None = None,
) -> ServiceResponse:
    body: dict[str, JsonValue] = {"target": target, "destination": destination}
    if options is not None:
        body["options"] = cast(JsonValue, options)
    return dispatch(service, "POST", f"/builds/{run_id}/publish", body)


def _blocker_codes(resp: ServiceResponse) -> list[str]:
    blockers = cast(list[dict[str, object]], resp.body["blockers"])
    return [cast(str, b["code"]) for b in blockers]


@pytest.fixture(autouse=True)
def _no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """credential_unavailable을 기본으로 재현 가능하게, 명시적으로 unset한다."""
    monkeypatch.delenv("HF_TOKEN", raising=False)
    monkeypatch.delenv("KAGGLE_USERNAME", raising=False)
    monkeypatch.delenv("KAGGLE_KEY", raising=False)
    monkeypatch.delenv("KAGGLE_CONFIG_DIR", raising=False)


def _with_credentials(monkeypatch: pytest.MonkeyPatch, target: str) -> None:
    assert target == "huggingface"
    monkeypatch.setenv("HF_TOKEN", "hf_super_secret_token_value")


class TestReadiness:
    def test_ready_when_succeeded_gold_license_and_credential(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_credentials(monkeypatch, "huggingface")
        service = _service(tmp_path)
        built = _build(service, "run-ready", LICENSED_SPEC_YAML)
        assert built.status_code == 200

        resp = _readiness(service, "run-ready", "huggingface")
        assert resp.status_code == 200
        assert resp.body["ready"] is True
        assert resp.body["blockers"] == []
        assert resp.body["target"] == "huggingface"
        assert resp.body["run_id"] == "run-ready"

    def test_running_job_is_blocker_not_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_credentials(monkeypatch, "huggingface")
        service = _service(tmp_path)
        snapshot = service._async_builds.registry.create(run_id="run-running", created_by="dev")
        assert service._async_builds.registry.begin_run(snapshot.run_id)

        resp = _readiness(service, "run-running")
        assert resp.status_code == 200
        assert resp.body["ready"] is False
        assert _blocker_codes(resp) == ["run_not_terminal"]

    def test_queued_job_is_blocker(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _with_credentials(monkeypatch, "huggingface")
        service = _service(tmp_path)
        service._async_builds.registry.create(run_id="run-queued", created_by="dev")

        resp = _readiness(service, "run-queued")
        assert resp.status_code == 200
        assert resp.body["ready"] is False
        assert _blocker_codes(resp) == ["run_not_terminal"]

    def test_failed_run_is_blocker(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        built = _build(service, "run-failed", FAILING_SPEC_YAML)
        assert built.status_code == 502

        resp = _readiness(service, "run-failed")
        assert resp.status_code == 200
        assert resp.body["ready"] is False
        assert "run_failed" in _blocker_codes(resp)

    def test_gold_deleted_after_success_is_blocker(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _build(service, "run-gold-gone", LICENSED_SPEC_YAML)
        import shutil

        shutil.rmtree(tmp_path / "run-gold-gone" / "gold")

        resp = _readiness(service, "run-gold-gone")
        assert resp.body["ready"] is False
        assert "gold_unavailable" in _blocker_codes(resp)

    def test_missing_artifact_file_is_blocker(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _build(service, "run-artifact-gone", LICENSED_SPEC_YAML)
        table_path = next((tmp_path / "run-artifact-gone" / "gold").rglob("table.parquet"))
        table_path.unlink()

        resp = _readiness(service, "run-artifact-gone")
        assert resp.body["ready"] is False
        assert "artifact_missing" in _blocker_codes(resp)

    def test_missing_license_is_blocker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_credentials(monkeypatch, "huggingface")
        service = _service(tmp_path)
        _build(service, "run-no-license", UNLICENSED_SPEC_YAML)

        resp = _readiness(service, "run-no-license")
        assert resp.body["ready"] is False
        assert "license_missing" in _blocker_codes(resp)

    def test_blank_license_is_not_recognized_as_declared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#491 지침 4: spec loader는 license가 문자열이기만 하면 통과시키므로
        whitespace-only 값이 BuildSpec.license에 들어올 수 있다 — #443 정책은
        바꾸지 않되, 그런 값을 "선언됨"으로 인정하지 않는지만 확인한다."""
        _with_credentials(monkeypatch, "huggingface")
        service = _service(tmp_path)
        _build(service, "run-blank-license", BLANK_LICENSE_SPEC_YAML)

        resp = _readiness(service, "run-blank-license")
        assert resp.body["ready"] is False
        assert "license_missing" in _blocker_codes(resp)

    def test_missing_credential_is_blocker(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _build(service, "run-no-cred", LICENSED_SPEC_YAML)

        resp = _readiness(service, "run-no-cred", "huggingface")
        assert resp.body["ready"] is False
        assert "credential_unavailable" in _blocker_codes(resp)

    def test_kaggle_target_is_not_available_over_http(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _build(service, "run-kaggle-http-disabled", LICENSED_SPEC_YAML)

        resp = _readiness(service, "run-kaggle-http-disabled", "kaggle")
        assert resp.status_code == 400
        assert resp.body["code"] == "unsupported_target"

    def test_effective_publish_policy_blocks_pii_allow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_credentials(monkeypatch, "huggingface")
        service = _service(tmp_path)
        built = _build(service, "run-pii-allow", PII_ALLOW_SPEC_YAML)
        assert built.status_code == 200

        resp = _readiness(service, "run-pii-allow")
        assert resp.body["ready"] is False
        assert "pii_allow_with_publish" in _blocker_codes(resp)

    def test_local_target_rejected_before_readiness_body(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _build(service, "run-local", LICENSED_SPEC_YAML)

        resp = _readiness(service, "run-local", "local")
        assert resp.status_code == 400
        assert "local" in cast(str, resp.body["error"])

    def test_unknown_target_returns_400(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _build(service, "run-unknown-target", LICENSED_SPEC_YAML)

        resp = _readiness(service, "run-unknown-target", "s3")
        assert resp.status_code == 400

    def test_missing_target_query_param_returns_400(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _build(service, "run-no-target", LICENSED_SPEC_YAML)

        resp = dispatch(service, "GET", "/builds/run-no-target/publish/readiness", None)
        assert resp.status_code == 400

    def test_invalid_run_id_returns_400(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        resp = dispatch(
            service,
            "GET",
            "/builds/../etc/publish/readiness",
            None,
            query="target=huggingface",
        )
        assert resp.status_code == 400

    def test_run_not_found_returns_404(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        resp = _readiness(service, "does-not-exist")
        assert resp.status_code == 404

    def test_cross_owner_returns_403(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        owner = Principal(kind="oidc", identifier="a", owner_id="oidc:owner-a")
        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: owner)
        service = _service(tmp_path)
        _build(service, "run-owned", LICENSED_SPEC_YAML)

        other = Principal(kind="oidc", identifier="b", owner_id="oidc:owner-b")
        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: other)
        resp = _readiness(service, "run-owned")
        assert resp.status_code == 403

    def test_readiness_never_calls_publisher(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_credentials(monkeypatch, "huggingface")
        spy = _SpyPublisher("huggingface")
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", spy)
        service = _service(tmp_path)
        _build(service, "run-side-effect-free", LICENSED_SPEC_YAML)

        resp = _readiness(service, "run-side-effect-free", "huggingface")
        assert resp.body["ready"] is True
        assert spy.calls == []


class TestPublish:
    def test_successful_publish_returns_structured_result(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_credentials(monkeypatch, "huggingface")
        spy = _SpyPublisher("huggingface")
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", spy)
        service = _service(tmp_path)
        _build(service, "run-publish-ok", LICENSED_SPEC_YAML)

        resp = _publish(service, "run-publish-ok", destination="kpubdata/air-quality")
        assert resp.status_code == 200
        assert resp.body["target"] == "huggingface"
        assert resp.body["publisher"] == "huggingface"
        assert resp.body["destination"] == "kpubdata/air-quality"
        assert resp.body["artifact_count"] and resp.body["artifact_count"] > 0
        assert resp.body["status"] == "ok"
        assert len(spy.calls) == 1
        artifact_paths, kwargs = spy.calls[0]
        assert kwargs["destination"] == "kpubdata/air-quality"
        assert kwargs["private"] is True
        assert all(p.is_file() for p in artifact_paths)

    def test_post_reverifies_independently_of_prior_get(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET을 아예 호출하지 않아도 POST 자체가 모든 검사를 한다."""
        spy = _SpyPublisher("huggingface")
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", spy)
        service = _service(tmp_path)
        _build(service, "run-no-license-post", UNLICENSED_SPEC_YAML)
        monkeypatch.setenv("HF_TOKEN", "token")

        resp = _publish(service, "run-no-license-post")
        assert resp.status_code == 409
        assert "license_missing" in _blocker_codes(resp)
        assert spy.calls == []

    def test_blocked_publish_never_calls_publisher(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spy = _SpyPublisher("huggingface")
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", spy)
        service = _service(tmp_path)
        _build(service, "run-blocked", LICENSED_SPEC_YAML)
        # HF_TOKEN 미설정 → credential_unavailable blocker.

        resp = _publish(service, "run-blocked")
        assert resp.status_code == 409
        assert spy.calls == []

    def test_running_run_cannot_be_published(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_credentials(monkeypatch, "huggingface")
        service = _service(tmp_path)
        snapshot = service._async_builds.registry.create(
            run_id="run-running-post", created_by="dev"
        )
        assert service._async_builds.registry.begin_run(snapshot.run_id)

        resp = _publish(service, "run-running-post")
        assert resp.status_code == 409
        assert "run_not_terminal" in _blocker_codes(resp)

    def test_failed_run_cannot_be_published(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _build(service, "run-failed-post", FAILING_SPEC_YAML)

        resp = _publish(service, "run-failed-post")
        assert resp.status_code == 409
        assert "run_failed" in _blocker_codes(resp)

    def test_cross_owner_cannot_publish(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        owner = Principal(kind="oidc", identifier="a", owner_id="oidc:owner-a")
        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: owner)
        service = _service(tmp_path)
        _build(service, "run-owned-post", LICENSED_SPEC_YAML)

        other = Principal(kind="oidc", identifier="b", owner_id="oidc:owner-b")
        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: other)
        resp = _publish(service, "run-owned-post")
        assert resp.status_code == 403

    def test_invalid_target_returns_400(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _build(service, "run-bad-target", LICENSED_SPEC_YAML)
        resp = _publish(service, "run-bad-target", target="s3")
        assert resp.status_code == 400

    def test_local_target_returns_400(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _build(service, "run-local-post", LICENSED_SPEC_YAML)
        resp = _publish(service, "run-local-post", target="local", destination="/tmp/x")
        assert resp.status_code == 400

    @pytest.mark.parametrize(
        "destination",
        [
            "",
            "   ",
            "https://huggingface.co/owner/name",
            "/etc/passwd",
            "../outside/name",
            "owner/../name",
            "C:\\Windows\\System32",
            "owner",
            "owner/name/extra",
            "owner/name\x00",
        ],
    )
    def test_unsafe_destination_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, destination: str
    ) -> None:
        _with_credentials(monkeypatch, "huggingface")
        spy = _SpyPublisher("huggingface")
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", spy)
        service = _service(tmp_path)
        _build(service, "run-unsafe-dest", LICENSED_SPEC_YAML)

        resp = _publish(service, "run-unsafe-dest", destination=destination)
        assert resp.status_code == 400
        assert spy.calls == []

    def test_unknown_option_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _with_credentials(monkeypatch, "huggingface")
        spy = _SpyPublisher("huggingface")
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", spy)
        service = _service(tmp_path)
        _build(service, "run-unknown-option", LICENSED_SPEC_YAML)

        resp = _publish(service, "run-unknown-option", options={"visibility": "private"})
        assert resp.status_code == 400
        assert spy.calls == []

    @pytest.mark.parametrize("private", [True, False])
    def test_private_option_is_passed_to_huggingface(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        private: bool,
    ) -> None:
        _with_credentials(monkeypatch, "huggingface")
        spy = _SpyPublisher("huggingface")
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", spy)
        service = _service(tmp_path)
        _build(service, f"run-private-{private}", LICENSED_SPEC_YAML)

        resp = _publish(service, f"run-private-{private}", options={"private": private})
        assert resp.status_code == 200
        assert spy.calls[0][1]["private"] is private

    def test_wrong_option_type_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_credentials(monkeypatch, "huggingface")
        spy = _SpyPublisher("huggingface")
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", spy)
        service = _service(tmp_path)
        _build(service, "run-bad-option-type", LICENSED_SPEC_YAML)

        resp = _publish(
            service,
            "run-bad-option-type",
            options={"private": "yes"},
        )
        assert resp.status_code == 400
        assert spy.calls == []

    def test_options_not_object_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_credentials(monkeypatch, "huggingface")
        service = _service(tmp_path)
        _build(service, "run-bad-options-type", LICENSED_SPEC_YAML)

        resp = dispatch(
            service,
            "POST",
            "/builds/run-bad-options-type/publish",
            {"target": "huggingface", "destination": "kpubdata/air-quality", "options": "nope"},
        )
        assert resp.status_code == 400

    def test_unknown_top_level_request_field_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_credentials(monkeypatch, "huggingface")
        service = _service(tmp_path)
        _build(service, "run-unknown-field", LICENSED_SPEC_YAML)

        resp = dispatch(
            service,
            "POST",
            "/builds/run-unknown-field/publish",
            {
                "target": "huggingface",
                "destination": "kpubdata/air-quality",
                "unexpected": True,
            },
        )
        assert resp.status_code == 400

    def test_publish_error_maps_to_502(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_credentials(monkeypatch, "huggingface")
        spy = _SpyPublisher("huggingface", error=PublishError("remote rejected the upload"))
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", spy)
        service = _service(tmp_path)
        _build(service, "run-publish-error", LICENSED_SPEC_YAML)

        resp = _publish(service, "run-publish-error")
        assert resp.status_code == 502
        # #491 blocker 1: PublishError도 "안전한 known exception"으로 취급해
        # str(exc)를 그대로 돌려주지 않는다 — stable generic 메시지만 나간다.
        assert resp.body["error"] == "publish failed due to an unexpected error"
        assert resp.body["code"] == "publish_failed"
        assert "remote rejected the upload" not in json.dumps(resp.body)

    def test_runtime_error_maps_to_502(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_credentials(monkeypatch, "huggingface")
        spy = _SpyPublisher("huggingface", error=RuntimeError("HF_TOKEN is not set"))
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", spy)
        service = _service(tmp_path)
        _build(service, "run-publish-runtime-error", LICENSED_SPEC_YAML)

        resp = _publish(service, "run-publish-runtime-error")
        assert resp.status_code == 502
        assert resp.body["error"] == "publish failed due to an unexpected error"
        assert resp.body["code"] == "publish_failed"
        assert "HF_TOKEN is not set" not in json.dumps(resp.body)

    def test_huggingface_create_repo_failure_is_sanitized_and_not_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secret = "create_repo failed with token=hf_create_secret"
        create_calls = 0

        class FailingHfApi:
            def __init__(self, token: str | None = None) -> None:
                del token

            def create_repo(self, **_kwargs: object) -> None:
                nonlocal create_calls
                create_calls += 1
                raise RuntimeError(secret)

        fake_module = types.ModuleType("huggingface_hub")
        fake_module.HfApi = FailingHfApi  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "huggingface_hub", fake_module)
        _with_credentials(monkeypatch, "huggingface")
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", HuggingFacePublisher())
        service = _service(tmp_path)
        _build(service, "run-create-repo-failure", LICENSED_SPEC_YAML)

        first = _publish(service, "run-create-repo-failure")
        second = _publish(service, "run-create-repo-failure")
        assert first.status_code == 502
        assert secret not in json.dumps(first.body)
        assert second.status_code == 409
        assert second.body["code"] == "publish_state_unknown"
        assert create_calls == 1

    def test_unexpected_exception_does_not_leak_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secret_bearing_message = "connection failed for token=hf_leak_me_1234567890"
        spy = _SpyPublisher("huggingface", error=ValueError(secret_bearing_message))
        _with_credentials(monkeypatch, "huggingface")
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", spy)
        service = _service(tmp_path)
        _build(service, "run-publish-unexpected", LICENSED_SPEC_YAML)

        resp = _publish(service, "run-publish-unexpected")
        assert resp.status_code == 502
        assert "hf_leak_me_1234567890" not in json.dumps(resp.body)
        assert secret_bearing_message not in json.dumps(resp.body)

    @pytest.mark.parametrize(
        "secret_message",
        [
            "leak: super-secret-value",
            "artifact write failed at /tmp/private/artifact",
            "artifact write failed at C:\\Users\\private\\artifact",
        ],
    )
    def test_exception_message_never_leaks_to_response_or_log(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        secret_message: str,
    ) -> None:
        """#491 blocker 1 필수 테스트: fake publisher가 secret/절대경로가 섞인
        예외를 던져도 HTTP response와 서버 log 어디에도 원문이 남지 않는다."""
        _with_credentials(monkeypatch, "huggingface")
        spy = _SpyPublisher("huggingface", error=PublishError(secret_message))
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", spy)
        service = _service(tmp_path)
        _build(service, "run-publish-secret-leak", LICENSED_SPEC_YAML)

        caplog.set_level(logging.INFO)
        resp = _publish(service, "run-publish-secret-leak")

        assert resp.status_code == 502
        assert resp.body == {
            "error": "publish failed due to an unexpected error",
            "code": "publish_failed",
        }
        body_text = json.dumps(resp.body)
        assert secret_message not in body_text
        assert "super-secret-value" not in body_text
        assert "/tmp/private/artifact" not in body_text
        assert "C:\\Users\\private\\artifact" not in body_text

        log_text = caplog.text
        assert secret_message not in log_text
        assert "super-secret-value" not in log_text
        assert "/tmp/private/artifact" not in log_text
        assert "C:\\Users\\private\\artifact" not in log_text

    def test_exact_retry_replays_persisted_success_without_remote_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_credentials(monkeypatch, "huggingface")
        spy = _SpyPublisher("huggingface")
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", spy)
        service = _service(tmp_path)
        _build(service, "run-retry", LICENSED_SPEC_YAML)

        first = _publish(service, "run-retry")
        # 성공 operation의 exact retry는 현재 credential/artifact 상태를 다시
        # 요구하지 않고 durable 성공 응답을 재생한다.
        monkeypatch.delenv("HF_TOKEN")
        next((tmp_path / "run-retry" / "gold").rglob("table.parquet")).unlink()
        second = _publish(service, "run-retry")
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.body == first.body
        assert len(spy.calls) == 1

    def test_success_receipt_replays_after_service_restart(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_credentials(monkeypatch, "huggingface")
        spy = _SpyPublisher("huggingface")
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", spy)
        first_service = _service(tmp_path)
        _build(first_service, "run-restart-replay", LICENSED_SPEC_YAML)
        first = _publish(first_service, "run-restart-replay")

        restarted = _service(tmp_path)
        replay = _publish(restarted, "run-restart-replay")
        assert replay.status_code == 200
        assert replay.body == first.body
        assert len(spy.calls) == 1

    def test_concurrent_exact_duplicate_calls_publisher_at_most_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_credentials(monkeypatch, "huggingface")
        publisher = _DeferredPublisher()
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", publisher)
        service = _service(tmp_path)
        _build(service, "run-concurrent", LICENSED_SPEC_YAML)
        first_responses: list[ServiceResponse] = []

        worker = threading.Thread(
            target=lambda: first_responses.append(_publish(service, "run-concurrent"))
        )
        worker.start()
        assert publisher.entered.wait(timeout=5)

        duplicate = _publish(service, "run-concurrent")
        assert duplicate.status_code == 409
        assert duplicate.body["code"] == "publish_in_progress"
        assert len(publisher.calls) == 1

        publisher.release.set()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert first_responses[0].status_code == 200
        assert len(publisher.calls) == 1

    def test_changed_options_conflict_without_remote_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_credentials(monkeypatch, "huggingface")
        spy = _SpyPublisher("huggingface")
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", spy)
        service = _service(tmp_path)
        _build(service, "run-options-conflict", LICENSED_SPEC_YAML)

        first = _publish(service, "run-options-conflict", options={"private": True})
        second = _publish(service, "run-options-conflict", options={"private": False})
        assert first.status_code == 200
        assert second.status_code == 409
        assert second.body["code"] == "publish_conflict"
        assert len(spy.calls) == 1

    def test_publisher_exception_becomes_unknown_and_blocks_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_credentials(monkeypatch, "huggingface")
        spy = _SpyPublisher("huggingface", error=RuntimeError("remote outcome unknown"))
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", spy)
        service = _service(tmp_path)
        _build(service, "run-unknown-outcome", LICENSED_SPEC_YAML)

        first = _publish(service, "run-unknown-outcome")
        second = _publish(service, "run-unknown-outcome")
        assert first.status_code == 502
        assert second.status_code == 409
        assert second.body["code"] == "publish_state_unknown"
        assert len(spy.calls) == 1

        with sqlite3.connect(tmp_path / "_publish_receipts.sqlite") as connection:
            state = connection.execute(
                "SELECT state FROM publish_receipts WHERE run_id = ?",
                ("run-unknown-outcome",),
            ).fetchone()
        assert state == ("unknown",)

    def test_kaggle_publish_is_rejected_before_publisher_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spy = _SpyPublisher("kaggle", expects_directory=True)
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "kaggle", spy)
        service = _service(tmp_path)
        _build(service, "run-kaggle-disabled-post", LICENSED_SPEC_YAML)

        resp = _publish(service, "run-kaggle-disabled-post", target="kaggle")
        assert resp.status_code == 400
        assert resp.body["code"] == "unsupported_target"
        assert spy.calls == []

    def test_effective_pii_policy_blocks_post_before_publisher_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_credentials(monkeypatch, "huggingface")
        spy = _SpyPublisher("huggingface")
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", spy)
        service = _service(tmp_path)
        _build(service, "run-pii-post", PII_ALLOW_SPEC_YAML)

        resp = _publish(service, "run-pii-post")
        assert resp.status_code == 409
        assert "pii_allow_with_publish" in _blocker_codes(resp)
        assert spy.calls == []


class TestSecurity:
    def test_credential_secret_never_in_readiness_body(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token = "hf_super_secret_token_value"
        monkeypatch.setenv("HF_TOKEN", token)
        service = _service(tmp_path)
        _build(service, "run-secret-readiness", LICENSED_SPEC_YAML)

        resp = _readiness(service, "run-secret-readiness")
        assert token not in json.dumps(resp.body)

    def test_credential_secret_never_in_publish_body(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token = "hf_super_secret_token_value"
        monkeypatch.setenv("HF_TOKEN", token)
        spy = _SpyPublisher("huggingface")
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", spy)
        service = _service(tmp_path)
        _build(service, "run-secret-publish", LICENSED_SPEC_YAML)

        resp = _publish(service, "run-secret-publish")
        assert token not in json.dumps(resp.body)

    def test_no_raw_absolute_path_in_publish_response(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_credentials(monkeypatch, "huggingface")
        spy = _SpyPublisher("huggingface")
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", spy)
        service = _service(tmp_path)
        _build(service, "run-no-path-leak", LICENSED_SPEC_YAML)

        resp = _publish(service, "run-no-path-leak")
        assert str(tmp_path) not in json.dumps(resp.body)

    def test_no_raw_absolute_path_in_readiness_response(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_credentials(monkeypatch, "huggingface")
        service = _service(tmp_path)
        _build(service, "run-no-path-leak-ready", LICENSED_SPEC_YAML)

        resp = _readiness(service, "run-no-path-leak-ready")
        assert str(tmp_path) not in json.dumps(resp.body)

    def test_receipt_contains_no_secret_or_artifact_path_and_is_not_public_artifact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        token = "hf_receipt_secret_value"
        monkeypatch.setenv("HF_TOKEN", token)
        spy = _SpyPublisher("huggingface")
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", spy)
        service = _service(tmp_path)
        _build(service, "run-receipt-security", LICENSED_SPEC_YAML)

        response = _publish(service, "run-receipt-security")
        assert response.status_code == 200
        with sqlite3.connect(tmp_path / "_publish_receipts.sqlite") as connection:
            row = connection.execute(
                "SELECT target, destination, options_json, state, result_json "
                "FROM publish_receipts WHERE run_id = ?",
                ("run-receipt-security",),
            ).fetchone()
        assert row is not None
        receipt_text = json.dumps(row)
        assert token not in receipt_text
        assert str(tmp_path) not in receipt_text
        assert row[3] == "succeeded"

        artifacts = service.artifacts("run-receipt-security")
        assert artifacts.status_code == 200
        assert "_publish_receipts.sqlite" not in cast(list[str], artifacts.body["files"])


class TestArtifactScope:
    def test_only_gold_files_are_passed_to_publisher(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_credentials(monkeypatch, "huggingface")
        spy = _SpyPublisher("huggingface")
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", spy)
        service = _service(tmp_path)
        _build(service, "run-artifact-scope", LICENSED_SPEC_YAML)

        resp = _publish(service, "run-artifact-scope")
        assert resp.status_code == 200
        artifact_paths, _ = spy.calls[0]
        run_dir = tmp_path / "run-artifact-scope"
        for path in artifact_paths:
            assert path.is_relative_to(run_dir / "gold"), path
        # BuildSpec snapshot과 manifest는 절대 섞이지 않는다.
        assert not any(p.name == "manifest.json" for p in artifact_paths)
        assert not any("buildspec" in p.name.lower() for p in artifact_paths)

    def test_untracked_file_in_gold_dir_is_not_published(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """manifest.outputs에 없는 파일(파이프라인이 쓰지 않은 파일)은
        gold 디렉터리 안에 있어도 절대 publish되지 않는다 — glob하지 않는다."""
        _with_credentials(monkeypatch, "huggingface")
        spy = _SpyPublisher("huggingface")
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", spy)
        service = _service(tmp_path)
        _build(service, "run-decoy", LICENSED_SPEC_YAML)

        gold_dirs = list((tmp_path / "run-decoy" / "gold").iterdir())
        decoy = gold_dirs[0] / "decoy-not-in-manifest.txt"
        decoy.write_text("should never be published", encoding="utf-8")

        resp = _publish(service, "run-decoy")
        assert resp.status_code == 200
        artifact_paths, _ = spy.calls[0]
        assert decoy not in artifact_paths

    def test_artifact_path_escape_blocks_publish_even_with_valid_artifacts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#491 blocker 2 필수 테스트: manifest.outputs에 유효한 gold artifact와
        gold_dir 밖(``..``) artifact가 함께 있으면, 무효 artifact만 조용히
        건너뛰고 나머지를 publish하는 것이 아니라 전체를 fail-closed로 막는다
        — readiness가 false, artifact blocker가 있고, POST는 409, Publisher는
        0회 호출된다."""
        _with_credentials(monkeypatch, "huggingface")
        spy = _SpyPublisher("huggingface")
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", spy)
        service = _service(tmp_path)
        _build(service, "run-escape", LICENSED_SPEC_YAML)

        outside = tmp_path / "outside-workspace-secret.txt"
        outside.write_text("must not be published", encoding="utf-8")
        manifest_path = tmp_path / "run-escape" / "manifest.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert data["outputs"], "fixture must already have at least one valid gold output"
        data["outputs"].append(str(outside))
        manifest_path.write_text(json.dumps(data), encoding="utf-8")

        readiness = _readiness(service, "run-escape")
        assert readiness.body["ready"] is False
        assert "artifact_invalid" in _blocker_codes(readiness)

        resp = _publish(service, "run-escape")
        assert resp.status_code == 409
        assert "artifact_invalid" in _blocker_codes(resp)
        assert spy.calls == []

    @requires_symlinks
    def test_symlink_escape_artifact_blocks_publish(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gold_dir 안의 symlink가 gold root 밖을 가리키면(#46/#47 기존
        path-safety 정책이 이미 다루는 범위, ``ensure_within``이 resolve 후
        검사) 같은 fail-closed 경로를 탄다."""
        _with_credentials(monkeypatch, "huggingface")
        spy = _SpyPublisher("huggingface")
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", spy)
        service = _service(tmp_path)
        _build(service, "run-symlink-escape", LICENSED_SPEC_YAML)

        outside_dir = tmp_path / "outside-symlink-target"
        outside_dir.mkdir()
        secret_file = outside_dir / "secret.parquet"
        secret_file.write_text("must not be published", encoding="utf-8")

        gold_dirs = list((tmp_path / "run-symlink-escape" / "gold").iterdir())
        link = gold_dirs[0] / "linked-escape"
        try:
            link.symlink_to(outside_dir, target_is_directory=True)
        except OSError:
            pytest.skip("symlink creation is not permitted in this environment")

        manifest_path = tmp_path / "run-symlink-escape" / "manifest.json"
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["outputs"].append(str(link / "secret.parquet"))
        manifest_path.write_text(json.dumps(data), encoding="utf-8")

        resp = _publish(service, "run-symlink-escape")
        assert resp.status_code == 409
        assert "artifact_invalid" in _blocker_codes(resp)
        assert spy.calls == []


def _assert_conforms(resp: ServiceResponse, path: str, method: str) -> None:
    contract = _load_contract()
    schema = response_schema(contract, path, method, resp.status_code)
    assert schema is not None, (
        f"{method} {path}: status {resp.status_code} is not declared in the contract"
    )
    errors = validate(resp.body, schema, contract)
    assert not errors, (
        f"{method} {path} {resp.status_code} response drifts from contract:\n  "
        + "\n  ".join(errors)
    )


class TestReceiptReconcile:
    """unknown receipt의 조회/reconcile/reset 운영 경로 (#551)."""

    def _unknown_receipt_service(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_id: str
    ) -> BuilderService:
        """publish가 실패해 receipt가 unknown인 service를 만든다."""
        _with_credentials(monkeypatch, "huggingface")
        failing = _SpyPublisher("huggingface", error=RuntimeError("remote blew up"))
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", failing)
        service = _service(tmp_path)
        _build(service, run_id, LICENSED_SPEC_YAML)
        resp = _publish(service, run_id)
        assert resp.status_code == 502
        return service

    def test_get_receipt_reports_unknown_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = self._unknown_receipt_service(tmp_path, monkeypatch, "run-unknown")
        resp = dispatch(
            service,
            "GET",
            "/builds/run-unknown/publish/receipt",
            None,
            query="target=huggingface&destination=kpubdata%2Fair-quality",
        )
        assert resp.status_code == 200
        assert resp.body["state"] == "unknown"
        assert resp.body["reconcilable"] is True
        assert resp.body["target"] == "huggingface"

    def test_get_receipt_missing_returns_404(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        resp = dispatch(
            service,
            "GET",
            "/builds/run-none/publish/receipt",
            None,
            query="target=huggingface&destination=kpubdata%2Fair-quality",
        )
        assert resp.status_code == 404

    def test_reconcile_remote_exists_confirms_succeeded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = self._unknown_receipt_service(tmp_path, monkeypatch, "run-exists")
        monkeypatch.setattr(service, "_probe_remote_publish_target", lambda *_args: True)

        resp = dispatch(
            service,
            "POST",
            "/builds/run-exists/publish/reconcile",
            {"target": "huggingface", "destination": "kpubdata/air-quality"},
        )
        assert resp.status_code == 200
        assert resp.body["state"] == "succeeded"
        assert resp.body["reconciled"] is True

        # 이후 동일 publish 요청은 receipt 결과를 replay한다(중복 게시 없음).
        ok_publisher = _SpyPublisher("huggingface")
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", ok_publisher)
        replay = _publish(service, "run-exists")
        assert replay.status_code == 200
        assert ok_publisher.calls == []

    def test_reconcile_remote_absent_resets_and_allows_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = self._unknown_receipt_service(tmp_path, monkeypatch, "run-absent")
        monkeypatch.setattr(service, "_probe_remote_publish_target", lambda *_args: False)

        resp = dispatch(
            service,
            "POST",
            "/builds/run-absent/publish/reconcile",
            {"target": "huggingface", "destination": "kpubdata/air-quality"},
        )
        assert resp.status_code == 200
        assert resp.body["state"] == "reset"
        assert resp.body["retry_allowed"] is True

        # reset 이후 같은 operation을 다시 게시할 수 있다(새 claim).
        ok_publisher = _SpyPublisher("huggingface")
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", ok_publisher)
        retry = _publish(service, "run-absent")
        assert retry.status_code == 200
        assert len(ok_publisher.calls) == 1

    def test_reconcile_probe_unavailable_changes_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = self._unknown_receipt_service(tmp_path, monkeypatch, "run-probe-down")
        monkeypatch.setattr(service, "_probe_remote_publish_target", lambda *_args: None)

        resp = dispatch(
            service,
            "POST",
            "/builds/run-probe-down/publish/reconcile",
            {"target": "huggingface", "destination": "kpubdata/air-quality"},
        )
        assert resp.status_code == 503
        assert resp.body["code"] == "reconcile_unavailable"

        blocked = _publish(service, "run-probe-down")
        assert blocked.status_code == 409
        assert blocked.body["code"] == "publish_state_unknown"

    def test_reconcile_succeeded_receipt_is_idempotent_noop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_credentials(monkeypatch, "huggingface")
        spy = _SpyPublisher("huggingface")
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", spy)
        service = _service(tmp_path)
        _build(service, "run-done", LICENSED_SPEC_YAML)
        assert _publish(service, "run-done").status_code == 200
        probes: list[tuple[str, str]] = []
        monkeypatch.setattr(
            service,
            "_probe_remote_publish_target",
            lambda target, destination: probes.append((target, destination)),
        )

        resp = dispatch(
            service,
            "POST",
            "/builds/run-done/publish/reconcile",
            {"target": "huggingface", "destination": "kpubdata/air-quality"},
        )
        assert resp.status_code == 200
        assert resp.body["state"] == "succeeded"
        assert resp.body["reconciled"] is False
        assert probes == []

    def test_delete_receipt_resets_with_audit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = self._unknown_receipt_service(tmp_path, monkeypatch, "run-manual")
        # reset 전 receipt에서 fingerprint와 owner를 읽어 감사 로그를 결정적으로 검증한다.
        receipts = service._publish_receipts
        import sqlite3

        with sqlite3.connect(receipts.path) as conn:
            fingerprint, owner_key = conn.execute(
                "SELECT fingerprint, owner_key FROM publish_receipts WHERE run_id = 'run-manual'"
            ).fetchone()

        resp = dispatch(
            service,
            "DELETE",
            "/builds/run-manual/publish/receipt",
            None,
            query="target=huggingface&destination=kpubdata%2Fair-quality",
        )
        assert resp.status_code == 200
        assert resp.body["state"] == "reset"

        gone = dispatch(
            service,
            "GET",
            "/builds/run-manual/publish/receipt",
            None,
            query="target=huggingface&destination=kpubdata%2Fair-quality",
        )
        assert gone.status_code == 404

        with sqlite3.connect(receipts.path) as conn:
            audit_rows = conn.execute(
                "SELECT fingerprint, action FROM publish_receipt_audit"
            ).fetchall()
        assert audit_rows == [(fingerprint, "manual_reset")]

    def test_cross_owner_receipt_is_404(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(_OWNERSHIP_ENV, "true")
        owner = Principal(kind="oidc", identifier="a", owner_id="oidc:owner-a")
        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: owner)
        _with_credentials(monkeypatch, "huggingface")
        failing = _SpyPublisher("huggingface", error=RuntimeError("boom"))
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", failing)
        service = _service(tmp_path)
        _build(service, "run-owned-reconcile", LICENSED_SPEC_YAML)
        assert _publish(service, "run-owned-reconcile").status_code == 502

        other = Principal(kind="oidc", identifier="b", owner_id="oidc:owner-b")
        monkeypatch.setattr(app_module, "authenticate", lambda **_kwargs: other)
        # publish/readiness 라우트와 동일하게 run 소유권 게이트가 403으로 차단한다.
        resp = dispatch(
            service,
            "GET",
            "/builds/run-owned-reconcile/publish/receipt",
            None,
            query="target=huggingface&destination=kpubdata%2Fair-quality",
        )
        assert resp.status_code == 403
        reset_resp = dispatch(
            service,
            "DELETE",
            "/builds/run-owned-reconcile/publish/receipt",
            None,
            query="target=huggingface&destination=kpubdata%2Fair-quality",
        )
        assert reset_resp.status_code == 403


class TestOpenApiConformance:
    """실제 dispatch() wire 응답이 OpenAPI 스키마와 정확히 일치하는지 (ADR-0005 방식)."""

    def test_readiness_200_ready_conforms(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _with_credentials(monkeypatch, "huggingface")
        service = _service(tmp_path)
        _build(service, "conform-ready", LICENSED_SPEC_YAML)
        resp = _readiness(service, "conform-ready")
        assert resp.status_code == 200
        _assert_conforms(resp, "/builds/{run_id}/publish/readiness", "GET")

    def test_readiness_200_blocked_conforms(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _build(service, "conform-blocked", LICENSED_SPEC_YAML)
        resp = _readiness(service, "conform-blocked")
        assert resp.status_code == 200
        assert resp.body["ready"] is False
        _assert_conforms(resp, "/builds/{run_id}/publish/readiness", "GET")

    def test_readiness_400_conforms(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _build(service, "conform-400", LICENSED_SPEC_YAML)
        resp = _readiness(service, "conform-400", "local")
        assert resp.status_code == 400
        _assert_conforms(resp, "/builds/{run_id}/publish/readiness", "GET")

    def test_readiness_404_conforms(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        resp = _readiness(service, "nope")
        assert resp.status_code == 404
        _assert_conforms(resp, "/builds/{run_id}/publish/readiness", "GET")

    def test_publish_200_conforms(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _with_credentials(monkeypatch, "huggingface")
        spy = _SpyPublisher("huggingface")
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", spy)
        service = _service(tmp_path)
        _build(service, "conform-publish-ok", LICENSED_SPEC_YAML)
        resp = _publish(service, "conform-publish-ok")
        assert resp.status_code == 200
        _assert_conforms(resp, "/builds/{run_id}/publish", "POST")

    def test_publish_400_conforms(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _build(service, "conform-publish-400", LICENSED_SPEC_YAML)
        resp = _publish(service, "conform-publish-400", destination="../escape")
        assert resp.status_code == 400
        _assert_conforms(resp, "/builds/{run_id}/publish", "POST")

    def test_publish_404_conforms(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        resp = _publish(service, "nope")
        assert resp.status_code == 404
        _assert_conforms(resp, "/builds/{run_id}/publish", "POST")

    def test_publish_409_conforms(self, tmp_path: Path) -> None:
        service = _service(tmp_path)
        _build(service, "conform-publish-409", LICENSED_SPEC_YAML)
        resp = _publish(service, "conform-publish-409")
        assert resp.status_code == 409
        _assert_conforms(resp, "/builds/{run_id}/publish", "POST")

    def test_publish_502_conforms(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _with_credentials(monkeypatch, "huggingface")
        spy = _SpyPublisher("huggingface", error=PublishError("boom"))
        monkeypatch.setitem(app_module.PUBLISHER_REGISTRY, "huggingface", spy)
        service = _service(tmp_path)
        _build(service, "conform-publish-502", LICENSED_SPEC_YAML)
        resp = _publish(service, "conform-publish-502")
        assert resp.status_code == 502
        _assert_conforms(resp, "/builds/{run_id}/publish", "POST")
