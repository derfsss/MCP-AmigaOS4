"""A late warmup reply must not poison the next request.

`_warmup_locked` fires `sys.uptime` probes with negative ids to prime
a freshly-booted daemon's dispatch path, and reads each answer with a
2 s timeout. On a daemon slow enough to need the warmup in the first
place, that answer can arrive *after* the timeout -- at which point it
is sitting in the socket, and the next real request reads it before
its own response.

The old behaviour was to treat any unexpected id as a desynchronised
stream, drop the connection, and raise. So the mechanism that exists
to make cold starts reliable could itself fail the first call after
one, with an error that reads like a protocol fault
("MCPd response id=-100 != 5") and reproduces only under timing.
"""

from __future__ import annotations

import json
import struct

import pytest

from amiga_fleet_mcp.config import McpdChannel
from amiga_fleet_mcp.transports import mcpd as mcpd_mod
from amiga_fleet_mcp.transports.mcpd import McpdTransport, _is_warmup_id


def test_warmup_ids_are_recognised() -> None:
    assert _is_warmup_id(-100)
    assert _is_warmup_id(-101)
    # Real request ids are positive and must never be mistaken for one.
    assert not _is_warmup_id(1)
    assert not _is_warmup_id(5)
    assert not _is_warmup_id(None)
    assert not _is_warmup_id("-100")


class _FakeStream:
    """Feeds pre-canned frames to the transport's reader."""

    def __init__(self, frames: list[bytes]) -> None:
        self._buf = b"".join(
            struct.pack(">I", len(f)) + f for f in frames
        )
        self.sent: list[bytes] = []

    async def readexactly(self, n: int) -> bytes:
        if len(self._buf) < n:
            raise AssertionError("fake stream exhausted")
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    # writer half
    def write(self, data: bytes) -> None:
        self.sent.append(data)

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass


@pytest.mark.asyncio
async def test_stale_warmup_reply_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A leftover id=-100 answer is discarded and the real one is
    returned, rather than killing the connection."""
    stale = json.dumps(
        {"jsonrpc": "2.0", "id": -100, "result": {"seconds": 1.0}}
    ).encode()
    real = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
    ).encode()
    stream = _FakeStream([stale, real])

    t = McpdTransport(McpdChannel(endpoint="127.0.0.1:4322"))
    t._reader = stream       # type: ignore[assignment]
    t._writer = stream       # type: ignore[assignment]

    async def _already_connected() -> None:
        return None

    monkeypatch.setattr(t, "_ensure_connected", _already_connected)

    assert await t.request("sys.uptime") == {"ok": True}


@pytest.mark.asyncio
async def test_genuinely_wrong_id_still_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A positive id that isn't ours is a real desync and must still
    be treated as one -- the fix narrows the tolerance, it does not
    remove it."""
    from amiga_fleet_mcp.errors import InternalError

    wrong = json.dumps(
        {"jsonrpc": "2.0", "id": 999, "result": {"ok": True}}
    ).encode()
    stream = _FakeStream([wrong, wrong])

    t = McpdTransport(McpdChannel(endpoint="127.0.0.1:4322"))
    t._reader = stream       # type: ignore[assignment]
    t._writer = stream       # type: ignore[assignment]

    async def _already_connected() -> None:
        return None

    monkeypatch.setattr(t, "_ensure_connected", _already_connected)
    monkeypatch.setattr(mcpd_mod.McpdTransport, "_warmup_locked",
                        _already_connected)

    with pytest.raises(InternalError, match="id=999"):
        await t.request("sys.uptime")
