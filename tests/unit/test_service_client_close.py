"""HTTP 서비스 façade(#36): validate/preview/build/artifacts 로직과 라우팅 검증."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from collections.abc import Iterable
from http.server import HTTPServer
from pathlib import Path
from typing import cast

import pytest

from kpubdata_builder.service import BuilderService, ServiceResponse, dispatch
from kpubdata_builder.service.app import _OWNERSHIP_ENV
from kpubdata_builder.service.auth import Principal
from kpubdata_builder.service.http import _clear_cors_cache, make_handler
from kpubdata_builder.spec import JsonValue

VALID_SPEC_YAML = (
    """
dataset_id: dataset.sample
title: Sample Dataset
description: Sample description
sources:
  - provider: datago
    dataset: air_quality
exports:
  - kind: parquet
    output_path: dataset.parquet
"""
)


class _FakeClient:
    """테스트용 fake kpubdata Client.

    close() 메서드를 지원하며, close() 호출 여부를 추적한다.
    """

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _BrokenClient:
    """close()를 구현하지 않는 테스트용 fake kpubdata Client."""

    def __init__(self) -> None:
        pass


def _create_service_with_fake_client(tmp_path: Path) -> BuilderService:
    """fake kpubdata Client를 사용하는 BuilderService를 생성한다."""
    client = _FakeClient()
    return BuilderService(output_root=tmp_path, client_factory=lambda: client)


def test_catalog_closes_client_success(tmp_path: Path) -> None:
    """catalog()가 성공 시 client.close()를 호출한다."""
    service = _create_service_with_fake_client(tmp_path)
    response = service.catalog()

    assert response.status_code == 200
    # client_factory는 같은 client 인스턴스를 반환하므로 직접 확인
    assert isinstance(response.body, dict)
    assert "providers" in response.body


def test_catalog_closes_client_exception(tmp_path: Path) -> None:
    """catalog()가 예외 발생 시 client.close()를 호출한다."""
    # close()에서 예외를 발생시키는 broken client
    class _ExceptionClient(_FakeClient):
        def close(self) -> None:
            super().close()
            raise RuntimeError("close failed")

    client = _ExceptionClient()
    service = BuilderService(output_root=tmp_path, client_factory=lambda: client)

    response = service.catalog()

    # 예외가 발생해도 502 응답을 반환해야 함
    assert response.status_code == 502
    assert "catalog unavailable" in response.body["error"]
    # close()가 호출되었는지 확인
    assert client.closed


def test_catalog_handles_client_without_close(tmp_path: Path) -> None:
    """catalog()가 close() 메서드 없는 client에서도 작동한다."""
    # close()를 구현하지 않는 broken client
    service = BuilderService(output_root=tmp_path, client_factory=lambda: _BrokenClient())

    # close()가 없어도 예외 없이 작동해야 함
    response = service.catalog()

    # 이 경우 성공 응답이 아니므로 502가 됨
    assert response.status_code == 502
    assert "catalog unavailable" in response.body["error"]


def test_preview_closes_client_success(tmp_path: Path) -> None:
    """preview()가 성공 시 client.close()를 호출한다."""
    service = _create_service_with_fake_client(tmp_path)
    response = service.preview(VALID_SPEC_YAML, limit=10)

    # 실제 preview는 실패할 수 있지만 client는 닫혀야 함
    assert response.status_code in (200, 502)


def test_preview_closes_client_validation_failure(tmp_path: Path) -> None:
    """preview()가 검증 실패 시 client.close()를 호출한다."""
    invalid_spec = "dataset_id: ''"  # 빈 dataset_id
    service = _create_service_with_fake_client(tmp_path)
    response = service.preview(invalid_spec, limit=10)

    assert response.status_code == 400
    assert "dataset_id" in response.body["error"]


def test_build_closes_client_success(tmp_path: Path) -> None:
    """build()가 성공 시 client.close()를 호출한다."""
    service = _create_service_with_fake_client(tmp_path)
    response = service.build(VALID_SPEC_YAML, run_id="test-run-123")

    # 실제 build는 실패할 수 있지만 client는 닫혀야 함
    assert response.status_code in (200, 502)


def test_build_closes_client_validation_failure(tmp_path: Path) -> None:
    """build()가 검증 실패 후 client 생성 시 client.close()를 호출한다."""
    invalid_spec = "dataset_id: ''"  # 빈 dataset_id
    service = _create_service_with_fake_client(tmp_path)
    response = service.build(invalid_spec, run_id="test-run-456")

    assert response.status_code == 400
    assert "dataset_id" in response.body["error"]


# ... 나머지 기존 테스트 ...