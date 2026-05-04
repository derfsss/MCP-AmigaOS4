"""QEMU lifecycle tools (phase 2): start / stop / reset / status / screenshot."""

from __future__ import annotations

import asyncio
import base64
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from ..errors import InternalError, InvalidParams, TargetError
from ..fleet import Fleet
from ..qemu import build_cmdline


class QemuStartResult(BaseModel):
    target: str
    pid: int
    ports: dict[str, int | None]
    cmdline: list[str]
    serial_log: str | None = None  # path to captured -serial output


class QemuStopResult(BaseModel):
    target: str
    method: Literal["qmp", "terminate", "kill", "already-stopped"]
    exit_code: int | None = None


class QemuStatusResult(BaseModel):
    target: str
    running: bool
    pid: int | None = None
    qmp_status: dict[str, Any] | None = None
    mcpd_reachable: bool | None = None


class QemuResetResult(BaseModel):
    target: str
    response: dict[str, Any]


class QemuScreenshotResult(BaseModel):
    target: str
    width: int | None = None
    height: int | None = None
    size: int
    image_b64: str
    saved_to: str | None = None


# ---------- helpers -------------------------------------------------


def _can_connect(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ---------- start ---------------------------------------------------


async def qemu_start(fleet: Fleet, target: str) -> QemuStartResult:
    """Launch QEMU for a configured target. Returns its PID."""
    cfg = fleet.target_config(target)
    if cfg.type != "qemu":
        raise InvalidParams(
            "qemu.start only applies to type=qemu targets",
            data={"target": target, "type": cfg.type},
        )
    if cfg.qemu_config is None:
        raise InvalidParams(
            "target.qemu_config is required for qemu.start",
            data={"target": target},
        )
    qemu_binary = fleet.config.paths.qemu_binary
    if qemu_binary is None:
        raise InvalidParams("paths.qemu_binary not set in config")

    existing = fleet.qemu_process(target)
    if existing is not None and existing.poll() is None:
        raise TargetError(
            f"target {target!r} is already running (pid {existing.pid})",
            data={"pid": existing.pid},
        )

    cmd, ports = build_cmdline(qemu_binary, Path(cfg.qemu_config), cfg)

    # Capture QEMU's stdout/stderr (the kyvos cmdline pipes -serial
    # to stdio) into a per-target log file under archive_root so the
    # qemu.serial_log MCP resource has something to read.
    archive_dir = (
        Path(fleet.config.server.archive_root) / "serial-logs" / target
    )
    archive_dir.mkdir(parents=True, exist_ok=True)
    serial_log_path = archive_dir / f"{int(time.time())}.log"
    # The child gets its own duplicate of the file descriptor; the
    # parent must close its handle once Popen has cloned it, otherwise
    # every qemu.start leaks one open fd.
    serial_fh = serial_log_path.open("wb")
    try:
        proc = await asyncio.to_thread(
            subprocess.Popen,
            cmd,
            cwd=str(Path(qemu_binary).parent),
            stdin=subprocess.DEVNULL,
            stdout=serial_fh,
            stderr=subprocess.STDOUT,
        )
    finally:
        serial_fh.close()
    fleet.register_qemu_process(
        target, proc, serial_log_path=serial_log_path,
    )
    return QemuStartResult(
        target=target, pid=proc.pid, ports=ports, cmdline=cmd,
        serial_log=str(serial_log_path),
    )


# ---------- stop ----------------------------------------------------


async def qemu_stop(
    fleet: Fleet, target: str, *, qmp_timeout_s: float = 10.0
) -> QemuStopResult:
    proc = fleet.qemu_process(target)
    if proc is None:
        return QemuStopResult(target=target, method="already-stopped")
    if proc.poll() is not None:
        rc = proc.returncode
        fleet.drop_qemu_process(target)
        return QemuStopResult(
            target=target, method="already-stopped", exit_code=rc
        )

    method: Literal["qmp", "terminate", "kill", "already-stopped"] = "qmp"
    exit_code: int | None = None
    try:
        # Try graceful QMP quit if QMP channel configured.
        cfg = fleet.target_config(target)
        if cfg.channels.qmp is not None and cfg.channels.qmp.enabled:
            qmp = fleet.qmp(target)
            try:
                await asyncio.wait_for(qmp.quit(), qmp_timeout_s)
            except (TimeoutError, TargetError):
                method = "terminate"
        else:
            method = "terminate"

        # Wait for process to exit; escalate to terminate / kill if it
        # didn't go.
        exited = False
        for _ in range(int(qmp_timeout_s * 2)):
            if proc.poll() is not None:
                exit_code = proc.returncode
                exited = True
                break
            await asyncio.sleep(0.5)
        if not exited:
            method = "terminate"
            await asyncio.to_thread(proc.terminate)
            try:
                exit_code = await asyncio.to_thread(proc.wait, 5)
            except subprocess.TimeoutExpired:
                method = "kill"
                await asyncio.to_thread(proc.kill)
                exit_code = await asyncio.to_thread(proc.wait, 5)
    finally:
        fleet.drop_qemu_process(target)

    return QemuStopResult(target=target, method=method, exit_code=exit_code)


# ---------- reset ---------------------------------------------------


async def qemu_reset(fleet: Fleet, target: str) -> QemuResetResult:
    qmp = fleet.qmp(target)
    resp = await qmp.system_reset()
    return QemuResetResult(target=target, response=resp)


# ---------- status --------------------------------------------------


async def qemu_status(fleet: Fleet, target: str) -> QemuStatusResult:
    cfg = fleet.target_config(target)
    proc = fleet.qemu_process(target)
    running = proc is not None and proc.poll() is None
    pid = proc.pid if running and proc is not None else None

    qmp_status = None
    if running and cfg.channels.qmp is not None and cfg.channels.qmp.enabled:
        try:
            qmp = fleet.qmp(target)
            qmp_status = await asyncio.wait_for(qmp.query_status(), 3.0)
        except (TimeoutError, TargetError):
            qmp_status = None

    mcpd_reachable = None
    if cfg.channels.mcpd is not None and cfg.channels.mcpd.enabled:
        mch = cfg.channels.mcpd
        mcpd_reachable = _can_connect(mch.host, mch.port, 1.0)

    return QemuStatusResult(
        target=target,
        running=running,
        pid=pid,
        qmp_status=qmp_status,
        mcpd_reachable=mcpd_reachable,
    )


# ---------- screenshot ---------------------------------------------


async def qemu_screenshot(
    fleet: Fleet, target: str, *, save_path: str | None = None
) -> QemuScreenshotResult:
    qmp = fleet.qmp(target)
    if save_path:
        out = Path(save_path)
    else:
        # mkstemp returns (fd, path); we don't need the fd (qmp writes
        # to the path on disk). Close it to avoid leaking a descriptor
        # per call.
        fd, tmp_path = tempfile.mkstemp(
            prefix="amiga_fleet_screen_", suffix=".png",
        )
        os.close(fd)
        out = Path(tmp_path)
    try:
        await qmp.screendump(out)
        # Give QEMU a moment to flush the file (screendump is sync but
        # we've seen partial writes on slow IO).
        for _ in range(20):
            if out.exists() and out.stat().st_size > 0:
                break
            await asyncio.sleep(0.05)
        if not out.exists() or out.stat().st_size == 0:
            raise InternalError("screendump produced empty file")
        data = out.read_bytes()
        return QemuScreenshotResult(
            target=target,
            size=len(data),
            image_b64=base64.b64encode(data).decode("ascii"),
            saved_to=str(out) if save_path else None,
        )
    finally:
        if not save_path:
            try:
                out.unlink()
            except OSError:
                pass
