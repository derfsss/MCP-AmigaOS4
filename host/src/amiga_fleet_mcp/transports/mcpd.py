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
                env = {"jsonrpc": "2.0", "id": -100 - i,
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
        code on failure. Reconnects with a warmup on EOF / connection-
        reset before giving up. The warmup primes the daemon's
        dispatch path - the first request after a cold daemon boot
        otherwise drops occasionally.
        """
        # Bind `resp` outside the for-loop so the post-loop check below
        # cannot trip an UnboundLocalError under any control-flow path.
        resp: dict[str, Any] | None = None
        async with self._lock:
            for attempt in range(2):
                await self._ensure_connected()
                if attempt == 1:
                    # Reconnect after a failure - daemon may be in the
                    # cold-start race window; prime the dispatch path
                    # before retrying the real request.
                    await self._warmup_locked()
                rid = next(self._next_id)
                env: dict[str, Any] = {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "method": method,
                }
                if params is not None:
                    env["params"] = params

                payload = json.dumps(env, ensure_ascii=False).encode("utf-8")
                try:
                    await self._send_frame(payload)
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
                        # id mismatch - the response stream is now
                        # ambiguous. Drop the connection so the next
                        # request reconnects with a fresh id space.
                        await self._close_locked()
                        raise InternalError(
                            f"MCPd response id={obj.get('id')} != {rid}",
                        )
                    break
                except (
                    asyncio.IncompleteReadError,
                    ConnectionResetError,
                    BrokenPipeError,
                    OSError,
                ) as e:
                    await self._close_locked()
                    if attempt == 1:
                        raise TargetError(
                            f"MCPd I/O failed: {e}",
                            data={"method": method},
                        ) from e
                    continue
                except TimeoutError as e:
                    await self._close_locked()
                    raise TargetError(
                        f"MCPd request timed out after {timeout_s}s",
                        data={"method": method},
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
