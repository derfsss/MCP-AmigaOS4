"""GDB Remote Serial Protocol client.

Talks to QEMU's `-gdb tcp::PORT` stub for whole-system debugging.
Used to read memory + CPU registers; can be extended with
breakpoint / step support.

Wire format (subset we implement):

  - Packets are `$<data>#<cs>` with cs = sum of data bytes mod 256
    in two-hex.
  - The receiver acks with `+` (good) or `-` (resend). We always send
    `+` after a good receive and treat lone `+` from the peer as ack.
  - We send `qSupported:...` once after connect, then exchange
    request/response packets.
  - `g` reads all general registers as one hex blob.
  - `m<addr>,<len>` reads `len` bytes of guest memory starting at
    `addr`, returned as hex.
  - `?` returns the stop reason (target-state probe).

Concurrency: one TCP connection, serialised via asyncio.Lock.
Reconnects on EOF.
"""

from __future__ import annotations

import asyncio

from ..config import GdbChannel
from ..errors import InternalError, TargetError

DEFAULT_TIMEOUT_S = 5.0


def _checksum(data: bytes) -> int:
    return sum(data) & 0xff


def _frame(payload: bytes) -> bytes:
    return b"$" + payload + b"#" + f"{_checksum(payload):02x}".encode("ascii")


