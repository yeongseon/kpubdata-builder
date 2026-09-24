"""Kaggle API를 통해 데이터셋을 퍼블리시하는 publisher."""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator, Mapping
from pathlib import Path

from ..errors import PublishError
from .base import BasePublisher, PublishResult


@contextlib.contextmanager
def _kaggle_environment(credentials: Mapping[str, str] | None) -> Iterator[None]:
    """전달받은 Kaggle 자격을 이 블록 동안만 환경에 둔다.

    Kaggle SDK 에 자격을 인자로 넘길 방법이 없어서 환경을 거친다. 블록을 벗어나면
    원래 값으로 되돌리므로, 한 요청의 자격이 다음 요청에 남지 않는다.

    같은 프로세스에서 두 게시가 동시에 돌면 이 방식은 안전하지 않다. publish 는
    run 단위로 직렬화되어 있어 현재는 문제가 되지 않지만, 병렬 게시를 도입한다면
    SDK 를 감싸는 다른 방법이 필요하다.
    """
    if not credentials:
        yield
        return
    previous = {key: os.environ.get(key) for key in credentials}
    os.environ.update(credentials)
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
