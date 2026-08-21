"""Kaggle metadata override 및 Local publish validation 테스트 (#550).

Issue #550 구현을 위한 unit tests:
- Kaggle metadata placeholder override 기능
- Local publish-root validation
- Kaggle credential blocker
- Local destination validation
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from kpubdata_builder.service import publish as publish_service


def test_kaggle_metadata_override_placeholder(tmp_path):
    """Kaggle dataset-metadata.json의 placeholder ID가 destination으로 override되는지 테스트."""
    # placeholder metadata 파일 생성
    metadata_path = tmp_path / "dataset-metadata.json"
    placeholder_metadata = {
        "id": "kpubdata-builder/placeholder",
        "title": "Test Dataset",
        "licenses": [{"name": "CC-BY-4.0"}],
        "resources": []
    }
    metadata_path.write_text(json.dumps(placeholder_metadata, ensure_ascii=False, indent=2))
    
    # ResolvedArtifacts 생성
    artifacts = publish_service.ResolvedArtifacts(
        paths=(metadata_path,),
        expects_directory=False,
    )
    
    # override 실행
    publish_service.override_kaggle_metadata_id(artifacts, "test-user/my-dataset")
    
    # 결과 확인
    updated_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert updated_metadata["id"] == "test-user/my-dataset"
    assert updated_metadata["title"] == "Test Dataset"  # 다른 필드는 유지


def test_kaggle_metadata_override_idempotent(tmp_path):
    """Kaggle metadata override가 멱등한지 테스트."""
    metadata_path = tmp_path / "dataset-metadata.json"
    metadata_path.write_text(json.dumps({
        "id": "kpubdata-builder/placeholder",
        "title": "Test Dataset"
    }, ensure_ascii=False))
    
    artifacts = publish_service.ResolvedArtifacts(paths=(metadata_path,), expects_directory=False)
    
    # 첫 번째 override
    publish_service.override_kaggle_metadata_id(artifacts, "owner/dataset1")
    assert json.loads(metadata_path.read_text())["id"] == "owner/dataset1"
    
    # 두 번째 override (같은 destination)
    publish_service.override_kaggle_metadata_id(artifacts, "owner/dataset1")
    assert json.loads(metadata_path.read_text())["id"] == "owner/dataset1"


def test_kaggle_metadata_override_preserves_non_placeholder(tmp_path):
    """placeholder가 아닌 metadata는 보존되는지 테스트."""
    metadata_path = tmp_path / "dataset-metadata.json"
    original_id = "original-user/original-dataset"
    metadata_path.write_text(json.dumps({
        "id": original_id,
        "title": "Original Dataset"
    }, ensure_ascii=False))
    
    artifacts = publish_service.ResolvedArtifacts(paths=(metadata_path,), expects_directory=False)
    
    # override 시도
    publish_service.override_kaggle_metadata_id(artifacts, "new-user/new-dataset")
    
    # 원본 ID가 유지되어야 함
    assert json.loads(metadata_path.read_text())["id"] == original_id


def test_local_publish_roots_empty(monkeypatch):
    """publish root 설정이 없을 때 빈 튜플을 반환하는지 테스트."""
    monkeypatch.setenv("KPUBDATA_LOCAL_PUBLISH_ROOTS", "")
    assert publish_service._get_local_publish_roots() == ()


def test_local_publish_roots_unix(monkeypatch, tmp_path):
    """Unix 경로 구분자로 여러 root를 파싱하는지 테스트 (Windows에서는 skip)."""
    import platform
    if platform.system() == "Windows":
        pytest.skip("Unix test skipped on Windows")
    
    roots = f"{tmp_path}/root1:{tmp_path}/root2:{tmp_path}/root3"
    monkeypatch.setenv("KPUBDATA_LOCAL_PUBLISH_ROOTS", roots)
    
    result = publish_service._get_local_publish_roots()
    assert len(result) == 3
    assert all(path.is_absolute() for path in result)


def test_local_publish_roots_windows(monkeypatch, tmp_path):
    """Windows 경로 구분자로 여러 root를 파싱하는지 테스트."""
    roots = f"{tmp_path}\\root1;{tmp_path}\\root2;{tmp_path}\\root3"
    monkeypatch.setenv("KPUBDATA_LOCAL_PUBLISH_ROOTS", roots)
    
    result = publish_service._get_local_publish_roots()
    assert len(result) == 3
    assert all(path.is_absolute() for path in result)


def test_local_destination_validation_disabled(monkeypatch):
    """publish가 비활성화된 상태를 테스트."""
    monkeypatch.setenv("KPUBDATA_LOCAL_PUBLISH_ROOTS", "")
    
    issue = publish_service._validate_local_destination("/any/path")
    assert issue is not None
    assert issue.code == "local_publish_disabled"


def test_local_destination_validation_not_absolute():
    """상대 경로가 거부되는지 테스트."""
    # publish가 비활성화된 상태가 먼저 체크됨
    issue = publish_service._validate_local_destination("relative/path")
    assert issue is not None
    assert issue.code == "local_publish_disabled"


def test_local_destination_validation_allowed(tmp_path, monkeypatch):
    """허용된 루트 내 경로가 승인되는지 테스트."""
    monkeypatch.setenv("KPUBDATA_LOCAL_PUBLISH_ROOTS", str(tmp_path))
    
    allowed_dest = tmp_path / "safe" / "dataset"
    issue = publish_service._validate_local_destination(str(allowed_dest))
    assert issue is None


def test_local_destination_validation_not_allowed(tmp_path, monkeypatch):
    """허용되지 않은 경로가 거부되는지 테스트."""
    monkeypatch.setenv("KPUBDATA_LOCAL_PUBLISH_ROOTS", str(tmp_path / "allowed"))
    
    # 허용되지 않은 경로
    not_allowed_dest = tmp_path / "not-allowed" / "dataset"
    issue = publish_service._validate_local_destination(str(not_allowed_dest))
    assert issue is not None
    assert issue.code == "local_destination_not_allowed"


def test_local_destination_validation_traversal(tmp_path, monkeypatch):
    """Path traversal 시도가 거부되는지 테스트."""
    allowed_root = tmp_path / "allowed"
    allowed_root.mkdir()
    monkeypatch.setenv("KPUBDATA_LOCAL_PUBLISH_ROOTS", str(allowed_root))
    
    # Path traversal 시도
    traversal_dest = allowed_root / ".." / "etc" / "passwd"
    issue = publish_service._validate_local_destination(str(traversal_dest))
    assert issue is not None
    assert issue.code == "local_destination_not_allowed"


def test_kaggle_credential_configured(monkeypatch):
    """Kaggle credential 설정 확인 테스트."""
    monkeypatch.setenv("KAGGLE_USERNAME", "test_user")
    monkeypatch.setenv("KAGGLE_KEY", "test_key")
    
    assert publish_service._kaggle_credential_configured() is True


def test_kaggle_credential_not_configured_missing_username(monkeypatch):
    """Kaggle credential 누락 테스트 (username 없음)."""
    monkeypatch.setenv("KAGGLE_KEY", "test_key")
    monkeypatch.delenv("KAGGLE_USERNAME", raising=False)
    
    assert publish_service._kaggle_credential_configured() is False


def test_kaggle_credential_not_configured_missing_key(monkeypatch):
    """Kaggle credential 누락 테스트 (key 없음)."""
    monkeypatch.setenv("KAGGLE_USERNAME", "test_user")
    monkeypatch.delenv("KAGGLE_KEY", raising=False)
    
    assert publish_service._kaggle_credential_configured() is False


def test_credential_blocker_huggingface(monkeypatch):
    """HuggingFace credential blocker 테스트."""
    monkeypatch.setenv("HF_TOKEN", "test_token")
    assert publish_service.credential_blocker("huggingface") is None
    
    monkeypatch.delenv("HF_TOKEN", raising=False)
    issue = publish_service.credential_blocker("huggingface")
    assert issue is not None
    assert issue.code == "credential_unavailable"


def test_credential_blocker_kaggle(monkeypatch):
    """Kaggle credential blocker 테스트."""
    monkeypatch.setenv("KAGGLE_USERNAME", "test_user")
    monkeypatch.setenv("KAGGLE_KEY", "test_key")
    assert publish_service.credential_blocker("kaggle") is None
    
    monkeypatch.delenv("KAGGLE_USERNAME", raising=False)
    issue = publish_service.credential_blocker("kaggle")
    assert issue is not None
    assert issue.code == "credential_unavailable"


def test_credential_blocker_local():
    """Local target은 항상 credential이 필요 없음을 테스트."""
    assert publish_service.credential_blocker("local") is None


def test_credential_blocker_unknown():
    """알 수 없는 target에 대한 credential blocker 테스트."""
    issue = publish_service.credential_blocker("unknown")
    assert issue is not None
    assert issue.code == "credential_unavailable"


def test_validate_destination_local(tmp_path, monkeypatch):
    """Local target destination validation 테스트."""
    monkeypatch.setenv("KPUBDATA_LOCAL_PUBLISH_ROOTS", str(tmp_path))
    
    # 절대 경로 허용
    assert publish_service.validate_destination("local", str(tmp_path / "dest")) is None
    
    # 상대 경로 거부
    error = publish_service.validate_destination("local", "relative/path")
    assert error is not None
    assert "must be an absolute path" in error


def test_validate_destination_kaggle():
    """Kaggle target destination validation 테스트."""
    # 올바른 형식
    assert publish_service.validate_destination("kaggle", "user/dataset") is None
    
    # URL 거부
    assert publish_service.validate_destination("kaggle", "https://example.com") is not None
    
    # 절대 경로 거부
    assert publish_service.validate_destination("kaggle", "/absolute/path") is not None
    
    # Path traversal 거부
    assert publish_service.validate_destination("kaggle", "user/../dataset") is not None


def test_validate_destination_huggingface():
    """HuggingFace target destination validation 테스트."""
    # 올바른 형식
    assert publish_service.validate_destination("huggingface", "user/dataset") is None
    
    # 잘못된 형식
    assert publish_service.validate_destination("huggingface", "invalid") is not None


def test_http_publish_targets():
    """HTTP publish targets에 kaggle과 local이 포함되어 있는지 테스트."""
    targets = publish_service.HTTP_PUBLISH_TARGETS
    assert "huggingface" in targets
    assert "kaggle" in targets
    assert "local" in targets


def test_allowed_options():
    """target별 허용된 options가 올바른지 테스트."""
    assert publish_service._ALLOWED_OPTIONS == {
        "huggingface": {"private": bool},
        "kaggle": {"public": bool},
        "local": {},
    }


def test_default_options():
    """target별 기본 options가 올바른지 테스트."""
    assert publish_service._DEFAULT_OPTIONS == {
        "huggingface": {"private": True},
        "kaggle": {"public": False},
        "local": {},
    }


def test_resolve_target_kaggle():
    """Kaggle target resolve 테스트."""
    target, error = publish_service.resolve_target("kaggle")
    assert target == "kaggle"
    assert error is None


def test_resolve_target_local():
    """Local target resolve 테스트."""
    target, error = publish_service.resolve_target("local")
    assert target == "local"
    assert error is None


def test_resolve_target_unknown():
    """알 수 없는 target resolve 테스트."""
    target, error = publish_service.resolve_target("unknown")
    assert target is None
    assert error is not None
    assert "unknown publish target" in error


def test_build_readiness_with_local_destination(tmp_path, monkeypatch):
    """build_readiness에서 local destination validation이 작동하는지 테스트."""
    monkeypatch.setenv("KPUBDATA_LOCAL_PUBLISH_ROOTS", str(tmp_path))
    
    # 허용된 경로
    result = publish_service.build_readiness(
        run_id="test-run",
        target="local",
        status="succeeded",
        manifest={"outputs": [], "errors": []},
        spec=None,
        output_root=tmp_path,
        destination=str(tmp_path / "allowed"),
    )
    
    # local destination validation이 통과해야 함
    assert "local_destination_not_allowed" not in [b.code for b in result.blockers]
    assert "local_publish_disabled" not in [b.code for b in result.blockers]


def test_build_readiness_local_destination_blocked(tmp_path, monkeypatch):
    """build_readiness에서 허용되지 않은 local destination이 차단되는지 테스트."""
    monkeypatch.setenv("KPUBDATA_LOCAL_PUBLISH_ROOTS", str(tmp_path / "allowed"))
    
    # 허용되지 않은 경로
    result = publish_service.build_readiness(
        run_id="test-run",
        target="local",
        status="succeeded",
        manifest={"outputs": [], "errors": []},
        spec=None,
        output_root=tmp_path,
        destination=str(tmp_path / "not-allowed"),
    )
    
    # local destination validation이 차단해야 함
    assert "local_destination_not_allowed" in [b.code for b in result.blockers]


def test_build_readiness_local_destination_not_configured(tmp_path, monkeypatch):
    """build_readiness에서 publish가 설정되지 않았을 때 차단되는지 테스트."""
    monkeypatch.setenv("KPUBDATA_LOCAL_PUBLISH_ROOTS", "")
    
    result = publish_service.build_readiness(
        run_id="test-run",
        target="local",
        status="succeeded",
        manifest={"outputs": [], "errors": []},
        spec=None,
        output_root=tmp_path,
        destination="/any/path",
    )
    
    # local publish disabled blocker가 있어야 함
    assert "local_publish_disabled" in [b.code for b in result.blockers]