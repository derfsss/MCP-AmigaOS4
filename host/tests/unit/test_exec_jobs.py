"""exec.* as tracked jobs (#25, #24): what the host sends and accepts."""

from __future__ import annotations

import asyncio
from typing import Any
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


# ---- sandbox=True (phase F) -----------------------------------------

def _probe(abort: bool = False) -> Any:
    from amiga_fleet_mcp.tools.sandbox import SandboxProbeResult
    return SandboxProbeResult(target="q", available=True,
                              path="SYS:Tools/sandboxvm",
                              sandboxvm_version="1.0",
                              abort_supported=abort)


@pytest.mark.asyncio
async def test_sandbox_mode_wraps_command_and_collects_guest_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from amiga_fleet_mcp.tools import sandbox as sb

    async def _ok(_f: Any, _t: Any) -> Any:
        return _probe()

    monkeypatch.setattr(sb, "_ensure_available", _ok)
    fleet, fake = _fleet()
    fake.responses["exec.cmd"] = {"output": "", "exit_code": -768,
                                  "state": "exited", "job_id": 4}
    fake.responses["fs.read"] = {"content_b64": "aGk="}  # "hi"
    r = await exec_tool.exec_cmd(fleet, "q", "RAM:crashy", args=["x y"],
                                 sandbox=True)
    method, params = fake.calls[0]
    assert method == "exec.cmd" and params is not None
    assert params["sandboxed"] is True
    assert "args" not in params  # they go to the guest, after --
    cmd = str(params["command"])
    assert cmd.startswith('"SYS:Tools/sandboxvm"') and '"RAM:crashy"' in cmd
    assert cmd.endswith('-- "x y"')
    assert r.sandbox is not None
    assert r.sandbox.name.startswith("crashy-")
    assert r.sandbox.trap_kind == "DSI"
    assert r.sandbox.stdout == "hi"


@pytest.mark.asyncio
async def test_sandbox_job_still_running_reads_nothing_yet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from amiga_fleet_mcp.tools import sandbox as sb

    async def _ok(_f: Any, _t: Any) -> Any:
        return _probe()

    monkeypatch.setattr(sb, "_ensure_available", _ok)
    fleet, fake = _fleet()
    fake.responses["exec.cmd"] = {"output": "", "exit_code": None,
                                  "state": "running", "job_id": 5}
    r = await exec_tool.exec_cmd(fleet, "q", "RAM:slow", sandbox=True,
                                 return_running=True)
    assert r.sandbox is not None and len(r.sandbox.capture_paths) == 2
    assert [m for m, _ in fake.calls] == ["exec.cmd"]  # no reads/deletes


@pytest.mark.asyncio
async def test_force_kill_refused_until_sandboxvm_supports_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from amiga_fleet_mcp.errors import NotCapable
    from amiga_fleet_mcp.tools import sandbox as sb

    async def _no_abort(_f: Any, _t: Any) -> Any:
        return _probe(abort=False)

    monkeypatch.setattr(sb, "sandbox_probe", _no_abort)
    fleet, fake = _fleet()
    with pytest.raises(NotCapable, match="SandboxVM#1"):
        await exec_tool.exec_kill(fleet, "q", 7, force=True)
    assert fake.calls == []

    async def _abort(_f: Any, _t: Any) -> Any:
        return _probe(abort=True)

    monkeypatch.setattr(sb, "sandbox_probe", _abort)
    fake.responses["exec.kill"] = {"job_id": 7, "state": "exited",
                                   "signal": "f", "sent": True,
                                   "exited": True, "forced": True}
    k = await exec_tool.exec_kill(fleet, "q", 7, force=True)
    assert fake.calls[0][1]["force"] is True  # type: ignore[index]
    assert k.forced


def test_abort_support_follows_the_minimum_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from amiga_fleet_mcp.tools import sandbox as sb

    assert sb._parse_version("sandboxvm 1.0 (05.05.2026)") == (1, 0)
    assert sb._parse_version("no version here") is None
    # SandboxVM 1.1 added the CTRL-F abort and -T; 1.0 (and the 0.1.0
    # builds that carried no $VER) did not.
    assert sb.SANDBOXVM_ABORT_MIN_VERSION == (1, 1)
    assert not sb._abort_supported((1, 0))
    monkeypatch.setattr(sb, "SANDBOXVM_ABORT_MIN_VERSION", None)
    assert not sb._abort_supported((9, 9))
    monkeypatch.setattr(sb, "SANDBOXVM_ABORT_MIN_VERSION", (1, 1))
    assert sb._abort_supported((1, 1))
    assert not sb._abort_supported(None)


@pytest.mark.asyncio
async def test_sandbox_trap_taken_from_ring_only_for_this_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sandboxvm exits 50 when its guest aborts, so the trap has to come
    from the debug ring -- and only a report naming this job counts."""
    from amiga_fleet_mcp.tools import sandbox as sb

    fleet, fake = _fleet()
    fake.responses["fs.read"] = {"content_b64": ""}
    lines = ["[sandboxvm/trap] Traptype=0x00000300 ip=0x7FFC90C4 dar=0x0",
             "[sandboxvm] guest_destroy crashy-abc123 returned"]

    async def _trap(_f: Any, _t: Any) -> Any:
        return sb.LastTrapResult(target="q", found=True, trap_kind="DSI",
                                 raw_lines=lines)

    monkeypatch.setattr(sb, "sandbox_last_trap", _trap)
    mine = await sb.sandbox_job_result(fleet, "q", "crashy-abc123", 50)
    assert mine.trap_kind == "DSI"
    assert mine.trap_lines == [lines[0]]
    other = await sb.sandbox_job_result(fleet, "q", "crashy-ffffff", 50)
    assert other.trap_kind is None and other.trap_lines == []


@pytest.mark.asyncio
async def test_run_guest_reads_trap_kind_from_ring_on_50(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With SandboxVM 1.1 every trap exits 50, so run_guest has to find
    the kind in the debug ring (the agent-reported gap)."""
    from amiga_fleet_mcp.tools import sandbox as sb

    async def _ok(_f: Any, _t: Any) -> Any:
        return _probe()

    async def _trap(_f: Any, _t: Any) -> Any:
        return sb.LastTrapResult(target="q", found=True, trap_kind="ISI",
                                 raw_lines=["[sandboxvm] guest_end name=crashy "
                                            "reason=trap rc=-1024"])

    monkeypatch.setattr(sb, "_ensure_available", _ok)
    monkeypatch.setattr(sb, "sandbox_last_trap", _trap)
    fleet, fake = _fleet()
    fake.responses["exec.cmd"] = {"output": "", "exit_code": 50}
    fake.responses["fs.read"] = {"content_b64": ""}
    r = await sb.sandbox_run_guest(fleet, "q", guest="RAM:crashy", name="crashy")
    assert r.exit_code == 50 and r.trap_kind == "ISI"
