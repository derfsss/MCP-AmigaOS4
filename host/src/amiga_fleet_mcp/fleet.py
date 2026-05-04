"""Target registry + connection pool.

In-Amiga method dispatch goes through the MCPd transport; the QMP
transport handles QEMU host-side lifecycle (savevm/loadvm/reset/...).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from .config import Config, TargetConfig
from .errors import InvalidParams, NotCapable
from .transports.gdb import GdbTransport
from .transports.mcpd import McpdTransport
from .transports.qmp import QmpTransport
from .transports.serial_capture import SerialCaptureRegistry


class Fleet:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._mcpd: dict[str, McpdTransport] = {}
        self._qmp: dict[str, QmpTransport] = {}
        self._gdb: dict[str, GdbTransport] = {}
        self._qemu_processes: dict[str, subprocess.Popen[bytes]] = {}
        self._serial_log_paths: dict[str, Path] = {}
        self._serial_captures = SerialCaptureRegistry(
            log_root=Path(config.server.log_dir)
        )

    @property
    def serial_captures(self) -> SerialCaptureRegistry:
        return self._serial_captures

    @property
    def config(self) -> Config:
        return self._config

    def list_targets(self, tags: list[str] | None = None) -> list[str]:
        """All target names, optionally filtered to those whose
        `tags` set is a superset of every entry in `tags` (AND-match,
        not OR-match). `[]` and None both return all targets.
        """
        names = sorted(self._config.targets.keys())
        if not tags:
            return names
        wanted = set(tags)
        return [n for n in names
                if wanted.issubset(set(self._config.targets[n].tags))]

    def target_config(self, name: str) -> TargetConfig:
        if name not in self._config.targets:
            raise InvalidParams(
                f"unknown target: {name!r}",
                data={"available": self.list_targets()},
            )
        return self._config.targets[name]

    def resolve_target(self, name: str | None) -> str:
        """Resolve `name` against the config's `default_target`.

        Pass `None` or "" to fall back to `[default_target]` from
        config.toml. Lets callers skip the `target=...` arg in
        single-target setups. Raises InvalidParams when nothing
        resolves or the resolved target isn't configured.
        """
        try:
            return self._config.resolve_target(name)
        except ValueError as e:
            raise InvalidParams(str(e),
                                data={"available": self.list_targets()}) from e

    def mcpd(self, name: str) -> McpdTransport:
        """Return (lazy-connected) MCPd transport for `name`.

        Raises NotCapable if the target has no MCPd channel
        configured.
        """
        if name in self._mcpd:
            return self._mcpd[name]
        target = self.target_config(name)
        ch = target.channels.mcpd
        if ch is None or not ch.enabled:
            raise NotCapable(
                "target has no enabled MCPd channel",
                data={"target": name},
            )
        t = McpdTransport(ch)
        self._mcpd[name] = t
        return t

    def qmp(self, name: str) -> QmpTransport:
        """Return (lazy-connected) QMP transport for a QEMU target."""
        if name in self._qmp:
            return self._qmp[name]
        target = self.target_config(name)
        ch = target.channels.qmp
        if ch is None or not ch.enabled:
            raise NotCapable(
                "target has no enabled QMP channel",
                data={"target": name},
            )
        if target.type != "qemu":
            raise NotCapable(
                "QMP only applies to qemu targets",
                data={"target": name, "type": target.type},
            )
        t = QmpTransport(ch, self._config.paths.qemu_runner)
        self._qmp[name] = t
        return t

    def gdb(self, name: str) -> GdbTransport:
        if name in self._gdb:
            return self._gdb[name]
        target = self.target_config(name)
        ch = target.channels.gdb
        if ch is None or not ch.enabled:
            raise NotCapable(
                "target has no enabled GDB stub channel",
                data={"target": name},
            )
        t = GdbTransport(ch)
        self._gdb[name] = t
        return t

    def register_qemu_process(
        self, name: str, proc: subprocess.Popen[bytes],
        *, serial_log_path: Path | None = None,
    ) -> None:
        self._qemu_processes[name] = proc
        if serial_log_path is not None:
            self._serial_log_paths[name] = serial_log_path

    def qemu_process(self, name: str) -> subprocess.Popen[bytes] | None:
        return self._qemu_processes.get(name)

    def serial_log_path(self, name: str) -> Path | None:
        return self._serial_log_paths.get(name)

    def drop_qemu_process(self, name: str) -> None:
        self._qemu_processes.pop(name, None)
        if name in self._mcpd:
            del self._mcpd[name]
        if name in self._qmp:
            del self._qmp[name]
        if name in self._gdb:
            del self._gdb[name]

    async def close_all(self) -> None:
        for t in list(self._mcpd.values()):
            await t.close()
        self._mcpd.clear()
        for q in list(self._qmp.values()):
            await q.close()
        self._qmp.clear()
        for g in list(self._gdb.values()):
            await g.close()
        self._gdb.clear()
        # Terminate any QEMU children launched via qemu.start so the
        # host process doesn't leave orphans behind. Try a polite
        # terminate first; if the child doesn't exit promptly escalate
        # to kill(). subprocess.terminate() and wait() are blocking
        # syscalls; route them through asyncio.to_thread so close_all
        # doesn't stall the event loop on a hung child.
        for name, proc in list(self._qemu_processes.items()):
            if proc.poll() is not None:
                continue   # already exited
            try:
                await asyncio.to_thread(proc.terminate)
            except OSError:
                pass
            try:
                await asyncio.to_thread(proc.wait, 5)
            except subprocess.TimeoutExpired:
                try:
                    await asyncio.to_thread(proc.kill)
                    await asyncio.to_thread(proc.wait, 2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            self._qemu_processes.pop(name, None)
