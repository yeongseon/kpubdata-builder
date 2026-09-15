"""Tests for the agent pipeline modules."""

from __future__ import annotations

from pathlib import Path

import pytest

from kpubdata_builder.agent.discover import DiscoveryResult, ParamInfo
from kpubdata_builder.agent.monitor import MonitorState
from kpubdata_builder.cli import build_parser, dispatch


class TestDiscoveryResult:
    def test_to_spec_yaml_basic(self) -> None:
        result = DiscoveryResult(
            dataset_id="datago.test_api",
            title="Test API",
            base_url="http://apis.data.go.kr/1234/TestSvc",
            operation="getTestData",
            data_go_kr_url="https://www.data.go.kr/data/12345/openapi.do",
            params=[ParamInfo(name="region", required=True, description="지역코드")],
        )
        yaml_str = result.to_spec_yaml()
        assert "id: datago.test_api" in yaml_str
        assert "title: Test API" in yaml_str
        assert "base_url: http://apis.data.go.kr/1234/TestSvc" in yaml_str
        assert "operation: getTestData" in yaml_str
        assert "name: region" in yaml_str
        assert "required: true" in yaml_str
        assert "status: unstable" in yaml_str

    def test_to_spec_yaml_no_params(self) -> None:
        result = DiscoveryResult(
            dataset_id="datago.simple",
            title="Simple",
            base_url="http://example.com",
            operation="op",
        )
        yaml_str = result.to_spec_yaml()
        assert "params:" not in yaml_str


class TestMonitorState:
    def test_add_and_save_load(self, tmp_path: Path) -> None:
        state = MonitorState()
        state.add("datago.test1", status="NEEDS_APPLICATION")
        state.add("datago.test2", status="WAITING_APPROVAL")
        assert len(state.pending) == 2

        state_file = tmp_path / "monitor.yaml"
        state.save(state_file)
        assert state_file.is_file()

        loaded = MonitorState.load(state_file)
        assert len(loaded.pending) == 2
        assert loaded.pending[0].dataset_id == "datago.test1"
        assert loaded.pending[1].status == "WAITING_APPROVAL"

    def test_add_updates_existing(self) -> None:
        state = MonitorState()
        state.add("datago.test1", status="NEEDS_APPLICATION")
        state.add("datago.test1", status="HEALTHY")
        assert len(state.pending) == 1
        assert state.pending[0].status == "HEALTHY"

    def test_remove(self) -> None:
        state = MonitorState()
        state.add("datago.test1")
        removed = state.remove("datago.test1")
        assert removed is not None
        assert removed.dataset_id == "datago.test1"
        assert len(state.pending) == 0

    def test_remove_nonexistent(self) -> None:
        state = MonitorState()
        assert state.remove("datago.nope") is None

    def test_load_empty_file(self, tmp_path: Path) -> None:
        state_file = tmp_path / "empty.yaml"
        loaded = MonitorState.load(state_file)
        assert len(loaded.pending) == 0


class TestDiscoverCLI:
    def test_discover_parser(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["discover", "https://www.data.go.kr/data/12345/openapi.do"])
        assert args.command == "discover"
        assert "12345" in args.url

    def test_discover_with_output(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "discover",
                "https://example.com",
                "--output",
                "out.yaml",
                "--dataset-id",
                "datago.custom",
            ]
        )
        assert args.output == "out.yaml"
        assert args.dataset_id == "datago.custom"


class TestMonitorCLI:
    def test_monitor_parser(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["monitor", "--add", "datago.test"])
        assert args.command == "monitor"
        assert args.add == "datago.test"

    def test_monitor_add(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        state_file = str(tmp_path / "state.yaml")
        parser = build_parser()
        args = parser.parse_args(["monitor", "--state-file", state_file, "--add", "datago.xyz"])
        code = dispatch(args)
        assert code == 0
        out = capsys.readouterr().out
        assert "Added" in out

        loaded = MonitorState.load(Path(state_file))
        assert len(loaded.pending) == 1


class TestPipelineCLI:
    def test_pipeline_parser(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["pipeline", "datago.apt_trade", "--skip-pr"])
        assert args.command == "pipeline"
        assert args.dataset == "datago.apt_trade"
        assert args.skip_pr is True
