"""exec.* as tracked jobs (#25, #24): what the host sends and accepts."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from amiga_fleet_mcp.config import (
    Config,
    DefaultsConfig,
    McpdChannel,
    TargetChannels,
    TargetConfig,
)
from amiga_fleet_mcp.fleet import Fleet
from amiga_fleet_mcp.server import build_server
from amiga_fleet_mcp.tools import exec as exec_tool

from .test_tools_with_fake_transport import FakeMcpd


def _fleet(exec_priority: int | None = None) -> tuple[Fleet, FakeMcpd]:
    cfg = Config(
        defaults=DefaultsConfig(exec_priority=exec_priority),
        targets={"q": TargetConfig(
            type="qemu",
            channels=TargetChannels(mcpd=McpdChannel(endpoint="127.0.0.1:4322")),
        )},
    )
    fleet = Fleet(cfg)
    fake = FakeMcpd()
    fleet._mcpd["q"] = fake  # type: ignore[assignment]
    return fleet, fake


@pytest.mark.asyncio
async def test_priority_left_to_the_daemon_by_default() -> None:
    fleet, fake = _fleet()
    fake.responses["exec.cmd"] = {"output": "", "exit_code": 0}
    await exec_tool.exec_cmd(fleet, "q", "Echo x")
    params = fake.calls[0][1]
    assert params is not None
    assert "priority" not in params
    assert "return_running" not in params


@pytest.mark.asyncio
async def test_config_default_then_call_override() -> None:
    fleet, fake = _fleet(exec_priority=-5)
    fake.responses["exec.cmd"] = {"output": "", "exit_code": 0}
    await exec_tool.exec_cmd(fleet, "q", "Echo x")
    await exec_tool.exec_cmd(fleet, "q", "Echo x", priority=-1)
    assert fake.calls[0][1]["priority"] == -5  # type: ignore[index]
    assert fake.calls[1][1]["priority"] == -1  # type: ignore[index]


@pytest.mark.asyncio
async def test_running_result_is_accepted() -> None:
    fleet, fake = _fleet()
    fake.responses["exec.cmd"] = {
        "output": "", "job_id": 9, "state": "running", "exit_code": None,
        "pid": 137, "priority": -2, "elapsed_ms": 1020,
        "truncated": False, "output_bytes": 0, "streamed": False,
    }
    r = await exec_tool.exec_cmd(fleet, "q", "Wait 60", return_running=True)
    assert fake.calls[0][1]["return_running"] is True  # type: ignore[index]
    assert r.state == "running"
    assert r.exit_code is None
    assert r.job_id == 9


@pytest.mark.asyncio
async def test_pre_2_0_daemon_result_still_parses() -> None:
    fleet, fake = _fleet()
    fake.responses["exec.cmd"] = {"output": "hi\n", "exit_code": 0,
                                  "truncated": True}
    r = await exec_tool.exec_cmd(fleet, "q", "Echo hi")
    assert r.output == "hi\n"
    assert r.state == "exited"
    assert r.job_id is None


@pytest.mark.asyncio
async def test_kill_sends_signal_and_wait() -> None:
    fleet, fake = _fleet()
    fake.responses["exec.kill"] = {
        "job_id": 3, "state": "exited", "exit_code": 5, "signal": "c",
        "sent": True, "exited": True,
    }
    r = await exec_tool.exec_kill(fleet, "q", 3, wait_s=1.5)
    assert fake.calls == [("exec.kill",
                           {"job_id": 3, "signal": "c", "wait_ms": 1500})]
    assert r.exited


def test_public_tools_registered() -> None:
    names = {t.name for t in asyncio.run(
        build_server(MagicMock(), MagicMock()).list_tools())}
    assert {"exec_cmd", "exec_status", "exec_output", "exec_kill",
            "exec_list", "exec"} <= names


def test_exec_priority_bounds() -> None:
    with pytest.raises(ValueError):
        DefaultsConfig(exec_priority=200)
