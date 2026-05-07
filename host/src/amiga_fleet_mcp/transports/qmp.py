"""QMP transport: wraps qemu-runner's QMPClient with async semantics.

The synchronous QMPClient runs inside asyncio.to_thread; calls are
serialised per target via an asyncio.Lock (QMP is request/reply
only, no streaming events that we consume).
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

from ..config import QmpChannel
from ..errors import InternalError, TargetError

_QMP_MODULE: ModuleType | None = None


def _load_qmp_client(qemu_runner_path: Path | None) -> ModuleType:
    global _QMP_MODULE
    if _QMP_MODULE is not None:
        return _QMP_MODULE
    if qemu_runner_path is None:
        raise InternalError(
            "QMP transport needs paths.qemu_runner in config.toml — "
            "see USAGE.md#helper-paths-paths or run "
            "`amiga-fleet-mcp --init`"
        )
    src = Path(qemu_runner_path) / "qmp_client.py"
    if not src.exists():
        raise InternalError(f"qmp_client.py not found at {src}")
    spec = importlib.util.spec_from_file_location(
        "_amiga_fleet_qemu_runner_qmp_client", src
    )
    if spec is None or spec.loader is None:
        raise InternalError(f"failed to load qmp_client.py at {src}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _QMP_MODULE = mod
    return mod


class QmpTransport:
    """Lazy-connected QMP wrapper for one target."""

    def __init__(
        self,
        channel: QmpChannel,
        qemu_runner_path: Path | None,
    ) -> None:
        self._channel = channel
        self._qemu_runner = qemu_runner_path
        self._client: Any = None
        self._lock = asyncio.Lock()

    async def _ensure_connected(self, timeout: float = 5.0) -> Any:
        if self._client is not None:
            return self._client
        mod = _load_qmp_client(self._qemu_runner)
        client = mod.QMPClient(host=self._channel.host, port=self._channel.port)
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
