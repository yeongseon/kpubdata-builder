"""테스트 설정 공유."""

import os

import pytest

# kpubdata provider key 환경변수 패턴 — 로컬 개발 환경에서 설정되어 있으면
# BuilderService._create_client가 test의 FakeClient에 provider_keys를
# 전달하려다 실패한다(#577).
_PROVIDER_KEY_PREFIXES = ("KPUBDATA_DATAGO", "KPUBDATA_BOK", "KPUBDATA_KOSIS",
                          "KPUBDATA_LOFIN", "KPUBDATA_LOCALDATA", "KPUBDATA_SEMAS",
                          "KPUBDATA_SEOUL", "KPUBDATA_SGIS")


@pytest.fixture(autouse=True)
def dev_mode_for_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """모든 테스트에서 dev-mode를 설정하고 provider key를 격리한다.

    dev-mode: 인증을 생략한다 (#321, ADR 0006).
    provider key 격리: 로컬 환경의 실 API 키가 테스트에 누출되어
    FakeClient가 예기치 않은 kwargs를 받는 문제를 방지한다 (#577).

    개별 테스트에서 인증 동작을 검증하려면 이 fixture를 오버라이드하거나
    명시적으로 환경변수를 삭제/설정하면 된다.
    """
    monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")
    for key in list(os.environ):
        if any(key.startswith(prefix) for prefix in _PROVIDER_KEY_PREFIXES):
            monkeypatch.delenv(key)
