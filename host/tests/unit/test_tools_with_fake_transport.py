"""Tool wiring against a fake MCPd transport (phase 4)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from amiga_fleet_mcp.config import (
    Config,
    McpdChannel,
    TargetChannels,
    TargetConfig,
)
from amiga_fleet_mcp.errors import NotCapable, TargetError
from amiga_fleet_mcp.fleet import Fleet
from amiga_fleet_mcp.tools import exec as exec_tool
from amiga_fleet_mcp.tools import fs as fs_tool
from amiga_fleet_mcp.tools import sys as sys_tool


class FakeMcpd:
    """Stub MCPd transport: programmable per-method responses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.responses: dict[str, Any] = {}
        self.errors: dict[str, TargetError] = {}

    async def request(self, method: str, params: dict | None = None,
                      timeout_s: float = 30.0) -> Any:
        self.calls.append((method, params))
        if method in self.errors:
            raise self.errors[method]
        if method in self.responses:
            return self.responses[method]
        return None


@pytest.fixture
def fleet_with_fake() -> tuple[Fleet, FakeMcpd]:
    cfg = Config(
        targets={
            "qemu-pegasos2": TargetConfig(
                type="qemu",
                channels=TargetChannels(
                    mcpd=McpdChannel(endpoint="127.0.0.1:4322"),
                ),
            )
        }
    )
    fleet = Fleet(cfg)
    fake = FakeMcpd()
    fleet._mcpd["qemu-pegasos2"] = fake  # type: ignore[assignment]
    return fleet, fake


@pytest.mark.asyncio
async def test_fs_list_passes_path(fleet_with_fake: Any) -> None:
    fleet, fake = fleet_with_fake
    fake.responses["fs.list"] = [
        {"name": "C", "type": "dir"},
        {"name": "Loader", "type": "file", "size": 1024},
    ]
    res = await fs_tool.fs_list(fleet, "qemu-pegasos2", "SYS:")
    assert fake.calls == [("fs.list", {"path": "SYS:"})]
    assert sorted((e.type, e.name) for e in res.entries) == [
        ("dir", "C"), ("file", "Loader")
    ]


@pytest.mark.asyncio
async def test_fs_stat(fleet_with_fake: Any) -> None:
    fleet, fake = fleet_with_fake
    fake.responses["fs.stat"] = {
        "name": "Loader", "type": "file", "size": 1024,
        "modified": "11-Apr-26 14:47:03",
    }
    st = await fs_tool.fs_stat(fleet, "qemu-pegasos2", "SYS:Loader")
    assert st.size == 1024
    assert st.type == "file"


@pytest.mark.asyncio
async def test_fs_read(fleet_with_fake: Any) -> None:
    import base64
    fleet, fake = fleet_with_fake
    fake.responses["fs.read"] = {
        "path": "RAM:hi.txt", "size": 12,
        "content_b64": base64.b64encode(b"hello\nworld\n").decode(),
    }
    r = await fs_tool.fs_read(fleet, "qemu-pegasos2", "RAM:hi.txt")
    assert r.size == 12
    assert base64.b64decode(r.content_b64) == b"hello\nworld\n"


@pytest.mark.asyncio
async def test_fs_write(fleet_with_fake: Any) -> None:
    import base64
    fleet, fake = fleet_with_fake
    fake.responses["fs.write"] = {"path": "RAM:test", "size": 8}
    payload = b"\x00\x01\x02hello"
    r = await fs_tool.fs_write(
        fleet, "qemu-pegasos2", "RAM:test", base64.b64encode(payload).decode()
    )
    assert fake.calls[0][0] == "fs.write"
    assert fake.calls[0][1] is not None
    assert fake.calls[0][1]["path"] == "RAM:test"
    assert r.size == 8


@pytest.mark.asyncio
async def test_fs_delete_target_error(fleet_with_fake: Any) -> None:
    fleet, fake = fleet_with_fake
    fake.errors["fs.delete"] = TargetError("Object not found",
                                            data={"path": "RAM:nope"})
    with pytest.raises(TargetError):
        await fs_tool.fs_delete(fleet, "qemu-pegasos2", "RAM:nope")


@pytest.mark.asyncio
async def test_fs_makedir(fleet_with_fake: Any) -> None:
    fleet, fake = fleet_with_fake
    fake.responses["fs.makedir"] = {"path": "RAM:newdir", "ok": True}
    r = await fs_tool.fs_makedir(fleet, "qemu-pegasos2", "RAM:newdir")
    assert r.path == "RAM:newdir"


@pytest.mark.asyncio
async def test_exec_cmd(fleet_with_fake: Any) -> None:
    fleet, fake = fleet_with_fake
    fake.responses["exec.cmd"] = {"output": "hi", "exit_code": 0,
                                  "truncated": False}
    r = await exec_tool.exec_cmd(fleet, "qemu-pegasos2", "echo hi")
    assert r.output == "hi"
    assert r.exit_code == 0


@pytest.mark.asyncio
async def test_sys_version(fleet_with_fake: Any) -> None:
    fleet, fake = fleet_with_fake
    fake.responses["sys.version"] = {
        "raw": "Kickstart 54.57, Workbench 53.21",
        "kickstart": "54.57", "workbench": "53.21",
    }
    r = await sys_tool.sys_version(fleet, "qemu-pegasos2")
    assert r.kickstart == "54.57"
    assert r.workbench == "53.21"


def test_no_mcpd_raises_notcapable(tmp_path: Path) -> None:
    cfg = Config(
        targets={
            "x5000-real": TargetConfig(
                type="remote",
                channels=TargetChannels(mcpd=None),
            )
        }
    )
    fleet = Fleet(cfg)
    with pytest.raises(NotCapable):
        fleet.mcpd("x5000-real")


def test_host_capabilities_advertises_methods(tmp_path: Path) -> None:
    cfg = Config(
        targets={
            "qemu-pegasos2": TargetConfig(
                type="qemu",
                channels=TargetChannels(
                    mcpd=McpdChannel(endpoint="127.0.0.1:4322"),
                ),
            )
        }
    )
    fleet = Fleet(cfg)
    caps = sys_tool.host_capabilities(
        fleet, "0.1.0", ["fs.list", "exec.cmd", "sys.version"],
    )
    assert "fs.list" in caps.methods
    assert "exec.cmd" in caps.methods
    assert "qemu-pegasos2" in caps.targets
