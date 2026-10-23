"""MCPd transport: JSON-RPC 2.0 over framed TCP.

The transport for in-Amiga method dispatch.

Wire format (matches mcpd/src/frame.c on the daemon side):
    uint32_be length | <length> bytes UTF-8 JSON-RPC

One TCP connection per target, lazy-initiated, serialised with an
asyncio.Lock (we don't multiplex requests over a single connection
since the daemon spawns a fresh handler task per connection). The
transport silently reconnects on EOF.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import struct
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import McpdChannel
from ..errors import (
    AuthRequired,
    Busy,
    Cancelled,
    InternalError,
    InvalidParams,
    JsonRpcError,
    MethodNotFound,
    NotCapable,
    ParseError,
    TargetError,
)

log = logging.getLogger(__name__)

_ERROR_BY_CODE: dict[int, type[JsonRpcError]] = {
    -32700: ParseError,
    -32600: InvalidParams,  # invalid request
    -32601: MethodNotFound,
    -32602: InvalidParams,
    -32603: InternalError,
    -32001: TargetError,
    -32002: Cancelled,
    -32003: NotCapable,
    -32004: AuthRequired,
    -32005: Busy,
}

DEFAULT_TIMEOUT_S = 30.0
MAX_FRAME_BYTES = 32 * 1024 * 1024


#: Ids used by the cold-start warmup probes. Negative so they can
#: never collide with the positive ids `request()` allocates.
WARMUP_ID_BASE = -100


def _is_warmup_id(rid: Any) -> bool:
    return isinstance(rid, int) and rid <= WARMUP_ID_BASE


#: Methods that can leave the target's filesystem dirty. AmigaOS
#: writes back lazily, so a guest killed shortly after one of these
#: can lose the write entirely -- the point is not to be exhaustive
#: about which of them touched a disk, but to know that *something*
#: might still be in flight.
MUTATING_METHODS = frozenset({
    "fs.write", "fs.write_chunk", "fs.delete", "fs.makedir",
    "fs.rename", "fs.protect", "fs.copy",
    "exec.cmd",
})

#: Methods that are safe to send a second time when the first attempt
#: may already have reached the daemon: they only read, or (fs.sync)
#: repeating them changes nothing. Anything not listed here is never
#: resent once its request has gone out -- resending exec.cmd after a
#: timeout ran the command twice (#22, #23). sys.read_pa is left out
#: on purpose: a bad address faults the handler, and resending it
#: just faults another one.
IDEMPOTENT_METHODS = frozenset({
    "proto.capabilities", "proto.version",
    "sys.version", "sys.tasks", "sys.libraries", "sys.devices",
    "sys.modules", "sys.crashhook_status", "sys.ports", "sys.lastalert",
    "sys.uptime", "sys.memory", "sys.volumes", "sys.assigns",
    "sys.hardware", "sys.hardware.perfcounters",
    "sys.executable.symbols", "sys.applications", "sys.alert_decode",
    "sys.read_ccsr", "sys.tlb_dump",
    "fs.list", "fs.stat", "fs.read", "fs.hash", "fs.sync",
    "wb.screens", "wb.windows", "wb.publicscreens", "wb.frontmost",
    "wb.screenshot",
    "input.state",
    "debug.task_snapshot", "debug.symbol", "debug.stacktrace",
})

#: The daemon drops a connection that has sent nothing for
#: MCPD_RECV_TIMEOUT_S (120 s, mcpd/src/main.c). A request written into
#: a connection it has already dropped fails with EOF and was never
#: run, but from here that looks the same as one that ran and lost its
#: reply. So reconnect before sending once a connection has been idle
#: this long, rather than find out afterwards.
STALE_AFTER_S = 100.0


def _describe(e: BaseException) -> str:
    """str(e), or the exception's type when it has no message."""
    return str(e) or type(e).__name__


