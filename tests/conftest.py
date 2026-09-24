"""테스트 설정 공유."""

import os

import pytest


@pytest.fixture(autouse=True)
def dev_mode_for_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """모든 테스트에서 dev-mode를 설정하여 인증을 생략한다 (#321, ADR 0006).

    개별 테스트에서 인증 동작을 검증하려면 이 fixture를 오버라이드하거나
    명시적으로 환경변수를 삭제/설정하면 된다.
    """
    monkeypatch.setenv("KPUBDATA_BUILDER_DEV_MODE", "true")


@pytest.fixture(autouse=True)
def hermetic_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """개발자 셸의 provider API 키가 테스트로 새는 것을 차단한다 (#577).

    ``CredentialResolver``는 user credential이 없으면 서버 기본값으로
    ``KPUBDATA_<PROVIDER>_API_KEY`` 환경변수(예: ``KPUBDATA_DATAGO_API_KEY``)를
    읽는다(kpubdata ``KPubDataConfig.from_env``). 로컬 devbox에 이 키가 설정돼
    있으면 dev-mode principal의 ``provider_keys``가 비어있지 않게 되어 테스트용
    단순 ``client_factory``(``lambda: client``)가 ``provider_keys`` kwarg를 받지
    못해 ``RuntimeError`` → 502 → run 디렉터리 미생성 → 후속 ``FileNotFoundError:
    manifest.json`` 연쇄를 일으킨다. CI 러너에는 이 키가 없어 통과하므로 로컬/CI
    결과가 갈렸다. 테스트를 환경 무관하게 hermetic하게 만들기 위해 여기서 모든
    ``KPUBDATA_*_API_KEY`` 를 제거한다. 특정 키가 필요한 테스트는 명시적으로
    ``monkeypatch.setenv`` 로 설정하면 된다.
    """
    for env_name in list(os.environ):
        if env_name.startswith("KPUBDATA_") and env_name.endswith("_API_KEY"):
            monkeypatch.delenv(env_name, raising=False)
