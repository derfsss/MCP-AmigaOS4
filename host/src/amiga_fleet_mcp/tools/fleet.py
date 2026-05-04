"""fleet.* tools: list / status / fan-out / barrier / quorum_run.

Coordination primitives:

- `fleet.barrier(method, params, targets, per_target_timeout_s)`
  fans out like run_on_all but each per-target call has its own
  asyncio.wait_for; useful when one target is allowed to be slow but
  the overall barrier shouldn't block forever on any single target.
- `fleet.quorum_run(method, params, targets, quorum)` returns as soon
  as `quorum` calls have succeeded. Other still-running calls are
  cancelled. Useful for "ask N machines the same question, take the
  majority answer" patterns.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel

from ..errors import InvalidParams, JsonRpcError
from ..fleet import Fleet
from . import exec as exec_tool
from . import fs as fs_tool
from . import sys as sys_tool
from . import wb as wb_tool


class TargetSummary(BaseModel):
    name: str
    type: str
    display_name: str | None = None
    machine: str | None = None
    mcpd: str | None = None
    qmp: str | None = None
    qemu_running: bool | None = None
    tags: list[str] = []


class TargetStatus(BaseModel):
    name: str
    type: str
    qemu_running: bool | None = None
    mcpd_reachable: bool | None = None
    qmp_reachable: bool | None = None


class FanOutEntry(BaseModel):
    ok: dict[str, Any] | list[Any] | str | int | float | bool | None = None
    error: dict[str, Any] | None = None


class FanOutResult(BaseModel):
    method: str
    results: dict[str, FanOutEntry]


# ---------- list_targets / target_status ---------------------------


class TargetListResult(BaseModel):
    targets: list[TargetSummary]


async def fleet_list_targets(
    fleet: Fleet, tags: list[str] | None = None,
) -> TargetListResult:
    out: list[TargetSummary] = []
    for name in fleet.list_targets(tags=tags):
        cfg = fleet.target_config(name)
        mcpd = cfg.channels.mcpd
        qmp = cfg.channels.qmp
        proc = fleet.qemu_process(name)
        out.append(TargetSummary(
            name=name,
            type=cfg.type,
            display_name=cfg.display_name,
            machine=cfg.machine,
            tags=list(cfg.tags),
            mcpd=mcpd.endpoint if mcpd and mcpd.enabled else None,
            qmp=qmp.endpoint if qmp and qmp.enabled else None,
            qemu_running=(
                proc is not None and proc.poll() is None
                if cfg.type == "qemu" else None
            ),
        ))
    return TargetListResult(targets=out)


async def fleet_target_status(fleet: Fleet, target: str) -> TargetStatus:
    import socket

    cfg = fleet.target_config(target)
    proc = fleet.qemu_process(target)
    running = proc is not None and proc.poll() is None

    def _probe(host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), 1.0):
                return True
        except OSError:
            return False

    mcpd_ok: bool | None = None
    if cfg.channels.mcpd and cfg.channels.mcpd.enabled:
        mc = cfg.channels.mcpd
        mcpd_ok = await asyncio.to_thread(_probe, mc.host, mc.port)

    qmp_ok: bool | None = None
    if cfg.channels.qmp and cfg.channels.qmp.enabled:
        qm = cfg.channels.qmp
        qmp_ok = await asyncio.to_thread(_probe, qm.host, qm.port)

    return TargetStatus(
        name=target,
        type=cfg.type,
        qemu_running=running if cfg.type == "qemu" else None,
        mcpd_reachable=mcpd_ok,
        qmp_reachable=qmp_ok,
    )


# ---------- fleet.snapshot ---------------------------------------


class TargetSnapshot(BaseModel):
    """Per-target health summary returned by fleet.snapshot."""
    name: str
    type: str
    display_name: str | None = None
    qemu_running: bool | None = None
    mcpd_reachable: bool | None = None
    qmp_reachable: bool | None = None
    uptime_s: float | None = None
    free_ram_mb: float | None = None
    last_alert: int | None = None
    error: str | None = None
    tags: list[str] = []


class FleetSnapshotResult(BaseModel):
    targets: list[TargetSnapshot]


async def _snapshot_one(fleet: Fleet, name: str) -> TargetSnapshot:
    """Probe one target. Reachable + uptime + free RAM + lastalert,
    all best-effort. Errors collapse into the `error` field; the rest
    is populated as much as the probe could fill in."""
    cfg = fleet.target_config(name)
    snap = TargetSnapshot(
        name=name, type=cfg.type, display_name=cfg.display_name,
        tags=list(cfg.tags),
    )

    # Step 1: TCP-level reachability via the existing fleet.target_status.
    try:
        st = await fleet_target_status(fleet, name)
        snap.qemu_running = st.qemu_running
        snap.mcpd_reachable = st.mcpd_reachable
        snap.qmp_reachable = st.qmp_reachable
    except Exception as e:
        snap.error = f"target_status: {e}"
        return snap

    # If MCPd isn't reachable, no point in further RPCs.
    if not snap.mcpd_reachable:
        return snap

    # Step 2: parallel sys.uptime + sys.memory + sys.lastalert.
    from . import sys as sys_tool

    async def _safe(coro: Any) -> Any:
        try:
            return await coro
        except Exception:
            return None

    up, mem, la = await asyncio.gather(
        _safe(sys_tool.sys_uptime(fleet, name)),
        _safe(sys_tool.sys_memory(fleet, name)),
        _safe(sys_tool.sys_lastalert(fleet, name)),
    )
    if up is not None:
        snap.uptime_s = float(up.seconds)
    if mem is not None:
        # mem.any.free is in bytes; convert to MiB for readability.
        try:
            snap.free_ram_mb = float(mem.any.free) / 1024.0 / 1024.0
        except Exception:
            pass
    if la is not None:
        try:
            snap.last_alert = int(la.alert_code)
        except Exception:
            pass
    return snap


async def fleet_snapshot(
    fleet: Fleet, tags: list[str] | None = None,
) -> FleetSnapshotResult:
    """One-call health summary across the fleet.

    Probes every configured target (or just those matching `tags`)
    in parallel and returns reachability + uptime + free RAM +
    lastalert per target. Replaces the multi-RPC probe pattern that
    every script reinvented.

    Per-target failures populate the `error` field rather than
    aborting; you always get one entry per target.
    """
    targets = fleet.list_targets(tags=tags)
    if not targets:
        return FleetSnapshotResult(targets=[])
    snaps = await asyncio.gather(
        *(_snapshot_one(fleet, t) for t in targets)
    )
    return FleetSnapshotResult(targets=list(snaps))


# ---------- run_on_all -------------------------------------------------


# Registry of fan-outable methods. Each entry takes (fleet, target,
# **params) and returns a coroutine.
_FANOUT: dict[str, Callable[..., Awaitable[Any]]] = {
    "fs.list": fs_tool.fs_list,
    "fs.stat": fs_tool.fs_stat,
    "fs.read": fs_tool.fs_read,
    "fs.write": fs_tool.fs_write,
    "fs.delete": fs_tool.fs_delete,
    "fs.makedir": fs_tool.fs_makedir,
    "fs.rename": fs_tool.fs_rename,
    "fs.protect": fs_tool.fs_protect,
    "fs.copy": fs_tool.fs_copy,
    "exec.cmd": exec_tool.exec_cmd,
    "sys.version": sys_tool.sys_version,
    "sys.tasks": sys_tool.sys_tasks,
    "sys.libraries": sys_tool.sys_libraries,
    "sys.devices": sys_tool.sys_devices,
    "sys.ports": sys_tool.sys_ports,
    "sys.lastalert": sys_tool.sys_lastalert,
    "wb.screens": wb_tool.wb_screens,
    "wb.windows": wb_tool.wb_windows,
}


def fanout_methods() -> list[str]:
    return sorted(_FANOUT.keys())


async def fleet_run_on_all(
    fleet: Fleet,
    method: str,
    params: dict[str, Any] | None = None,
    targets: list[str] | None = None,
    tags: list[str] | None = None,
) -> FanOutResult:
    """Run `method(target, **params)` against multiple targets in parallel.

    `targets` selects an explicit subset; `tags` selects targets whose
    tag set is a superset of every entry. Pass neither to fan out
    across all configured targets.

    Always returns one entry per target (no early raise). On failure,
    `error` carries the JSON-RPC error dict; on success, `ok` carries
    the model-dumped result.
    """
    if method not in _FANOUT:
        raise InvalidParams(
            f"method {method!r} cannot be fanned out",
            data={"available": fanout_methods()},
        )
    fn = _FANOUT[method]
    params = params or {}

    if targets is None:
        targets = fleet.list_targets(tags=tags)
    if not targets:
        return FanOutResult(method=method, results={})

    async def _one(name: str) -> tuple[str, FanOutEntry]:
        try:
            r = await fn(fleet, name, **params)
        except JsonRpcError as e:
            return name, FanOutEntry(error=e.to_dict())
        except Exception as e:
            return name, FanOutEntry(
                error={"code": -32603, "message": str(e)}
            )
        if hasattr(r, "model_dump"):
            return name, FanOutEntry(ok=r.model_dump())
        if isinstance(r, list):
            return name, FanOutEntry(
                ok=[x.model_dump() if hasattr(x, "model_dump") else x for x in r]
            )
        return name, FanOutEntry(ok=r)

    pairs = await asyncio.gather(*(_one(t) for t in targets))
    return FanOutResult(method=method, results=dict(pairs))


# ---------- barrier (phase 11b) -----------------------------------


def _model_dump_or_pass(r: Any) -> Any:
    if hasattr(r, "model_dump"):
        return r.model_dump()
    if isinstance(r, list):
        return [x.model_dump() if hasattr(x, "model_dump") else x for x in r]
    return r


async def fleet_barrier(
    fleet: Fleet,
    method: str,
    params: dict[str, Any] | None = None,
    targets: list[str] | None = None,
    per_target_timeout_s: float = 30.0,
    tags: list[str] | None = None,
) -> FanOutResult:
    """Fan-out with a per-target timeout.

    Each target call is wrapped in `asyncio.wait_for`; targets that
    don't complete within `per_target_timeout_s` get a timeout error
    in their result entry rather than blocking the whole barrier.
    """
    if method not in _FANOUT:
        raise InvalidParams(
            f"method {method!r} cannot be fanned out",
            data={"available": fanout_methods()},
        )
    fn = _FANOUT[method]
    params = params or {}
    if targets is None:
        targets = fleet.list_targets(tags=tags)
    if not targets:
        return FanOutResult(method=method, results={})

    async def _one(name: str) -> tuple[str, FanOutEntry]:
        try:
            r = await asyncio.wait_for(
                fn(fleet, name, **params), per_target_timeout_s
            )
        except TimeoutError:
            return name, FanOutEntry(error={
                "code": -32002,
                "message": f"barrier: target timed out after {per_target_timeout_s}s",
                "data": {"target": name, "timeout_s": per_target_timeout_s},
            })
        except JsonRpcError as e:
            return name, FanOutEntry(error=e.to_dict())
        except Exception as e:
            return name, FanOutEntry(error={"code": -32603, "message": str(e)})
        return name, FanOutEntry(ok=_model_dump_or_pass(r))

    pairs = await asyncio.gather(*(_one(t) for t in targets))
    return FanOutResult(method=method, results=dict(pairs))


# ---------- quorum_run (phase 11b) --------------------------------


class QuorumRunResult(BaseModel):
    method: str
    quorum: int
    reached: bool
    results: dict[str, FanOutEntry]


async def fleet_quorum_run(
    fleet: Fleet,
    method: str,
    quorum: int,
    params: dict[str, Any] | None = None,
    targets: list[str] | None = None,
    overall_timeout_s: float = 60.0,
    tags: list[str] | None = None,
) -> QuorumRunResult:
    """Run `method` on each target; return as soon as `quorum` succeed.

    Outstanding calls beyond the quorum are cancelled. Failures count
    against quorum (a successful target needs both ok and no error).
    `reached` indicates whether `quorum` ok responses arrived before
    the targets list ran out *or* the overall timeout hit.
    """
    if method not in _FANOUT:
        raise InvalidParams(
            f"method {method!r} cannot be fanned out",
            data={"available": fanout_methods()},
        )
    if quorum <= 0:
        raise InvalidParams("quorum must be >= 1")
    fn = _FANOUT[method]
    params = params or {}
    if targets is None:
        targets = fleet.list_targets(tags=tags)
    if quorum > len(targets):
        raise InvalidParams(
            f"quorum ({quorum}) > targets ({len(targets)})",
            data={"targets": targets},
        )

    results: dict[str, FanOutEntry] = {}
    ok_count = 0

    async def _one(name: str) -> tuple[str, FanOutEntry]:
        try:
            r = await fn(fleet, name, **params)
        except JsonRpcError as e:
            return name, FanOutEntry(error=e.to_dict())
        except Exception as e:
            return name, FanOutEntry(error={"code": -32603, "message": str(e)})
        return name, FanOutEntry(ok=_model_dump_or_pass(r))

    tasks = {asyncio.create_task(_one(t)): t for t in targets}
    deadline = asyncio.get_event_loop().time() + overall_timeout_s

    try:
        while tasks and ok_count < quorum:
            remaining = max(0.0, deadline - asyncio.get_event_loop().time())
            if remaining <= 0:
                break
            done, _pending = await asyncio.wait(
                tasks.keys(),
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                break  # overall timeout
            for t in done:
                name, entry = await t
                results[name] = entry
                if entry.error is None:
                    ok_count += 1
                tasks.pop(t, None)
    finally:
        # Cancel any still-pending tasks - we have the quorum or timed out.
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    # For tasks we cancelled, record an entry so callers see the
    # complete target picture.
    for name in tasks.values():
        if name not in results:
            results[name] = FanOutEntry(error={
                "code": -32002,
                "message": "cancelled: quorum reached or overall timeout",
                "data": {"target": name},
            })

    return QuorumRunResult(
        method=method,
        quorum=quorum,
        reached=ok_count >= quorum,
        results=results,
    )


# ---------- fleet.relay (host-side file copy between targets) -------


class FleetRelayResult(BaseModel):
    src_target: str
    src_path: str
    dst_target: str
    dst_path: str
    bytes: int


async def fleet_relay(
    fleet: Fleet,
    src_target: str, src_path: str,
    dst_target: str, dst_path: str,
) -> FleetRelayResult:
    """Relay a file from one target to another via the host.

    Reads `src_path` from `src_target` (fs.read), writes the bytes
    to `dst_path` on `dst_target` (fs.write). Pure host-side
    coordination - no daemon-to-daemon transfer.
    """
    import base64
    rd = await fs_tool.fs_read(fleet, src_target, src_path)
    raw_bytes = base64.b64decode(rd.content_b64)
    wr = await fs_tool.fs_write(
        fleet, dst_target, dst_path,
        base64.b64encode(raw_bytes).decode("ascii"),
    )
    return FleetRelayResult(
        src_target=src_target, src_path=src_path,
        dst_target=dst_target, dst_path=dst_path,
        bytes=wr.size,
    )
