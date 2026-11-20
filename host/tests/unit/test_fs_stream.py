"""fs.upload / fs.download over the raw stream (fs.stream_put /
fs.stream_get, MCPd 2.3+), against a fake daemon on a real local TCP
socket, so the framing, the raw bytes after a response, and a
connection dropped mid-stream are all exercised.

Verifies:
- transfer="auto" streams a large file both ways, byte for byte
- a stream broken part-way resumes on a new connection and completes
- a daemon error before the bytes flow is raised, not retried
- transfer="stream" against a daemon without the methods is NotCapable
- transfer="auto" against such a daemon falls back to the chunked path
- fs.stream_status: each stream's CRC-32 is checked; a corrupted
  stream is sent / fetched again
- a broken put whose last bytes the daemon writes late (and whose file
  it still holds) resumes from the settled size
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
import zlib
from pathlib import Path
from typing import Any

import pytest

from amiga_fleet_mcp.config import (
    Config,
    McpdChannel,
    TargetChannels,
    TargetConfig,
)
from amiga_fleet_mcp.errors import InvalidParams, NotCapable
from amiga_fleet_mcp.fleet import Fleet
from amiga_fleet_mcp.tools import fs as fs_tool


class StreamDaemon:
    """Just enough MCPd: proto.capabilities, fs.stat, fs.sync,
    fs.stream_put, fs.stream_get. `break_put_after` / `break_get_after`
    drop the connection once that many raw bytes have moved (once)."""

    def __init__(self, with_streams: bool = True, with_status: bool = False) -> None:
        self.files: dict[str, bytearray] = {}
        self.with_streams = with_streams
        self.with_status = with_status
        #: flip one stored (put) or sent (get) byte, once
        self.corrupt_put_once = False
        self.corrupt_get_once = False
        #: on a broken put: bytes the daemon still writes after the break,
        #: `late_after_s` later, and how long it refuses a resume meanwhile
        self.late_bytes = 0
        self.late_after_s = 0.0
        self.locked_until = 0.0
        self.break_put_after: int | None = None
        self.break_get_after: int | None = None
        self.calls: list[str] = []
        self.connections = 0
        self.server: asyncio.base_events.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    @staticmethod
    async def _frame(w: asyncio.StreamWriter, obj: dict[str, Any]) -> None:
        body = json.dumps(obj).encode()
        w.write(struct.pack(">I", len(body)) + body)
        await w.drain()

    def _err(self, rid: Any, code: int, msg: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": msg}}

    async def _serve(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        self.connections += 1
        last: dict[str, Any] | None = None
        try:
            while True:
                try:
                    hdr = await r.readexactly(4)
                except asyncio.IncompleteReadError:
                    return
                (n,) = struct.unpack(">I", hdr)
                req = json.loads(await r.readexactly(n))
                rid, method, p = req.get("id"), req["method"], req.get("params") or {}
                self.calls.append(method)
                if method == "proto.capabilities":
                    names = ["fs.stat", "fs.sync"]
                    if self.with_streams:
                        names += ["fs.stream_put", "fs.stream_get"]
                    if self.with_status:
                        names.append("fs.stream_status")
                    await self._frame(w, {"jsonrpc": "2.0", "id": rid,
                                          "result": {"method_names": names}})
                elif method == "fs.stat":
                    if p["path"] not in self.files:
                        await self._frame(w, self._err(rid, -32001, "Object not found"))
                    else:
                        await self._frame(w, {"jsonrpc": "2.0", "id": rid, "result": {
                            "name": p["path"], "type": "file",
                            "size": len(self.files[p["path"]])}})
                elif method == "fs.sync":
                    await self._frame(w, {"jsonrpc": "2.0", "id": rid,
                                          "result": {"path": p["path"], "flushed": True}})
                elif method == "fs.stream_status" and self.with_status:
                    if last is None:
                        await self._frame(w, self._err(rid, -32602, "no stream"))
                    else:
                        await self._frame(w, {"jsonrpc": "2.0", "id": rid, "result": last})
                elif method == "fs.stream_put" and self.with_streams:
                    off, length = int(p["offset"]), int(p["length"])
                    loop = asyncio.get_running_loop()
                    if off > 0 and loop.time() < self.locked_until:
                        await self._frame(w, self._err(rid, -32001,
                            "cannot open the file to resume"))
                        continue
                    if off == 0:
                        self.files[p["path"]] = bytearray()
                    elif len(self.files.get(p["path"], b"")) != off:
                        await self._frame(w, self._err(rid, -32602,
                            "a resumed put must start at the file's current size"))
                        continue
                    buf = self.files[p["path"]]
                    # An event frame written before the response must be
                    # skipped by the client.
                    await self._frame(w, {"jsonrpc": "2.0", "method": "events.notify",
                                          "params": {}})
                    await self._frame(w, {"jsonrpc": "2.0", "id": rid, "result": {
                        "path": p["path"], "offset": off, "length": length,
                        "ready": True}})
                    left = length
                    while left > 0:
                        chunk = await r.read(min(65536, left))
                        if not chunk:
                            return
                        if self.break_put_after is not None:
                            room = self.break_put_after - (len(buf) - off)
                            if room <= len(chunk):
                                buf.extend(chunk[:max(room, 0)])
                                late = chunk[max(room, 0):max(room, 0) + self.late_bytes]
                                if late:
                                    self.locked_until = loop.time() + self.late_after_s + 0.5
                                    loop.call_later(self.late_after_s, buf.extend, late)
                                self.break_put_after = None
                                w.transport.abort()
                                return
                        if self.corrupt_put_once:
                            chunk = bytes([chunk[0] ^ 0xFF]) + chunk[1:]
                            self.corrupt_put_once = False
                        buf.extend(chunk)
                        left -= len(chunk)
                    last = {"dir": "put", "offset": off, "bytes": length,
                            "complete": True, "crc32": zlib.crc32(bytes(buf[off:]))}
                elif method == "fs.stream_get" and self.with_streams:
                    data = bytes(self.files[p["path"]])
                    off = int(p.get("offset", 0))
                    out = data[off:]
                    last = {"dir": "get", "offset": off, "bytes": len(out),
                            "complete": True, "crc32": zlib.crc32(out)}
                    if self.corrupt_get_once and out:
                        out = bytes([out[0] ^ 0xFF]) + out[1:]
                        self.corrupt_get_once = False
                    await self._frame(w, {"jsonrpc": "2.0", "id": rid, "result": {
                        "path": p["path"], "offset": off, "length": len(out),
                        "total_size": len(data)}})
                    if self.break_get_after is not None:
                        w.write(out[:self.break_get_after])
                        await w.drain()
                        self.break_get_after = None
                        w.transport.abort()
                        return
                    w.write(out)
                    await w.drain()
                else:
                    await self._frame(w, self._err(rid, -32601, "Method not found"))
        finally:
            w.close()


@pytest.fixture
async def daemon() -> Any:
    d = StreamDaemon()
    await d.start()
    yield d
    await d.stop()


def _fleet(port: int) -> Fleet:
    return Fleet(Config(targets={
        "q": TargetConfig(
            type="remote",
            channels=TargetChannels(mcpd=McpdChannel(endpoint=f"127.0.0.1:{port}")),
        ),
    }))


@pytest.mark.asyncio
async def test_stream_round_trip(daemon: StreamDaemon, tmp_path: Path) -> None:
    fleet = _fleet(daemon.port)
    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(3 * 1024 * 1024 + 123))
    up = await fs_tool.fs_upload(fleet, "q", local_path=str(src), remote_path="DH0:big.bin")
    assert up.transfer == "stream"
    assert bytes(daemon.files["DH0:big.bin"]) == src.read_bytes()
    assert "fs.sync" in daemon.calls
    dst = tmp_path / "back.bin"
    down = await fs_tool.fs_download(fleet, "q", remote_path="DH0:big.bin", local_path=str(dst))
    assert down.transfer == "stream"
    assert dst.read_bytes() == src.read_bytes()
    await fleet.mcpd("q").close()


@pytest.mark.asyncio
async def test_broken_put_resumes(daemon: StreamDaemon, tmp_path: Path) -> None:
    fleet = _fleet(daemon.port)
    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(2 * 1024 * 1024))
    daemon.break_put_after = 700_000
    up = await fs_tool.fs_upload(fleet, "q", local_path=str(src), remote_path="DH0:x")
    assert up.chunks == 2  # one break, one resume
    assert bytes(daemon.files["DH0:x"]) == src.read_bytes()
    await fleet.mcpd("q").close()


@pytest.mark.asyncio
async def test_broken_get_resumes(daemon: StreamDaemon, tmp_path: Path) -> None:
    fleet = _fleet(daemon.port)
    data = os.urandom(2 * 1024 * 1024 + 7)
    daemon.files["DH0:y"] = bytearray(data)
    daemon.break_get_after = 900_001
    dst = tmp_path / "y.bin"
    down = await fs_tool.fs_download(fleet, "q", remote_path="DH0:y", local_path=str(dst))
    assert down.chunks == 2
    assert dst.read_bytes() == data
    await fleet.mcpd("q").close()


@pytest.mark.asyncio
async def test_daemon_error_before_stream_is_not_retried(
    daemon: StreamDaemon, tmp_path: Path,
) -> None:
    fleet = _fleet(daemon.port)
    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(2 * 1024 * 1024))
    daemon.files["DH0:z"] = bytearray(b"12345")
    from amiga_fleet_mcp.upload import stream_upload
    with pytest.raises(InvalidParams):
        await stream_upload(fleet, "q", src, "DH0:z", resume_from=100)
    assert daemon.calls.count("fs.stream_put") == 1
    await fleet.mcpd("q").close()


@pytest.mark.asyncio
async def test_old_daemon(tmp_path: Path) -> None:
    d = StreamDaemon(with_streams=False)
    await d.start()
    try:
        fleet = _fleet(d.port)
        src = tmp_path / "big.bin"
        src.write_bytes(os.urandom(2 * 1024 * 1024))
        with pytest.raises(NotCapable):
            await fs_tool.fs_upload(fleet, "q", local_path=str(src),
                                    remote_path="DH0:a", transfer="stream")
        assert await fs_tool._use_stream(fleet, "q", "auto", 10 * 1024 * 1024) is False
        await fleet.mcpd("q").close()
    finally:
        await d.stop()


@pytest.mark.asyncio
async def test_stream_crc_checked_and_corruption_redone(tmp_path: Path) -> None:
    d = StreamDaemon(with_status=True)
    await d.start()
    try:
        fleet = _fleet(d.port)
        src = tmp_path / "big.bin"
        src.write_bytes(os.urandom(2 * 1024 * 1024 + 5))
        up = await fs_tool.fs_upload(fleet, "q", local_path=str(src), remote_path="DH0:c")
        assert up.crc32_verified and up.chunks == 1
        d.corrupt_put_once = True
        up = await fs_tool.fs_upload(fleet, "q", local_path=str(src), remote_path="DH0:c")
        assert up.crc32_verified and up.chunks == 2
        assert bytes(d.files["DH0:c"]) == src.read_bytes()
        # A broken (then resumed) stream leaves bytes unchecked.
        d.break_put_after = 700_000
        up = await fs_tool.fs_upload(fleet, "q", local_path=str(src), remote_path="DH0:c")
        assert up.chunks == 2 and not up.crc32_verified
        assert bytes(d.files["DH0:c"]) == src.read_bytes()
        d.break_get_after = 900_001
        dst2 = tmp_path / "c2.bin"
        down = await fs_tool.fs_download(fleet, "q", remote_path="DH0:c", local_path=str(dst2))
        assert down.chunks == 2 and not down.crc32_verified
        assert dst2.read_bytes() == src.read_bytes()
        d.corrupt_get_once = True
        dst = tmp_path / "c.bin"
        down = await fs_tool.fs_download(fleet, "q", remote_path="DH0:c", local_path=str(dst))
        assert down.crc32_verified and down.chunks == 2
        assert dst.read_bytes() == src.read_bytes()
        await fleet.mcpd("q").close()
    finally:
        await d.stop()


@pytest.mark.asyncio
async def test_broken_put_with_late_writes_resumes(
    daemon: StreamDaemon, tmp_path: Path,
) -> None:
    """The daemon writes 300 KB more after the break and refuses a
    resume until it has: an fs.stat straight after the break reads a
    size that is about to change."""
    fleet = _fleet(daemon.port)
    src = tmp_path / "big.bin"
    src.write_bytes(os.urandom(3 * 1024 * 1024))
    daemon.break_put_after = 1_000_000
    daemon.late_bytes = 300_000
    daemon.late_after_s = 1.2
    try:
        up = await fs_tool.fs_upload(fleet, "q", local_path=str(src), remote_path="DH0:late")
        assert bytes(daemon.files["DH0:late"]) == src.read_bytes()
        assert not up.crc32_verified  # this daemon has no fs.stream_status
        assert up.chunks >= 2
    finally:
        await fleet.mcpd("q").close()
