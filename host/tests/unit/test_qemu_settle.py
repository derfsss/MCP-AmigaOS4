"""qemu.stop flushes the guest whenever MCPd answers.

It used to flush only when *this* process had written to the target
(transport.last_write_at). Writes through any other connection -- a
deploy script, another session, a job on the guest -- were invisible to
it, so it reported `nothing-written` and killed QEMU with them still
in the guest's cache. Seen on 2026-10-23 after a scripted MCPd upload.
"""

from __future__ import annotations

from typing import Any

import pytest

from amiga_fleet_mcp.config import Config, McpdChannel, TargetChannels, TargetConfig
from amiga_fleet_mcp.errors import MethodNotFound
from amiga_fleet_mcp.fleet import Fleet
from amiga_fleet_mcp.tools import qemu as qemu_tool

from .test_tools_with_fake_transport import FakeMcpd


def _fleet() -> tuple[Fleet, FakeMcpd]:
    fleet = Fleet(Config(targets={"q": TargetConfig(
        type="qemu",
        channels=TargetChannels(mcpd=McpdChannel(endpoint="127.0.0.1:4322")),
    )}))
    fake = FakeMcpd()
    fake.last_write_at = None  # type: ignore[attr-defined]
    fleet._mcpd["q"] = fake  # type: ignore[assignment]
    return fleet, fake


@pytest.mark.asyncio
async def test_flushes_even_when_this_process_wrote_nothing() -> None:
    fleet, fake = _fleet()
    fake.responses["fs.sync"] = {"path": "SYS:", "flushed": True}
    assert await qemu_tool._settle_before_stop(fleet, "q", 10.0) == "fs.sync"
    assert fake.calls == [("fs.sync", {"path": "SYS:"})]


@pytest.mark.asyncio
async def test_old_daemon_and_no_known_writes_does_not_wait() -> None:
    fleet, fake = _fleet()
    fake.errors["fs.sync"] = MethodNotFound("no fs.sync")  # type: ignore[assignment]
    assert (await qemu_tool._settle_before_stop(fleet, "q", 10.0)
            == "nothing-written")


@pytest.mark.asyncio
async def test_unreachable_daemon_is_not_waited_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fleet = Fleet(Config(targets={"q": TargetConfig(
        type="qemu",
        channels=TargetChannels(mcpd=McpdChannel(endpoint="127.0.0.1:4322")),
    )}))

    def _down(*_a: Any, **_k: Any) -> bool:
        return False

    monkeypatch.setattr(qemu_tool, "_can_connect", _down)
    assert (await qemu_tool._settle_before_stop(fleet, "q", 10.0)
            == "nothing-written")
    assert fleet.mcpd_if_connected("q") is None
