"""테스트 설정 공유."""

import pytest


@pytest.fixture(autouse=True)
def dev_mode_for_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """모든 테스트에서 dev-mode를 설정하여 인증을 생략한다 (#321, ADR 0006).

    개별 테스트에서 인증 동작을 검증하려면 이 fixture를 오버라이드하거나
    명시적으로 환경변수를 삭제/설정하면 된다.
    """
    monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")
