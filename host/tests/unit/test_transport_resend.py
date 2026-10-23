"""The transport must never run a command twice on its own.

`request()` used to catch OSError before TimeoutError. TimeoutError has
been a subclass of OSError since Python 3.11, so a request that outlived
its timeout was treated as a dropped connection and sent again. An
exec.cmd longer than its timeout therefore ran twice (#22). The second
copy found its redirect file still held by the first and failed with
"unable to open redirection file", while the first kept running (#23).
Reproduced on qemu-pegasos2 before the fix: a script that appends one
line and waits 40 s left two lines.
"""

from __future__ import annotations

import asyncio
import json
import struct
import time
from typing import Any

import pytest

from amiga_fleet_mcp.config import McpdChannel
from amiga_fleet_mcp.errors import TargetError
from amiga_fleet_mcp.transports import mcpd as mcpd_mod
from amiga_fleet_mcp.transports.mcpd import McpdTransport


def _frame(rid: int, result: Any) -> bytes:
    body = json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}).encode()
    return struct.pack(">I", len(body)) + body


class _Conn:
    """One fake connection. `mode` decides what a read does:
    "reply" answers every request, "hang" never answers, "eof" closes
    after the request arrives. `dead` means the daemon already closed
    it (at_eof)."""

    def __init__(self, log: list[str], mode: str, dead: bool = False) -> None:
        self.log = log
        self.mode = mode
        self.dead = dead
        self._buf = b""

    # reader half
    async def readexactly(self, n: int) -> bytes:
        if self.mode == "hang":
            await asyncio.sleep(60)
        if self.mode == "eof" or len(self._buf) < n:
            raise asyncio.IncompleteReadError(b"", n)
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def at_eof(self) -> bool:
        return self.dead

    # writer half
    def write(self, data: bytes) -> None:
        env = json.loads(data[4:])
        self.log.append(env["method"])
        if self.mode == "reply":
            self._buf += _frame(env["id"], {"ok": True})

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


def _transport(
    monkeypatch: pytest.MonkeyPatch, modes: list[str], dead_first: bool = False,
) -> tuple[McpdTransport, list[str], list[_Conn]]:
    """A transport whose successive connections behave as `modes`."""
    log: list[str] = []
    conns: list[_Conn] = []
    t = McpdTransport(McpdChannel(endpoint="127.0.0.1:4322"))

    async def _connect() -> None:
        if t._reader is not None:
            return
        mode = modes[min(len(conns), len(modes) - 1)]
        c = _Conn(log, mode, dead=dead_first and not conns)
        conns.append(c)
        t._reader = c  # type: ignore[assignment]
        t._writer = c  # type: ignore[assignment]
        t._last_io_at = time.monotonic()

    async def _no_warmup() -> None:
        return None

    monkeypatch.setattr(t, "_ensure_connected", _connect)
    monkeypatch.setattr(t, "_warmup_locked", _no_warmup)
    return t, log, conns


@pytest.mark.asyncio
async def test_timeout_is_not_resent(monkeypatch: pytest.MonkeyPatch) -> None:
    t, log, _ = _transport(monkeypatch, ["hang", "reply"])
    with pytest.raises(TargetError, match="timed out") as ei:
        await t.request("exec.cmd", {"command": "Wait 60"}, timeout_s=0.05)
    assert log == ["exec.cmd"]
    assert ei.value.data["may_have_run"] is True


@pytest.mark.asyncio
async def test_timeout_on_a_read_is_not_resent_either(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t, log, _ = _transport(monkeypatch, ["hang", "reply"])
    with pytest.raises(TargetError, match="timed out"):
        await t.request("sys.uptime", timeout_s=0.05)
    assert log == ["sys.uptime"]


@pytest.mark.asyncio
async def test_lost_reply_to_a_command_is_not_resent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t, log, _ = _transport(monkeypatch, ["eof", "reply"])
    with pytest.raises(TargetError, match="I/O failed") as ei:
        await t.request("exec.cmd", {"command": "Echo x"})
    assert log == ["exec.cmd"]
    assert ei.value.data["may_have_run"] is True
    # The message names the failure even when the exception has none.
    assert str(ei.value).strip() != "MCPd I/O failed:"


@pytest.mark.asyncio
async def test_lost_reply_to_a_read_is_resent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t, log, _ = _transport(monkeypatch, ["eof", "reply"])
    assert await t.request("sys.uptime") == {"ok": True}
    assert log == ["sys.uptime", "sys.uptime"]


@pytest.mark.asyncio
async def test_failed_send_is_resent_for_any_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request that never left the host cannot have run, so trying
    again on a fresh connection is safe even for exec.cmd."""
    t, log, conns = _transport(monkeypatch, ["reply"])
    real_send = t._send_frame
    calls = 0

    async def _send_once_fails(payload: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionResetError("peer reset")
        await real_send(payload)

    monkeypatch.setattr(t, "_send_frame", _send_once_fails)
    assert await t.request("exec.cmd", {"command": "Echo x"}) == {"ok": True}
    assert log == ["exec.cmd"]
    assert len(conns) == 2


@pytest.mark.asyncio
async def test_dead_connection_is_replaced_before_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daemon drops idle connections. Writing a command into one it
    has already closed would end in EOF and an error, now that commands
    are not resent, so the transport has to notice first."""
    t, log, conns = _transport(monkeypatch, ["eof", "reply"], dead_first=True)
    await t._ensure_connected()
    assert await t.request("exec.cmd", {"command": "Echo x"}) == {"ok": True}
    assert len(conns) == 2
    assert log == ["exec.cmd"]


@pytest.mark.asyncio
async def test_idle_connection_is_replaced_before_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t, log, conns = _transport(monkeypatch, ["eof", "reply"])
    await t._ensure_connected()
    t._last_io_at = time.monotonic() - mcpd_mod.STALE_AFTER_S - 1
    assert await t.request("exec.cmd", {"command": "Echo x"}) == {"ok": True}
    assert len(conns) == 2
    assert log == ["exec.cmd"]


def test_every_idempotent_method_exists_and_none_mutates() -> None:
    assert not (mcpd_mod.IDEMPOTENT_METHODS & mcpd_mod.MUTATING_METHODS)
    for m in ("exec.cmd", "sys.cold_reboot", "sys.mcu_cmd", "sys.read_pa",
              "events.wait", "input.type", "debug.write_memory"):
        assert m not in mcpd_mod.IDEMPOTENT_METHODS


@pytest.mark.parametrize("failure", ["timeout", "eof"])
@pytest.mark.asyncio
async def test_gdb_step_is_never_resent(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    """The GDB transport had the same except ordering. Resending `s`
    would single-step twice."""
    from amiga_fleet_mcp.config import GdbChannel
    from amiga_fleet_mcp.transports.gdb import GdbTransport

    g = GdbTransport(GdbChannel(endpoint="127.0.0.1:1234"))
    sent: list[bytes] = []

    async def _noop(*_a: Any) -> None:
        return None

    async def _send(payload: bytes, _timeout_s: float) -> None:
        sent.append(payload)

    async def _recv(_timeout_s: float) -> bytes:
        if failure == "timeout":
            raise TimeoutError
        raise asyncio.IncompleteReadError(b"", 1)

    monkeypatch.setattr(g, "_ensure_connected", _noop)
    monkeypatch.setattr(g, "_close_locked", _noop)
    monkeypatch.setattr(g, "_send", _send)
    monkeypatch.setattr(g, "_recv", _recv)
    with pytest.raises(TargetError) as ei:
        await g._exchange(b"s")
    assert sent == [b"s"]
    assert ei.value.data["may_have_run"] is True
