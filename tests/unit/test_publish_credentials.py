"""요청자별 publish credential 해석 (#635).

publish credential 이 서버 환경변수 하나였다. 그러면 인증된 아무 사용자나
서버 소유자의 Hugging Face / Kaggle 계정으로 게시할 수 있다 — provider
credential 은 이미 principal 별 저장소를 갖고 있는데(ADR 0012) publish 경로만
그 앞을 지나쳐 환경변수를 직접 읽었다.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kpubdata_builder.credentials.crypto import AesGcmCredentialCipher
from kpubdata_builder.credentials.store import SQLiteCredentialRepository
from kpubdata_builder.service.publish_credentials import (
    PUBLISH_CREDENTIAL_SLOTS,
    _slot,
    resolve_publish_credentials,
)


class _Repo:
    """slot 이름을 검증하지 않는 가짜 저장소.

    **이 가짜가 버그를 숨겼다.** 처음 구현은 slot 을
    ``publish:huggingface:HF_TOKEN`` 으로 만들었는데, 실제 저장소는 provider key
    를 ``^[a-z0-9][a-z0-9_-]{0,63}$`` 로 검증하므로 ValueError 가 난다. 가짜는
    그냥 dict 조회라 통과했다. 아래 ``TestAgainstTheRealStore`` 가 실제 SQLite
    저장소로 같은 계약을 다시 확인한다.
    """

    def __init__(self, secrets: dict[str, str]) -> None:
        self._secrets = secrets

    def get_secret(self, owner_id: str, provider: str) -> str | None:
        return self._secrets.get(f"{owner_id}|{provider}")


class _BrokenRepo:
    def get_secret(self, owner_id: str, provider: str) -> str | None:
        raise RuntimeError("credential backend unavailable")


class TestResolution:
    def test_a_stored_credential_wins_over_the_server_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HF_TOKEN", "server-token")
        repo = _Repo({f"oidc:a|{_slot('huggingface', 'HF_TOKEN')}": "her-own-token"})

        assert resolve_publish_credentials(repo, "oidc:a", "huggingface").values == {
            "HF_TOKEN": "her-own-token"
        }

    def test_the_server_token_is_still_the_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 단일 사용자 배포에서는 전역 토큰 하나가 정상 구성이다 — 깨지 않는다.
        monkeypatch.setenv("HF_TOKEN", "server-token")

        assert resolve_publish_credentials(_Repo({}), "oidc:a", "huggingface").values == {
            "HF_TOKEN": "server-token"
        }

    def test_no_credential_anywhere_resolves_to_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("HF_TOKEN", raising=False)

        assert resolve_publish_credentials(_Repo({}), "oidc:a", "huggingface").values == {}

    def test_an_anonymous_caller_falls_back_to_the_server(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HF_TOKEN", "server-token")

        assert resolve_publish_credentials(_Repo({}), None, "huggingface").values == {
            "HF_TOKEN": "server-token"
        }

    def test_a_paired_credential_is_never_half_stored_half_server(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """하나는 요청자 것, 하나는 서버 것을 섞으면 어느 계정인지 알 수 없다."""
        monkeypatch.setenv("KAGGLE_USERNAME", "server-user")
        monkeypatch.setenv("KAGGLE_KEY", "server-key")
        repo = _Repo({f"oidc:a|{_slot('kaggle', 'KAGGLE_USERNAME')}": "her-user"})

        assert resolve_publish_credentials(repo, "oidc:a", "kaggle").values == {
            "KAGGLE_USERNAME": "her-user"
        }

    def test_local_publishing_needs_no_credential(self) -> None:
        assert resolve_publish_credentials(_Repo({}), "oidc:a", "local").values == {}

    def test_a_publish_slot_does_not_collide_with_a_provider_slot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 같은 owner 가 datago credential 과 HF 토큰을 둘 다 가질 수 있어야 한다.
        # provider slot 에 값이 있어도 publish 는 그것을 집지 않는다.
        monkeypatch.delenv("HF_TOKEN", raising=False)
        repo = _Repo({"oidc:a|huggingface": "provider-shaped-value"})

        assert resolve_publish_credentials(repo, "oidc:a", "huggingface").values == {}


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

        HuggingFacePublisher().publish(
            (),
            destination="kpubdata/x",
            credentials={"HF_TOKEN": "her-own-token"},
        )

        assert captured["token"] == "her-own-token"

    def test_huggingface_falls_back_to_the_server_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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

        HuggingFacePublisher().publish((), destination="kpubdata/x")

        assert captured["token"] == "server-token"

    def test_huggingface_refuses_when_no_token_exists_anywhere(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys
        import types

        module = types.ModuleType("huggingface_hub")
        module.HfApi = object  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "huggingface_hub", module)
        monkeypatch.delenv("HF_TOKEN", raising=False)

        from kpubdata_builder.publishers.huggingface import HuggingFacePublisher

        with pytest.raises(RuntimeError, match="No Hugging Face API token"):
            HuggingFacePublisher().publish((), destination="kpubdata/x")


class TestAgainstTheRealStore:
    """가짜가 아니라 실제 SQLite 저장소로 확인한다 (#635, #655).

    slot 이름이 저장소의 provider key 검증을 통과하는지는 가짜로는 알 수 없다.
    """

    @staticmethod
    def _repository(tmp_path: Path) -> SQLiteCredentialRepository:
        return SQLiteCredentialRepository(
            tmp_path / "credentials.sqlite3", AesGcmCredentialCipher(b"k" * 32)
        )

    @pytest.mark.parametrize("target", ["huggingface", "kaggle"])
    def test_every_slot_name_is_storable(self, tmp_path: Path, target: str) -> None:
        repository = self._repository(tmp_path)

        for variable in PUBLISH_CREDENTIAL_SLOTS[target]:
            slot = _slot(target, variable)
            repository.put("oidc:a", slot, f"value-for-{variable}")

            assert repository.get_secret("oidc:a", slot) == f"value-for-{variable}"

    def test_a_stored_token_round_trips_through_resolution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HF_TOKEN", "server-token")
        repository = self._repository(tmp_path)
        repository.put("oidc:a", _slot("huggingface", "HF_TOKEN"), "her-own-token")

        assert resolve_publish_credentials(repository, "oidc:a", "huggingface").values == {
            "HF_TOKEN": "her-own-token"
        }

    def test_an_owner_without_a_stored_token_still_gets_the_server_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HF_TOKEN", "server-token")

        assert resolve_publish_credentials(
            self._repository(tmp_path), "oidc:b", "huggingface"
        ).values == {"HF_TOKEN": "server-token"}

    def test_a_publish_slot_never_collides_with_a_provider_slot(self, tmp_path: Path) -> None:
        # 같은 owner 가 datago provider key 와 HF 토큰을 둘 다 가질 수 있어야 한다.
        repository = self._repository(tmp_path)
        repository.put("oidc:a", "datago", "provider-key")
        repository.put("oidc:a", _slot("huggingface", "HF_TOKEN"), "hf-token")

        assert repository.get_secret("oidc:a", "datago") == "provider-key"
        assert repository.get_secret("oidc:a", _slot("huggingface", "HF_TOKEN")) == "hf-token"


class TestABrokenStoreDoesNotBreakPublishing:
    def test_a_failing_lookup_falls_back_to_the_server(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 저장소 장애가 게시 실패가 되면, 전역 토큰만 쓰던 배포가 credential
        # 백엔드를 켠 순간 게시를 못 하게 된다.
        monkeypatch.setenv("HF_TOKEN", "server-token")

        assert resolve_publish_credentials(_BrokenRepo(), "oidc:a", "huggingface").values == {
            "HF_TOKEN": "server-token"
        }


class TestKaggleUsesTheCredentialsItIsGiven:
    def test_the_passed_credentials_reach_the_sdk(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """인자를 받기만 하고 쓰지 않으면 모든 게시가 서버 계정으로 나간다."""
        import sys
        import types

        seen: dict[str, str | None] = {}

        class _Api:
            def authenticate(self) -> None:
                seen["user"] = os.environ.get("KAGGLE_USERNAME")
                seen["key"] = os.environ.get("KAGGLE_KEY")
                raise RuntimeError("stop here — authentication is all we need to observe")

        module = types.ModuleType("kaggle.api.kaggle_api_extended")
        module.KaggleApi = _Api  # type: ignore[attr-defined]
        parent = types.ModuleType("kaggle")
        api_pkg = types.ModuleType("kaggle.api")
        monkeypatch.setitem(sys.modules, "kaggle", parent)
        monkeypatch.setitem(sys.modules, "kaggle.api", api_pkg)
        monkeypatch.setitem(sys.modules, "kaggle.api.kaggle_api_extended", module)
        monkeypatch.setenv("KAGGLE_USERNAME", "server-user")
        monkeypatch.setenv("KAGGLE_KEY", "server-key")

        from kpubdata_builder.publishers.kaggle import KagglePublisher

        with pytest.raises(Exception):  # noqa: B017 - authenticate 가 멈추는 지점
            KagglePublisher().publish(
                (),
                destination="owner/name",
                credentials={"KAGGLE_USERNAME": "her-user", "KAGGLE_KEY": "her-key"},
            )

        assert seen == {"user": "her-user", "key": "her-key"}

    def test_the_environment_is_restored_afterwards(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 한 요청의 자격이 다음 요청에 남으면 안 된다.
        import sys
        import types

        class _Api:
            def authenticate(self) -> None:
                raise RuntimeError("stop")

        module = types.ModuleType("kaggle.api.kaggle_api_extended")
        module.KaggleApi = _Api  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "kaggle", types.ModuleType("kaggle"))
        monkeypatch.setitem(sys.modules, "kaggle.api", types.ModuleType("kaggle.api"))
        monkeypatch.setitem(sys.modules, "kaggle.api.kaggle_api_extended", module)
        monkeypatch.setenv("KAGGLE_USERNAME", "server-user")

        from kpubdata_builder.publishers.kaggle import KagglePublisher

        with pytest.raises(Exception):  # noqa: B017
            KagglePublisher().publish(
                (), destination="owner/name", credentials={"KAGGLE_USERNAME": "her-user"}
            )

        assert os.environ["KAGGLE_USERNAME"] == "server-user"


class TestClosingTheServerFallback:
    """다중 사용자 배포는 서버 토큰 폴백을 닫을 수 있어야 한다 (#635).

    폴백이 열려 있는 한, credential 을 저장하지 않은 principal 은 여전히 서버
    소유자 계정으로 게시한다 — 요청자별 credential 을 넣은 것만으로는 원래
    문제가 닫히지 않는다.
    """

    def test_the_fallback_is_open_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 단일 사용자 배포에서 전역 토큰 하나는 정상 구성이다. 기본값을 뒤집으면
        # 그 배포가 조용히 게시를 멈춘다.
        monkeypatch.delenv("KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL", raising=False)
        monkeypatch.setenv("HF_TOKEN", "server-token")

        assert resolve_publish_credentials(_Repo({}), "oidc:b", "huggingface").values == {
            "HF_TOKEN": "server-token"
        }

    def test_closing_it_leaves_a_principal_without_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL", "true")
        monkeypatch.setenv("HF_TOKEN", "server-token")

        resolution = resolve_publish_credentials(_Repo({}), "oidc:b", "huggingface")

        assert resolution.values == {}
        # 비어 있다는 것만으로는 부족하다 — 호출자가 "그럼 안 넘기면 되지" 로
        # 처리하면 publisher 가 환경변수로 내려간다. 거절이라고 말해야 한다.
        assert resolution.refused is True

    def test_closing_it_does_not_affect_a_principal_with_their_own(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("KPUBDATA_BUILDER_REQUIRE_OWN_PUBLISH_CREDENTIAL", "true")
        monkeypatch.setenv("HF_TOKEN", "server-token")
        repo = _Repo({f"oidc:a|{_slot('huggingface', 'HF_TOKEN')}": "hers"})

        assert resolve_publish_credentials(repo, "oidc:a", "huggingface").values == {
            "HF_TOKEN": "hers"
        }


class TestTheSwitchActuallyBlocksPublishing:
    """``REQUIRE_OWN_PUBLISH_CREDENTIAL=true`` 가 실제로 게시를 막는지.

    resolver 는 처음부터 ``{}`` 를 돌려주고 있었고 그 단위 테스트도 통과했다.
    그런데 ``publish_api`` 가 ``if credentials:`` 로 빈 결과면 kwarg 를 생략했고,
    publisher 는 인자가 없으면 ``os.environ`` 을 읽었다. 그래서 스위치를 켜도
    인증된 아무나 서버 계정으로 게시할 수 있었다 — 세 조각 모두 각자의 테스트를
    통과하면서.
    """

    def _hf_api(self, monkeypatch: pytest.MonkeyPatch, captured: dict[str, object]) -> None:
        import sys
        import types

        class _Api:
            def __init__(self, token: str | None = None) -> None:
                captured["token"] = token

            def create_repo(self, **_kwargs: object) -> None: ...
            def upload_file(self, **_kwargs: object) -> None: ...
            def upload_folder(self, **_kwargs: object) -> None: ...

        module = types.ModuleType("huggingface_hub")
        module.HfApi = _Api  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "huggingface_hub", module)

    def test_an_empty_mapping_does_not_fall_back_to_the_server_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """이게 우회의 마지막 관문이었다."""
        captured: dict[str, object] = {}
        self._hf_api(monkeypatch, captured)
        monkeypatch.setenv("HF_TOKEN", "server-token")

        from kpubdata_builder.publishers.huggingface import HuggingFacePublisher

        with pytest.raises(RuntimeError, match="No Hugging Face API token"):
            HuggingFacePublisher().publish((), destination="kpubdata/x", credentials={})

        assert "token" not in captured, "서버 토큰으로 API 를 만들면 안 된다"

    def test_omitting_the_argument_still_uses_the_server_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI 경로는 그대로다 — 실행하는 사람과 서버 환경이 같은 경우다.

        ``None`` 과 ``{}`` 의 구분이 이 fix 의 전부이므로 양쪽을 함께 고정한다.
        """
        captured: dict[str, object] = {}
        self._hf_api(monkeypatch, captured)
        monkeypatch.setenv("HF_TOKEN", "server-token")

        from kpubdata_builder.publishers.huggingface import HuggingFacePublisher

        HuggingFacePublisher().publish((), destination="kpubdata/x")

        assert captured["token"] == "server-token"

    def test_kaggle_with_an_empty_mapping_hides_the_server_account(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Kaggle SDK 는 환경변수만 읽는다. 비워 주지 않으면 서버 계정을 집어 든다."""
        import os

        from kpubdata_builder.publishers.kaggle import _kaggle_environment

        monkeypatch.setenv("KAGGLE_USERNAME", "server-user")
        monkeypatch.setenv("KAGGLE_KEY", "server-key")

        with _kaggle_environment({}):
            assert "KAGGLE_USERNAME" not in os.environ
            assert "KAGGLE_KEY" not in os.environ

        # 블록을 벗어나면 원래대로 돌아온다.
        assert os.environ["KAGGLE_USERNAME"] == "server-user"
        assert os.environ["KAGGLE_KEY"] == "server-key"

    def test_kaggle_with_no_argument_leaves_the_environment_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os

        from kpubdata_builder.publishers.kaggle import _kaggle_environment

        monkeypatch.setenv("KAGGLE_USERNAME", "server-user")

        with _kaggle_environment(None):
            assert os.environ["KAGGLE_USERNAME"] == "server-user"


class TestReadinessAgreesWithPublish:
    """readiness 와 POST 가 같은 기준으로 판정해야 한다.

    readiness 는 서버 환경변수만 봤다. 그래서 폴백을 닫아 둔 배포에서 readiness
    는 ready 를 답하고 POST 는 거절하는, 서로 다른 두 답이 나왔다.
    """

    def test_a_refused_resolution_blocks_readiness(self) -> None:
        from kpubdata_builder.service.publish import credential_blocker
        from kpubdata_builder.service.publish_credentials import PublishCredentialResolution

        issue = credential_blocker("huggingface", PublishCredentialResolution(refused=True))

        assert issue is not None
        assert issue.code == "credential_required"

    def test_a_resolved_credential_clears_readiness(self) -> None:
        from kpubdata_builder.service.publish import credential_blocker
        from kpubdata_builder.service.publish_credentials import PublishCredentialResolution

        resolution = PublishCredentialResolution(values={"HF_TOKEN": "hers"})

        assert credential_blocker("huggingface", resolution) is None

    def test_an_empty_resolution_blocks_readiness(self) -> None:
        from kpubdata_builder.service.publish import credential_blocker
        from kpubdata_builder.service.publish_credentials import PublishCredentialResolution

        issue = credential_blocker("huggingface", PublishCredentialResolution())

        assert issue is not None
        assert issue.code == "credential_unavailable"

    def test_callers_without_a_resolution_keep_the_old_server_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI/기존 호출자는 동작이 바뀌지 않는다."""
        from kpubdata_builder.service.publish import credential_blocker

        monkeypatch.setenv("HF_TOKEN", "server-token")

        assert credential_blocker("huggingface") is None


class TestKaggleCredentialsDoNotCross:
    """환경변수는 스레드가 아니라 프로세스에 속한다.

    receipt 직렬화는 ``(owner, run, target)`` 단위라 서로 다른 principal 의 동시
    publish 를 막지 않는다. lock 이 없을 때 실제로 한쪽이 다른 쪽의 자격을 보고
    (``('bob', 'alice')``), 다른 쪽 정리 과정에서 변수가 사라져 KeyError 까지 났다.
    """

    def test_concurrent_publishes_each_see_their_own_credentials(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os
        import threading
        import time

        from kpubdata_builder.publishers.kaggle import _kaggle_environment

        monkeypatch.setenv("KAGGLE_USERNAME", "server-user")
        monkeypatch.setenv("KAGGLE_KEY", "server-key")

        observed: list[tuple[str, str]] = []
        start = threading.Barrier(2)

        def _publish_as(user: str) -> None:
            start.wait(timeout=5)
            with _kaggle_environment({"KAGGLE_USERNAME": user, "KAGGLE_KEY": f"{user}-key"}):
                time.sleep(0.05)
                observed.append((user, os.environ["KAGGLE_USERNAME"]))

        threads = [threading.Thread(target=_publish_as, args=(u,)) for u in ("alice", "bob")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        assert len(observed) == 2
        assert all(wanted == seen for wanted, seen in observed), observed

    def test_the_server_environment_is_restored_afterwards(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import os

        from kpubdata_builder.publishers.kaggle import _kaggle_environment

        monkeypatch.setenv("KAGGLE_USERNAME", "server-user")

        with _kaggle_environment({"KAGGLE_USERNAME": "hers", "KAGGLE_KEY": "her-key"}):
            assert os.environ["KAGGLE_USERNAME"] == "hers"

        assert os.environ["KAGGLE_USERNAME"] == "server-user"

    def test_an_empty_mapping_also_hides_the_kaggle_json_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """env 를 비워도 SDK 는 ``~/.kaggle/kaggle.json`` 을 읽는다.

        그 파일이 서버에 있으면 결국 서버 계정으로 인증되므로, 두 번째 방어선이
        없으면 REQUIRE_OWN_PUBLISH_CREDENTIAL 이 다시 우회된다.
        """
        import os
        from pathlib import Path

        from kpubdata_builder.publishers.kaggle import _kaggle_environment

        monkeypatch.setenv("KAGGLE_USERNAME", "server-user")
        monkeypatch.delenv("KAGGLE_CONFIG_DIR", raising=False)

        with _kaggle_environment({}):
            config_dir = os.environ.get("KAGGLE_CONFIG_DIR")
            assert config_dir is not None, "SDK 가 서버의 kaggle.json 을 찾을 수 있다"
            assert list(Path(config_dir).iterdir()) == []

        assert "KAGGLE_CONFIG_DIR" not in os.environ

    def test_the_cli_path_is_left_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``None`` 은 호출자가 정하지 않았다는 뜻이다 — 환경도 config 도 그대로."""
        import os

        from kpubdata_builder.publishers.kaggle import _kaggle_environment

        monkeypatch.setenv("KAGGLE_USERNAME", "server-user")
        monkeypatch.delenv("KAGGLE_CONFIG_DIR", raising=False)

        with _kaggle_environment(None):
            assert os.environ["KAGGLE_USERNAME"] == "server-user"
            assert "KAGGLE_CONFIG_DIR" not in os.environ