class GdbTransport:
    """One connection to QEMU's GDB stub for a target."""

    def __init__(self, channel: GdbChannel) -> None:
        self._channel = channel
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    async def _ensure_connected(self) -> None:
        if self._reader is not None and self._writer is not None:
            return
        try:
            r, w = await asyncio.open_connection(
                self._channel.host, self._channel.port
            )
        except (ConnectionRefusedError, OSError) as e:
            raise TargetError(
                f"GDB stub not reachable at {self._channel.endpoint}",
                data={"endpoint": self._channel.endpoint, "error": str(e)},
            ) from e
        self._reader, self._writer = r, w
        # Hand-shake: announce supported features.
        await self._send(b"qSupported:swbreak+;hwbreak+")
        await self._recv()  # ignore reply contents

    async def close(self) -> None:
        async with self._lock:
            await self._close_locked()

    async def _close_locked(self) -> None:
        w = self._writer
        self._writer = None
        self._reader = None
        if w is not None:
            try:
                w.close()
                await w.wait_closed()
            except OSError:
                pass

    async def _send(self, payload: bytes,
                    timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        assert self._writer is not None
        assert self._reader is not None
        self._writer.write(_frame(payload))
        await self._writer.drain()
        # Expect a `+` (or `-` for retry) byte from the peer.
        ack = await asyncio.wait_for(self._reader.readexactly(1), timeout_s)
        if ack == b"-":
            # GDB asked to resend. One retry only.
            self._writer.write(_frame(payload))
            await self._writer.drain()
            ack = await asyncio.wait_for(self._reader.readexactly(1), timeout_s)
        if ack != b"+":
            raise InternalError(f"GDB stub returned bad ack: {ack!r}")

    async def _recv(self, timeout_s: float = DEFAULT_TIMEOUT_S) -> bytes:
        """Read one full $...#cs packet, ack it, return the inner payload."""
        assert self._reader is not None and self._writer is not None
        # Skip leading acks, retransmit notifications.
        while True:
            b = await asyncio.wait_for(self._reader.readexactly(1), timeout_s)
            if b == b"$":
                break
            if b in (b"+", b"-"):
                continue
            # Unknown leading byte; ignore.

        buf = bytearray()
        while True:
            b = await asyncio.wait_for(self._reader.readexactly(1), timeout_s)
            if b == b"#":
                break
            buf.extend(b)
            if len(buf) > 2 * 1024 * 1024:
                raise InternalError("GDB packet too large")
        # Two checksum bytes.
        cs_bytes = await asyncio.wait_for(self._reader.readexactly(2), timeout_s)
        cs_recv = int(cs_bytes.decode("ascii"), 16)
        cs_calc = _checksum(bytes(buf))
        if cs_recv != cs_calc:
            self._writer.write(b"-")
            await self._writer.drain()
            raise InternalError(
                f"GDB packet checksum mismatch: got {cs_recv:02x} "
                f"expected {cs_calc:02x}"
            )
        self._writer.write(b"+")
        await self._writer.drain()
        return bytes(buf)

    async def _exchange(self, payload: bytes,
                        timeout_s: float = DEFAULT_TIMEOUT_S) -> bytes:
        async with self._lock:
            for attempt in range(2):
                try:
                    await self._ensure_connected()
                    await self._send(payload, timeout_s)
                    return await self._recv(timeout_s)
                except (asyncio.IncompleteReadError, ConnectionResetError,
                        BrokenPipeError, OSError) as e:
                    await self._close_locked()
                    if attempt == 1:
                        raise TargetError(
                            f"GDB stub I/O failed: {e}",
                            data={"payload": payload[:80].decode(
                                "ascii", errors="replace")},
                        ) from e
                except TimeoutError as e:
                    await self._close_locked()
                    raise TargetError(
                        f"GDB stub request timed out after {timeout_s}s",
                        data={"payload": payload[:80].decode(
                            "ascii", errors="replace")},
                    ) from e
        raise InternalError("unreachable")

    # ---- public ----------------------------------------------------

    async def read_registers(self) -> bytes:
        """Send `g`, return raw register hex blob (decoded to bytes)."""
        resp = await self._exchange(b"g")
        if resp.startswith(b"E") and len(resp) <= 4:
            raise TargetError(
                f"GDB stub error reading registers: {resp.decode('ascii')}"
            )
        try:
            return bytes.fromhex(resp.decode("ascii"))
        except ValueError as e:
            raise InternalError(
                f"GDB stub returned non-hex register blob: {resp[:80]!r}"
            ) from e

    async def read_one_register(self, regnum: int) -> int:
        """Send `p<hex>` for one register. Returns the register value
        as an unsigned int (4 bytes -> uint32, 8 bytes -> uint64).

        Raises TargetError if the stub returns an error or an empty
        reply (means "p packet not supported").
        """
        req = f"p{regnum:x}".encode("ascii")
        resp = await self._exchange(req)
        if resp.startswith(b"E") and len(resp) <= 4:
            raise TargetError(
                f"GDB read register {regnum} failed: {resp.decode('ascii')}",
                data={"regnum": regnum},
            )
        if resp == b"":
            raise TargetError(
                "GDB stub doesn't support `p` packet",
                data={"regnum": regnum},
            )
        try:
            b = bytes.fromhex(resp.decode("ascii"))
        except ValueError as e:
            raise InternalError(
                f"non-hex register reply: {resp[:80]!r}"
            ) from e
        if len(b) == 4:
            import struct as _struct
            return int(_struct.unpack(">I", b)[0])
        if len(b) == 8:
            import struct as _struct
            return int(_struct.unpack(">Q", b)[0])
        raise TargetError(
            f"unexpected register width: {len(b)} bytes",
            data={"regnum": regnum, "bytes": len(b)},
        )

    async def cont(self, timeout_s: float = 60.0) -> str:
        """Send `c` (continue execution) and wait for the next stop
        notification.

        If the CPU doesn't halt within `timeout_s`, sends an
        out-of-band Ctrl-C (0x03) byte to interrupt the CPU and waits
        briefly for the resulting stop reply.
        """
        async with self._lock:
            await self._ensure_connected()
            assert self._writer is not None
            await self._send(b"c")
            try:
                resp = await asyncio.wait_for(
                    self._recv(timeout_s + 5.0), timeout_s,
                )
            except TimeoutError:
                # Ctrl-C is sent as a raw 0x03 byte, NOT as a packet.
                self._writer.write(b"\x03")
                await self._writer.drain()
                try:
                    resp = await asyncio.wait_for(self._recv(10.0), 10.0)
                except TimeoutError as e:
                    await self._close_locked()
                    raise TargetError(
                        "continue: stub didn't respond to Ctrl-C interrupt",
                    ) from e
        return resp.decode("ascii", errors="replace")

    async def read_memory(self, addr: int, length: int) -> bytes:
        if length <= 0:
            return b""
        if length > 0x10000:
            raise TargetError(
                "read_memory length capped at 64 KiB per request",
                data={"requested": length},
            )
        req = f"m{addr:x},{length:x}".encode("ascii")
        resp = await self._exchange(req)
        if resp.startswith(b"E") and len(resp) <= 4:
            raise TargetError(
                f"GDB stub error reading memory at 0x{addr:x}: "
                f"{resp.decode('ascii')}",
                data={"addr": addr, "length": length},
            )
        try:
            return bytes.fromhex(resp.decode("ascii"))
        except ValueError as e:
            raise InternalError(
                f"GDB stub returned non-hex memory blob: {resp[:80]!r}"
            ) from e

    async def stop_reason(self) -> str:
        """Send `?`, return the raw stop reply (e.g. T05thread:01;)."""
        resp = await self._exchange(b"?")
        return resp.decode("ascii", errors="replace")

    async def set_breakpoint(self, addr: int, kind: int = 4,
                             hardware: bool = False) -> str:
        """Set a software breakpoint (Z0) or hardware (Z1) at `addr`.

        `kind` is the architecture-specific BP "kind"; for PowerPC
        it's the breakpoint instruction size (typically 4). Returns
        the raw stub reply (`OK` on success, empty string if the
        stub doesn't support that flavour).
        """
        op = b"Z1" if hardware else b"Z0"
        req = op + f",{addr:x},{kind:x}".encode("ascii")
        resp = await self._exchange(req)
        if resp.startswith(b"E") and len(resp) <= 4:
            raise TargetError(
                f"GDB stub error setting breakpoint at 0x{addr:x}: "
                f"{resp.decode('ascii')}",
                data={"addr": addr, "kind": kind, "hardware": hardware},
            )
        return resp.decode("ascii", errors="replace")

    async def clear_breakpoint(self, addr: int, kind: int = 4,
                               hardware: bool = False) -> str:
        op = b"z1" if hardware else b"z0"
        req = op + f",{addr:x},{kind:x}".encode("ascii")
        resp = await self._exchange(req)
        if resp.startswith(b"E") and len(resp) <= 4:
            raise TargetError(
                f"GDB stub error clearing breakpoint at 0x{addr:x}: "
                f"{resp.decode('ascii')}",
                data={"addr": addr, "kind": kind, "hardware": hardware},
            )
        return resp.decode("ascii", errors="replace")

    async def step(self) -> str:
        """Single-step: `s`. Returns the stop reply (T<sig>...)
        synchronously - the stub executes one instruction and reports
        the new state immediately.
        """
        resp = await self._exchange(b"s", timeout_s=10.0)
        return resp.decode("ascii", errors="replace")

    async def detach(self) -> None:
        """Tell the GDB stub to detach so the CPU resumes normal
        execution. We close the TCP connection on our side too. After
        detach the next call to read_registers / read_memory will
        re-attach (and the stub will pause the CPU again on attach)."""
        async with self._lock:
            if self._reader is None or self._writer is None:
                return
            try:
                await self._send(b"D")
                # Best-effort read of the reply ("OK" usually).
                try:
                    await asyncio.wait_for(self._recv(), 1.0)
                except (TimeoutError, asyncio.IncompleteReadError, OSError):
                    pass
            except Exception:
                pass
            await self._close_locked()
