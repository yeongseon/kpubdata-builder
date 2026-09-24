"""Publish credential 해석 — 요청자별 우선, 서버 전역은 폴백 (#635).

publish credential 이 요청자별이 아니라 서버 환경변수 하나였다. 그러면 인증된
아무 사용자나 **서버 소유자의 Hugging Face / Kaggle 계정으로** 게시할 수 있다.
provider credential 은 이미 principal 별 저장소를 갖고 있는데(ADR 0012) publish
경로만 그 앞을 지나쳐 환경변수를 직접 읽었다.

여기서 기본값을 바꾸지는 않는다. 요청자에게 저장된 credential 이 있으면 그것을
쓰고, 없으면 예전처럼 서버 환경변수로 내려간다 — 단일 사용자 배포에서는 전역
토큰 하나가 정상 구성이고, 그 배포를 깨지 않는다. 다중 사용자 배포가 요청자별로
분리하고 싶으면 이제 그렇게 할 수 있다는 것이 이 모듈의 요점이다.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from kpubdata_builder.credentials.store import CredentialRepository

__all__ = ["PUBLISH_CREDENTIAL_SLOTS", "resolve_publish_credentials"]

#: target 별로 필요한 환경변수 이름과, 그 값을 저장하는 credential slot.
#:
#: slot 이름에 ``publish:`` 를 붙여 provider credential(``datago`` 등)과 같은
#: 이름공간을 쓰지 않게 한다 — 같은 owner 가 둘 다 가질 수 있어야 한다.
PUBLISH_CREDENTIAL_SLOTS: Mapping[str, tuple[str, ...]] = {
    "huggingface": ("HF_TOKEN",),
    "kaggle": ("KAGGLE_USERNAME", "KAGGLE_KEY"),
    "local": (),
}


def _slot(target: str, variable: str) -> str:
    return f"publish:{target}:{variable}"


def resolve_publish_credentials(
    repository: CredentialRepository | None,
    owner_id: str | None,
    target: str,
) -> dict[str, str]:
    """이 게시에 쓸 credential 을 해석한다.

    우선순위는 ``CredentialResolver`` 와 같다 — 요청자 credential, 그다음 서버
    환경변수. 어느 쪽에도 없으면 그 키는 결과에 없다.

    **부분 해석을 하지 않는다.** kaggle 처럼 두 값이 한 쌍인 target 에서 하나는
    요청자 것, 하나는 서버 것을 섞으면 어느 계정으로 게시되는지 아무도 말할 수
    없다. 요청자가 그 target 의 값을 하나라도 저장해 두었으면 그 target 은
    요청자 credential 로만 해석한다.
    """
    variables = PUBLISH_CREDENTIAL_SLOTS.get(target, ())
    if not variables:
        return {}

    stored: dict[str, str] = {}
    if repository is not None and owner_id is not None:
        for variable in variables:
            value = repository.get_secret(owner_id, _slot(target, variable))
            if value:
                stored[variable] = value
    if stored:
        return stored

    resolved: dict[str, str] = {}
    for variable in variables:
        value = os.environ.get(variable, "").strip()
        if value:
            resolved[variable] = value
    return resolved
