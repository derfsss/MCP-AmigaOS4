"""QMP transport: wraps qemu-runner's QMPClient with async semantics.

The synchronous QMPClient runs inside asyncio.to_thread; calls are
serialised per target via an asyncio.Lock (QMP is request/reply
only, no streaming events that we consume).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from qmp_client import QMPClient  # from the qemu-runner package

from ..config import QmpChannel
from ..errors import TargetError


class QmpTransport:
    """Lazy-connected QMP wrapper for one target."""

    def __init__(self, channel: QmpChannel) -> None:
        self._channel = channel
        self._client: Any = None
        self._lock = asyncio.Lock()

    async def _ensure_connected(self, timeout: float = 5.0) -> Any:
        if self._client is not None:
            return self._client
        client = QMPClient(host=self._channel.host, port=self._channel.port)
        try:
            await asyncio.to_thread(client.connect, timeout)
        except (ConnectionRefusedError, OSError) as e:
            raise TargetError(
                f"QMP not reachable at {self._channel.endpoint}",
                data={"endpoint": self._channel.endpoint, "error": str(e)},
            ) from e
        self._client = client
        return client

    async def close(self) -> None:
        async with self._lock:
            if self._client is not None:
                await asyncio.to_thread(self._client.close)
                self._client = None

    async def query_status(self) -> dict[str, Any]:
        async with self._lock:
            c = await self._ensure_connected()
            r: dict[str, Any] = await asyncio.to_thread(c.status)
            return r

    async def system_reset(self) -> dict[str, Any]:
        async with self._lock:
            c = await self._ensure_connected()
            r: dict[str, Any] = await asyncio.to_thread(c.reset)
            return r

    async def quit(self) -> dict[str, Any]:
        async with self._lock:
            c = await self._ensure_connected()
            try:
                r: dict[str, Any] = await asyncio.to_thread(c.quit)
                return r
            finally:
                # Connection drops on quit.
                await asyncio.to_thread(c.close)
                self._client = None

    async def stop_cpu(self) -> dict[str, Any]:
        async with self._lock:
            c = await self._ensure_connected()
            r: dict[str, Any] = await asyncio.to_thread(c.stop)
            return r

    async def cont_cpu(self) -> dict[str, Any]:
        async with self._lock:
            c = await self._ensure_connected()
            r: dict[str, Any] = await asyncio.to_thread(c.cont)
            return r

    async def command(self, name: str, **args: Any) -> dict[str, Any]:
        async with self._lock:
            c = await self._ensure_connected()
            r: dict[str, Any] = await asyncio.to_thread(c.command, name, **args)
            return r

    async def screendump(self, host_path: Path) -> dict[str, Any]:
        """QMP screendump - QEMU writes a PNG to host_path itself.

        QEMU 6+ supports `format=png`. The path is interpreted on the
        QEMU host (us), not the guest.
        """
        return await self.command(
            "screendump", filename=str(host_path), format="png"
        )

    async def hmp(self, command: str) -> str:
        """Run an HMP (human-monitor) command and return its
        textual output.

        Some operations (savevm / loadvm / info snapshots) aren't
        exposed as QMP-native commands and have to go through HMP.
        """
        resp = await self.command("human-monitor-command", **{"command-line": command})
        out = resp.get("return", "")
        return out if isinstance(out, str) else ""