class McpdTransport:
    """One TCP connection to MCPd on a target."""

    def __init__(self, channel: McpdChannel) -> None:
        self._channel = channel
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._next_id = itertools.count(1)
        self._notification_handlers: list[
            Callable[[dict[str, Any]], Awaitable[None] | None]
        ] = []
        self._notification_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        #: Monotonic timestamp of the last call that changed something
        #: on the target's disk, or None if this connection has only
        #: ever read. `qemu.stop` uses it to decide whether the guest
        #: needs time to write back before the process is killed --
        #: see MUTATING_METHODS.
        self.last_write_at: float | None = None
        #: Monotonic time of the last connect or completed exchange, for
        #: the STALE_AFTER_S check. None while not connected.
        self._last_io_at: float | None = None

    def subscribe_notifications(
        self,
        handler: Callable[[dict[str, Any]], Awaitable[None] | None],
    ) -> Callable[[], None]:
        """Register a handler called whenever the daemon emits a
        JSON-RPC notification (server-push event). Returns an
        unsubscribe callable."""
        self._notification_handlers.append(handler)

        def _unsub() -> None:
            try:
                self._notification_handlers.remove(handler)
            except ValueError:
                pass

        return _unsub

    async def get_notification(
        self, timeout_s: float = 30.0,
    ) -> dict[str, Any] | None:
        """Pop one queued notification. Returns None on timeout.

        The queue fills as a side-effect of any `request()` that
        encountered a notification frame between request-send and
        response-receive. Useful for tests; production code should
        prefer subscribe_notifications()."""
        try:
            return await asyncio.wait_for(
                self._notification_q.get(), timeout_s,
            )
        except TimeoutError:
            return None

    @property
    def endpoint(self) -> str:
        return self._channel.endpoint

    async def _ensure_connected(self) -> None:
        if self._reader is not None and self._writer is not None:
            return
        try:
            r, w = await asyncio.open_connection(self._channel.host, self._channel.port)
        except (ConnectionRefusedError, OSError) as e:
            raise TargetError(
                f"MCPd not reachable at {self._channel.endpoint}",
                data={"endpoint": self._channel.endpoint, "error": str(e)},
            ) from e
        self._reader, self._writer = r, w
        self._last_io_at = time.monotonic()

    def _is_stale(self) -> bool:
        """True when the open connection is known or likely to be dead:
        the daemon closed it (EOF already seen), or it has been idle
        long enough that the daemon will have."""
        if self._reader is None:
            return False
        if self._reader.at_eof():
            return True
        return (
            self._last_io_at is not None
            and time.monotonic() - self._last_io_at > STALE_AFTER_S
        )

    async def close(self) -> None:
        async with self._lock:
            await self._close_locked()

    async def _close_locked(self) -> None:
        w = self._writer
        self._writer = None
        self._reader = None
        self._last_io_at = None
        if w is not None:
            try:
                w.close()
                await w.wait_closed()
            except OSError:
                pass

    async def _read_frame(self, timeout_s: float) -> bytes:
        assert self._reader is not None
        hdr = await asyncio.wait_for(self._reader.readexactly(4), timeout_s)
        (n,) = struct.unpack(">I", hdr)
        if n == 0 or n > MAX_FRAME_BYTES:
            raise InternalError(f"bad frame length: {n}")
        body = await asyncio.wait_for(self._reader.readexactly(n), timeout_s)
        return body

    async def _send_frame(self, payload: bytes) -> None:
        assert self._writer is not None
        if len(payload) > MAX_FRAME_BYTES:
            raise InternalError(f"frame too large: {len(payload)}")
        self._writer.write(struct.pack(">I", len(payload)) + payload)
        await self._writer.drain()

    async def _warmup_locked(self) -> None:
        """Cold-start dispatch primer.

        On a freshly-booted MCPd the TCP listener accepts connections
        before its dispatch path is fully ready, so the first 1-2
        RPCs occasionally drop silently. Fire a couple of cheap
        sys.uptime probes; ignore responses + errors. Caller holds
        `_lock` so this is safe to interleave with the real request
        build below.
        """
        for i in range(2):
            try:
                env = {"jsonrpc": "2.0", "id": WARMUP_ID_BASE - i,
                       "method": "sys.uptime"}
                payload = json.dumps(env).encode("utf-8")
                await self._send_frame(payload)
                # Drain whatever comes back (response or notification),
                # bounded; don't care about correctness here.
                try:
                    await self._read_frame(timeout_s=2.0)
                except (asyncio.IncompleteReadError, TimeoutError,
                        ConnectionResetError, BrokenPipeError, OSError):
                    pass
            except (asyncio.IncompleteReadError, ConnectionResetError,
                    BrokenPipeError, OSError):
                # Send failed - reopen socket and try again
                await self._close_locked()
                try:
                    await self._ensure_connected()
                except TargetError:
                    return  # daemon really not reachable; let caller fail
            await asyncio.sleep(0.1)

    async def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> Any:
        """Send one JSON-RPC request and return its result.

        Raises a JsonRpcError subclass mirroring the daemon's error
        code on failure. A connection that is already dead or has
        been idle past STALE_AFTER_S is replaced before sending.

        On EOF / connection reset the request is sent once more (after
        a warmup that primes a cold daemon's dispatch path), but only
        when that cannot run it twice: the send itself failed, or the
        method is in IDEMPOTENT_METHODS. A timeout is never retried.
        Every transport failure after the request went out carries
        ``data["may_have_run"] = True``: the daemon may have acted on
        it, so the caller must check before repeating it.
        """
        # Bind `resp` outside the for-loop so the post-loop check below
        # cannot trip an UnboundLocalError under any control-flow path.
        resp: dict[str, Any] | None = None
        async with self._lock:
            for attempt in range(2):
                if self._is_stale():
                    await self._close_locked()
                await self._ensure_connected()
                if attempt == 1:
                    # Reconnect after a failure - daemon may be in the
                    # cold-start race window; prime the dispatch path
                    # before retrying the real request.
                    await self._warmup_locked()
                if method in MUTATING_METHODS:
                    self.last_write_at = time.monotonic()
                rid = next(self._next_id)
                env: dict[str, Any] = {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "method": method,
                }
                if params is not None:
                    env["params"] = params

                payload = json.dumps(env, ensure_ascii=False).encode("utf-8")
                sent = False
                try:
                    await self._send_frame(payload)
                    sent = True
                    # Read frames until we see one with our id. Frames
                    # that look like JSON-RPC notifications (no `id`)
                    # are dispatched to subscribers + queue. Server-push
                    # event frames may arrive interleaved with the
                    # response.
                    while resp is None:
                        body = await self._read_frame(timeout_s)
                        try:
                            obj = json.loads(body.decode("utf-8"))
                        except (UnicodeDecodeError, json.JSONDecodeError) as e:
                            raise InternalError(
                                f"MCPd returned non-JSON: {e}",
                            ) from e
                        if "id" in obj and obj["id"] == rid:
                            resp = obj
                            break
                        if "id" not in obj:
                            await self._dispatch_notification(obj)
                            continue
                        if _is_warmup_id(obj.get("id")):
                            # A late reply to a cold-start warmup probe.
                            # The warmup reads with a short timeout, so
                            # on a slow daemon its answer can arrive
                            # after we moved on -- and then it is the
                            # first thing in the stream for the next
                            # real request. Discarding it is right;
                            # treating it as a desync used to drop the
                            # connection and surface as a spurious
                            # "response id=-100 != 5" on the first call
                            # after a guest restart.
                            continue
                        # id mismatch - the response stream is now
                        # ambiguous. Drop the connection so the next
                        # request reconnects with a fresh id space.
                        await self._close_locked()
                        raise InternalError(
                            f"MCPd response id={obj.get('id')} != {rid}",
                        )
                    self._last_io_at = time.monotonic()
                    break
                # TimeoutError has been a subclass of OSError since
                # Python 3.11, so it must be caught first: listed after
                # OSError it was never reached, and a slow exec.cmd was
                # treated as a dropped connection and sent again.
                except TimeoutError as e:
                    await self._close_locked()
                    raise TargetError(
                        f"MCPd request timed out after {timeout_s}s",
                        data={"method": method, "may_have_run": sent},
                    ) from e
                except (
                    asyncio.IncompleteReadError,
                    ConnectionResetError,
                    BrokenPipeError,
                    OSError,
                ) as e:
                    await self._close_locked()
                    if attempt == 0 and (
                        not sent or method in IDEMPOTENT_METHODS
                    ):
                        continue
                    raise TargetError(
                        f"MCPd I/O failed: {_describe(e)}",
                        data={"method": method, "may_have_run": sent},
                    ) from e

        if resp is None:
            # Unreachable in practice (the for/try/except chain either
            # binds resp or raises), but using assert here would be
            # stripped by `python -O`. Raise an explicit error instead.
            raise InternalError(
                "MCPd request loop completed without a response",
                data={"method": method},
            )
        if "error" in resp:
            err = resp["error"]
            cls = _ERROR_BY_CODE.get(int(err.get("code", -32603)), JsonRpcError)
            raise cls(err.get("message", "MCPd error"),
                      data=err.get("data"))
        if "result" not in resp:
            raise InternalError("MCPd response had neither result nor error")
        return resp["result"]

    async def _dispatch_notification(self, obj: dict[str, Any]) -> None:
        """A frame with no `id` arrived. Push to the queue and call
        any registered handlers. Handler errors are logged but never
        re-raised: a buggy subscriber must not break the underlying
        request/response stream."""
        await self._notification_q.put(obj)
        for h in list(self._notification_handlers):
            try:
                r = h(obj)
                if asyncio.iscoroutine(r):
                    await r
            except Exception:
                log.exception(
                    "notification handler raised; continuing",
                )
