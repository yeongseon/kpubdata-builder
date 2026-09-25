"""Kaggle API를 통해 데이터셋을 퍼블리시하는 publisher."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from threading import Lock

from ..errors import PublishError
from .base import BasePublisher, PublishResult

#: Kaggle SDK 가 자격을 읽는 환경변수. 서비스 경로에서 빈 자격을 받았을 때
#: 이 키들을 비워서 SDK 가 서버 계정으로 인증하지 않게 한다.
_KAGGLE_ENVIRONMENT_KEYS = frozenset({"KAGGLE_USERNAME", "KAGGLE_KEY"})

#: SDK 가 kaggle.json 을 찾는 디렉터리. 자격을 정해 준 호출에서는 빈 디렉터리로
#: 돌려서, 환경을 비워도 서버의 파일 계정으로 인증되는 경로를 닫는다.
_KAGGLE_CONFIG_DIR_ENV = "KAGGLE_CONFIG_DIR"


#: 프로세스 전역 환경을 건드리는 구간을 직렬화한다.
#:
#: receipt 직렬화는 ``(owner, run, target)`` 단위라 **서로 다른 principal 의
#: 동시 publish 를 막지 않는다.** 그 둘이 이 구간에 함께 들어오면 한쪽이 다른
#: 쪽의 자격으로 인증할 수 있다 — 환경변수는 스레드가 아니라 프로세스에 속하기
#: 때문이다. 자격을 인자로 받지 않는 SDK 를 쓰는 한 이 lock 이 유일한 경계다.
_KAGGLE_ENVIRONMENT_LOCK = Lock()


@contextlib.contextmanager
def _kaggle_environment(credentials: Mapping[str, str] | None) -> Iterator[None]:
    """전달받은 Kaggle 자격을 이 블록 동안만, 배타적으로 환경에 둔다.

    Kaggle SDK 에 자격을 인자로 넘길 방법이 없어서 환경을 거친다. 블록을 벗어나면
    원래 값으로 되돌리므로, 한 요청의 자격이 다음 요청에 남지 않는다.

    ``None`` 은 "호출자가 정하지 않았다"(CLI 경로)라서 환경을 그대로 둔다.
    **빈 mapping 은 "줄 것이 없다"** 이므로 SDK 가 서버 계정을 집어 들지 않게
    관련 환경변수를 이 블록 동안 비운다 — 예전에는 둘을 구분하지 않아서, 요청자
    credential 을 강제하는 설정을 켜도 서버 계정으로 게시가 나갔다 (#635).

    환경변수를 비우는 것만으로는 부족하다. ``KaggleApi.authenticate()`` 는
    환경에서 자격을 찾지 못하면 ``~/.kaggle/kaggle.json`` 을 읽으므로, 서버에
    그 파일이 있으면 결국 서버 계정으로 인증된다. 그래서 호출자가 자격을 정한
    경우에는 ``KAGGLE_CONFIG_DIR`` 을 빈 임시 디렉터리로 돌려 파일 경로까지 닫는다.
    """
    if credentials is None:
        yield
        return
    managed = dict(credentials) if credentials else {}
    with _KAGGLE_ENVIRONMENT_LOCK, tempfile.TemporaryDirectory() as empty_config_dir:
        keys = set(managed) | _KAGGLE_ENVIRONMENT_KEYS | {_KAGGLE_CONFIG_DIR_ENV}
        previous = {key: os.environ.get(key) for key in keys}
        for key in keys:
            if key in managed:
                os.environ[key] = managed[key]
            elif key == _KAGGLE_CONFIG_DIR_ENV:
                os.environ[key] = empty_config_dir
            else:
                os.environ.pop(key, None)
        try:
            yield
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


class KagglePublisher(BasePublisher):
    """Kaggle API로 dataset-metadata.json이 있는 디렉터리를 업로드한다."""

    @property
    def name(self) -> str:
        return "kaggle"

    @property
    def expects_directory(self) -> bool:
        # Kaggle API는 dataset-metadata.json이 포함된 디렉터리 단위로 업로드한다 (#176).
        return True

    def publish(
        self,
        artifact_paths: tuple[Path, ...],
        *,
        destination: str,
        public: bool = False,
        credentials: Mapping[str, str] | None = None,
    ) -> PublishResult:
        """Kaggle 데이터셋을 새 버전으로 업로드한다.

        Args:
            artifact_paths: dataset-metadata.json을 포함하는 디렉터리 경로.
            destination: Kaggle dataset ID (e.g. "username/dataset-name"). 디렉터리의
                ``dataset-metadata.json`` ``id``와 반드시 일치해야 한다.
            public: 신규 데이터셋 생성 시 공개 여부. 안전을 위해 기본은 비공개이며,
                의도적으로 공개하려면 명시적으로 ``True``를 전달한다 (#177).
        """
        try:
            from kaggle.api.kaggle_api_extended import KaggleApi  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                "kaggle is required for Kaggle publishing. Install it with: pip install kaggle"
            ) from exc

        api = KaggleApi()
        # Kaggle SDK 는 환경변수에서만 자격을 읽는다. 요청자별 credential 을
        # 전달받았으면 이 호출 동안만 환경에 올려 두고 원래 값으로 되돌린다 —
        # 인자를 받기만 하고 쓰지 않으면 모든 게시가 서버 계정으로 나간다 (#635).
        with _kaggle_environment(credentials):
            # 인증 실패 예외를 PublishError로 변환해 CLI raw traceback 노출을 막는다 (#178).
            try:
                api.authenticate()
            except Exception as exc:
                raise PublishError(f"Kaggle authentication failed: {exc}") from exc

        count = 0
        for path in artifact_paths:
            if not path.is_dir():
                raise PublishError(
                    f"KagglePublisher expects a directory with dataset-metadata.json, "
                    f"got file: {path}"
                )

            # 업로드 전 dataset-metadata.json을 검증해, 실제 업로드 대상(metadata id)이
            # destination과 일치하는지 확인한다. Kaggle API는 metadata의 id로 대상을
            # 결정하므로 불일치 시 엉뚱한 데이터셋이 생성/갱신될 수 있다 (#177).
            metadata_path = path / "dataset-metadata.json"
            if not metadata_path.is_file():
                raise PublishError(f"KagglePublisher requires dataset-metadata.json in {path}")
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise PublishError(
                    f"Failed to read dataset-metadata.json in {path}: {exc}"
                ) from exc
            metadata_id = metadata.get("id") if isinstance(metadata, dict) else None
            if metadata_id != destination:
                raise PublishError(
                    f"dataset-metadata.json id {metadata_id!r} does not match "
                    f"destination {destination!r}; they must be identical so the upload "
                    "targets the intended dataset"
                )

            # dataset_list 실패를 삼키면 네트워크 오류 시 의도치 않게 신규(공개)
            # 데이터셋을 생성할 수 있으므로 PublishError로 전파한다 (#177).
            try:
                results = api.dataset_list(mine=True, search=destination.split("/")[-1])
            except Exception as exc:
                raise PublishError(
                    f"Failed to query existing Kaggle datasets for {destination}: {exc}"
                ) from exc
            dataset_exists = any(str(d) == destination for d in results)

            try:
                if dataset_exists:
                    api.dataset_create_version(
                        str(path),
                        version_notes="Update via kpubdata-builder",
                        dir_mode="zip",
                    )
                else:
                    api.dataset_create_new(folder=str(path), dir_mode="zip", public=public)
            except Exception as exc:
                raise PublishError(
                    f"Failed to publish Kaggle dataset to {destination}: {exc}"
                ) from exc
            count += 1

        return PublishResult(
            publisher=self.name,
            reference=f"https://www.kaggle.com/datasets/{destination}",
            artifact_count=count,
        )
