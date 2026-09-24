"""요청자별 publish credential 해석 (#635).

publish credential 이 서버 환경변수 하나였다. 그러면 인증된 아무 사용자나
서버 소유자의 Hugging Face / Kaggle 계정으로 게시할 수 있다 — provider
credential 은 이미 principal 별 저장소를 갖고 있는데(ADR 0012) publish 경로만
그 앞을 지나쳐 환경변수를 직접 읽었다.
"""

from __future__ import annotations

import pytest

from kpubdata_builder.service.publish_credentials import resolve_publish_credentials


class _Repo:
    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = secrets

    def get_secret(self, owner_id: str, provider: str) -> str | None:
        return self._secrets.get(f"{owner_id}|{provider}")


class TestResolution:
    def test_a_stored_credential_wins_over_the_server_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HF_TOKEN", "server-token")
        repo = _Repo({"oidc:a|publish:huggingface:HF_TOKEN": "her-own-token"})

        assert resolve_publish_credentials(repo, "oidc:a", "huggingface") == {
            "HF_TOKEN": "her-own-token"
        }

    def test_the_server_token_is_still_the_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 단일 사용자 배포에서는 전역 토큰 하나가 정상 구성이다 — 깨지 않는다.
        monkeypatch.setenv("HF_TOKEN", "server-token")

        assert resolve_publish_credentials(_Repo({}), "oidc:a", "huggingface") == {
            "HF_TOKEN": "server-token"
        }

    def test_no_credential_anywhere_resolves_to_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HF_TOKEN", raising=False)

        assert resolve_publish_credentials(_Repo({}), "oidc:a", "huggingface") == {}

    def test_an_anonymous_caller_falls_back_to_the_server(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HF_TOKEN", "server-token")

        assert resolve_publish_credentials(_Repo({}), None, "huggingface") == {
            "HF_TOKEN": "server-token"
        }

    def test_a_paired_credential_is_never_half_stored_half_server(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """하나는 요청자 것, 하나는 서버 것을 섞으면 어느 계정인지 알 수 없다."""
        monkeypatch.setenv("KAGGLE_USERNAME", "server-user")
        monkeypatch.setenv("KAGGLE_KEY", "server-key")
        repo = _Repo({"oidc:a|publish:kaggle:KAGGLE_USERNAME": "her-user"})

        assert resolve_publish_credentials(repo, "oidc:a", "kaggle") == {
            "KAGGLE_USERNAME": "her-user"
        }

    def test_local_publishing_needs_no_credential(self) -> None:
        assert resolve_publish_credentials(_Repo({}), "oidc:a", "local") == {}

    def test_a_publish_slot_does_not_collide_with_a_provider_slot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 같은 owner 가 datago credential 과 HF 토큰을 둘 다 가질 수 있어야 한다.
        # provider slot 에 값이 있어도 publish 는 그것을 집지 않는다.
        monkeypatch.delenv("HF_TOKEN", raising=False)
        repo = _Repo({"oidc:a|huggingface": "provider-shaped-value"})

        assert resolve_publish_credentials(repo, "oidc:a", "huggingface") == {}


class TestPublisherPrefersPassedCredentials:
    def test_huggingface_uses_the_passed_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import sys
        import types

        captured: dict[str, object] = {}

        class _Api:
            def __init__(self, token: str | None = None) -> None:
                captured["token"] = token

            def create_repo(self, **_kwargs: object) -> None: ...
            def upload_file(self, **_kwargs: object) -> None: ...
            def upload_folder(self, **_kwargs: object) -> None: ...

        module = types.ModuleType("huggingface_hub")
        module.HfApi = _Api  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "huggingface_hub", module)
        monkeypatch.setenv("HF_TOKEN", "server-token")

        from kpubdata_builder.publishers.huggingface import HuggingFacePublisher

        with pytest.raises(Exception):  # noqa: B017 - 업로드까지 가지 않아도 된다
            HuggingFacePublisher().publish(
                (),
                destination="kpubdata/x",
                credentials={"HF_TOKEN": "her-own-token"},
            )

        assert captured["token"] == "her-own-token"
