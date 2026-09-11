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

from ..archive.store import prune_logs
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
    #: How durability was handled before the stop: "fs.sync" (the
    #: guest confirmed the flush), "nothing-written", "waited-Ns"
    #: (older daemon, timed hope), "already-elapsed", or "skipped".
    settled: str = "nothing-written"
    method: Literal["qmp", "qmp-unowned", "terminate", "kill",
                    "already-stopped"]
    exit_code: int | None = None


class QemuStatusResult(BaseModel):
    target: str
    #: True when a guest is live, whoever started it.
    running: bool
    #: True only when *this* process holds the QEMU handle. A guest
    #: started elsewhere is `running=True, owned=False`; `pid` is
    #: available only for an owned one.
    owned: bool = False
    pid: int | None = None
    qmp_status: dict[str, Any] | None = None
    mcpd_reachable: bool | None = None


class QemuResetResult(BaseModel):
    target: str
    #: "stop_start" (the default, and the only one AmigaOS 4 guests
    #: survive) or "system_reset" when that was asked for explicitly.
    method: str = "stop_start"
    response: dict[str, Any] = {}
    pid: int | None = None


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
        raise InvalidParams(
            "paths.qemu_binary not set in config — "
            "see USAGE.md#helper-paths-paths or run "
            "`amiga-fleet-mcp --init`"
        )

    existing = fleet.qemu_process(target)
    if existing is not None and existing.poll() is None:
        raise TargetError(
            f"target {target!r} is already running (pid {existing.pid})",
            data={"pid": existing.pid},
        )

    # A guest we did not start is still a guest. Launching a second
    # QEMU on the same target gets a process that cannot bind the
    # hostfwd ports and dies, after which every call quietly lands on
    # the original guest -- a failure that presents as anything but
    # what it is.
    for chan, label in ((cfg.channels.mcpd, "mcpd"),
                        (cfg.channels.qmp, "qmp")):
        if chan is None or not chan.enabled:
            continue
        if _can_connect(chan.host, chan.port, 1.0):
            raise TargetError(
                f"target {target!r} already has something listening on its "
                f"{label} port ({chan.host}:{chan.port}) -- a guest is "
                "running that this process did not start. Stop it first "
                "(qemu.stop now handles unowned guests) or check "
                "qemu.status, which reports `owned`.",
                data={"target": target, "port": chan.port, "channel": label},
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
    # One log per launch, none of them small, and nothing used to
    # remove them -- 2.8 GB of old serial output on one machine.
    keep = fleet.config.server.serial_log_keep
    if keep is not None and keep > 0:
        prune_logs(archive_dir, keep)
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

#: How long to let the guest settle before killing QEMU, when
#: something has been written to it.
#:
#: Stopping QEMU is abrupt however it is done -- QMP `quit` included --
#: and AmigaOS writes its filesystem cache back lazily, so a write that
#: has not reached the disk image yet is simply gone. Measured on a
#: pegasos2 guest with a 312 KB write: lost at 0 s and at 2 s, kept at
#: 5 s, 10 s and 20 s. Ten seconds is double the smallest value
#: observed to work, and only applies when there is something to lose.
DEFAULT_SETTLE_S = 10.0


async def _settle_before_stop(
    fleet: Fleet, target: str, settle_s: float,
) -> str:
    """Make sure the guest's writes are on disk before we kill it.

    Preferred route is `fs.sync`, which sends ACTION_FLUSH and waits
    for the filesystem to answer -- when it returns, the data is down.
    Older daemons have no such method, so fall back to waiting and
    hoping: that is what `settle_s` is for, and it is genuinely only
    hope. Measured on a pegasos2 guest, the same 312 KB write was lost
    at 2 s, kept at 5/10/20 s, then lost again at 10 s on a later run.
    The periodic flush has enough jitter that no fixed delay is a
    guarantee, which is exactly why the explicit flush exists.

    A target that has only been read from does neither.
    """
    t = fleet.mcpd_if_connected(target)
    if t is None or t.last_write_at is None:
        return "nothing-written"

    try:
        await asyncio.wait_for(t.request("fs.sync", {"path": "SYS:"}), 30.0)
        return "fs.sync"
    except Exception:
        # Daemon predates fs.sync, or the volume would not flush.
        pass

    if settle_s <= 0:
        return "skipped"
    remaining = settle_s - (time.monotonic() - t.last_write_at)
    if remaining <= 0:
        return "already-elapsed"
    await asyncio.sleep(remaining)
    return f"waited-{remaining:.1f}s"


async def _stop_unowned(
    fleet: Fleet, target: str, qmp_timeout_s: float,
) -> QemuStopResult:
    """Stop a guest whose process handle we do not hold."""
    cfg = fleet.target_config(target)
    qch = cfg.channels.qmp
    qmp_up = (qch is not None and qch.enabled
              and _can_connect(qch.host, qch.port, 1.0))
    if qmp_up:
        try:
            qmp = fleet.qmp(target)
            await asyncio.wait_for(qmp.quit(), qmp_timeout_s)
            return QemuStopResult(target=target, method="qmp-unowned")
        except (TimeoutError, TargetError) as e:
            raise TargetError(
                f"guest for {target!r} is running but was not started by "
                f"this process, and QMP quit failed: {e}",
                data={"target": target, "owned": False},
            ) from e

    mch = cfg.channels.mcpd
    reachable = (mch is not None and mch.enabled
                 and _can_connect(mch.host, mch.port, 1.0))
    if reachable:
        raise TargetError(
            f"guest for {target!r} is running but was not started by this "
            "process and has no reachable QMP channel, so it cannot be "
            "stopped from here. Stop it where it was started, or "
            "configure [targets.<name>.channels.qmp].",
            data={"target": target, "owned": False},
        )
    return QemuStopResult(target=target, method="already-stopped")


async def qemu_stop(
    fleet: Fleet, target: str, *, qmp_timeout_s: float = 10.0,
    settle_s: float | None = None,
) -> QemuStopResult:
    """Stop the target's guest.

    Works on a guest this process did not start. The handle-based path
    is preferred when we have one, but QMP `quit` reaches any guest
    with a QMP channel, and a guest started by a previous session is
    still very much running -- reporting `already-stopped` for it made
    restart sequences silently operate on a machine they believed they
    had replaced.

    If anything has been written to this target through MCPd, the
    guest is given `settle_s` seconds (default 10) to write its
    filesystem cache back first, because stopping QEMU is abrupt and
    an unflushed write is simply lost. Targets that have only been
    read from wait not at all. Pass `settle_s=0` to skip it when you
    know nothing is pending and want the stop immediately.
    """
    settled = await _settle_before_stop(
        fleet, target,
        settle_s if settle_s is not None else DEFAULT_SETTLE_S)

    proc = fleet.qemu_process(target)
    if proc is None:
        r = await _stop_unowned(fleet, target, qmp_timeout_s)
        r.settled = settled
        return r
    if proc.poll() is not None:
        rc = proc.returncode
        fleet.drop_qemu_process(target)
        return QemuStopResult(
            target=target, method="already-stopped", exit_code=rc,
            settled=settled,
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

    return QemuStopResult(target=target, method=method,
                          exit_code=exit_code, settled=settled)


# ---------- reset ---------------------------------------------------


async def qemu_reset(
    fleet: Fleet, target: str, *, system_reset: bool = False,
) -> QemuResetResult:
    """Restart the guest.

    By default this stops the QEMU process and starts a fresh one,
    because that is the only restart an AmigaOS 4 guest reliably
    survives. QMP `system_reset` leaves the kernel stuck partway
    through the reboot on every machine model this project targets,
    so it is not the default even though it is what "reset" means
    to QEMU.

    `system_reset=True` sends the raw QMP command for anyone driving
    a non-AmigaOS guest, or comparing the two.

    The stop path settles first, like `qemu.stop`, so writes the
    guest has not yet flushed are not lost on the way through.
    """
    if system_reset:
        qmp = fleet.qmp(target)
        resp = await qmp.system_reset()
        return QemuResetResult(
            target=target, method="system_reset", response=resp)

    await qemu_stop(fleet, target)
    await asyncio.sleep(2.0)
    started = await qemu_start(fleet, target)
    return QemuResetResult(
        target=target, method="stop_start", pid=started.pid,
        response={"stopped": True, "started": True},
    )


# ---------- status --------------------------------------------------


async def qemu_status(fleet: Fleet, target: str) -> QemuStatusResult:
    """Is this target's guest running?

    "Running" means running, not "started by us". A guest launched by
    a previous session, a helper script, or another process is still a
    guest: reporting it as stopped makes a restart sequence believe it
    restarted something when it did not. `owned` carries the
    distinction the caller might actually want.
    """
    cfg = fleet.target_config(target)
    proc = fleet.qemu_process(target)
    owned = proc is not None and proc.poll() is None
    pid = proc.pid if owned and proc is not None else None

    # Ports answering is evidence of a live guest whoever started it.
    qmp_up = False
    if cfg.channels.qmp is not None and cfg.channels.qmp.enabled:
        qch = cfg.channels.qmp
        qmp_up = _can_connect(qch.host, qch.port, 1.0)
    mcpd_up = False
    if cfg.channels.mcpd is not None and cfg.channels.mcpd.enabled:
        mch0 = cfg.channels.mcpd
        mcpd_up = _can_connect(mch0.host, mch0.port, 1.0)
    running = owned or qmp_up or mcpd_up

    qmp_status = None
    if qmp_up and cfg.channels.qmp is not None and cfg.channels.qmp.enabled:
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
        owned=owned,
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
