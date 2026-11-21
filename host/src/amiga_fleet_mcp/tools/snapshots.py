"""QEMU snapshot management (savevm / loadvm / list / delete).

QEMU's `savevm` / `loadvm` aren't directly exposed as QMP commands -
we drive them via QMP `human-monitor-command`, which executes an HMP
line and returns its textual output. The output is then parsed for
success / failure.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from ..errors import TargetError
from ..fleet import Fleet


class SavevmResult(BaseModel):
    target: str
    name: str
    output: str = ""


class LoadvmResult(BaseModel):
    target: str
    name: str
    output: str = ""


class SnapshotEntry(BaseModel):
    id: str
    name: str
    vm_size: str | None = None
    date: str | None = None
    vm_clock: str | None = None


class ListSnapshotsResult(BaseModel):
    target: str
    snapshots: list[SnapshotEntry]
    raw: str


class DeleteSnapshotResult(BaseModel):
    target: str
    name: str
    output: str = ""


# HMP `info snapshots` looks like:
#   ID        TAG                 VM SIZE                DATE       VM CLOCK
#   --        before-test        4.2 GiB 2026-04-29 ...     00:01:03.213
# Header parse - tolerate extra columns / spaces.
_SNAPLINE_RE = re.compile(
    r"^\s*(?P<id>[\d\-*]+)\s+"
    r"(?P<name>\S+)\s+"
    r"(?P<vmsize>\S+(?:\s*\S+)?)\s+"
    r"(?P<date>\S+\s+\S+)\s+"
    r"(?P<vmclock>[\d:.]+)\s*$"
)


def _parse_info_snapshots(out: str) -> list[SnapshotEntry]:
    """Parse `info snapshots` HMP output into SnapshotEntry rows."""
    entries: list[SnapshotEntry] = []
    seen_header = False
    for line in out.splitlines():
        s = line.strip()
        if not s:
            continue
        # Skip the header + the dashed separator under it.
        if s.startswith("ID") or s.startswith("--") or s.startswith("List"):
            seen_header = True
            continue
        if not seen_header:
            continue
        m = _SNAPLINE_RE.match(s)
        if m:
            entries.append(SnapshotEntry(
                id=m.group("id"),
                name=m.group("name"),
                vm_size=m.group("vmsize"),
                date=m.group("date"),
                vm_clock=m.group("vmclock"),
            ))
            continue
        # Fall-back: take the second whitespace-separated field as the
        # name and put the rest into `id`. Better to surface a partial
        # entry than to drop a snapshot row silently.
        parts = s.split()
        if len(parts) >= 2:
            entries.append(SnapshotEntry(id=parts[0], name=parts[1]))
    return entries


_ERR_PATTERNS = re.compile(
    r"^Error:|^qemu-system-ppc:|cannot|unable to|not found",
    re.IGNORECASE | re.MULTILINE,
)


def _check_hmp_error(out: str, *, op: str) -> None:
    if _ERR_PATTERNS.search(out):
        raise TargetError(
            f"{op} failed: {out.strip().splitlines()[0]}",
            data={"hmp_output": out},
        )


# ---------- public methods ----------------------------------------


async def _vm_running(qmp: Any) -> bool:
    st = await qmp.query_status()
    ret = st.get("return", st) if isinstance(st, dict) else {}
    return bool(ret.get("running"))


async def _run_keeping_vm_up(qmp: Any, command: str, *, op: str) -> str:
    """Run an HMP snapshot command. QEMU stops the VM for savevm /
    loadvm; when the command fails it can leave it stopped (a failed
    loadvm leaves it in "restore-vm" for good, and the guest then
    answers nothing). If the VM was running before and is not after a
    failure, resume it, and say so in the error's data."""
    was_running = await _vm_running(qmp)
    out = await qmp.hmp(command)
    try:
        _check_hmp_error(out, op=op)
    except TargetError as e:
        resumed = False
        if was_running and not await _vm_running(qmp):
            await qmp.command("cont")
            resumed = True
        data = dict(e.data) if isinstance(e.data, dict) else {}
        data["vm_resumed"] = resumed
        raise TargetError(e.message, data=data) from e
    return str(out)


async def qemu_savevm(fleet: Fleet, target: str, name: str) -> SavevmResult:
    """Save the running VM state under `name` (in the qcow2 backing
    store). On failure the VM keeps running."""
    qmp = fleet.qmp(target)
    out = await _run_keeping_vm_up(qmp, f"savevm {name}", op="savevm")
    return SavevmResult(target=target, name=name, output=out)


async def qemu_loadvm(fleet: Fleet, target: str, name: str) -> LoadvmResult:
    """Restore VM state from the snapshot `name`. A name that is not in
    `info snapshots` is refused without touching the VM; any other
    failure leaves the VM running if it was."""
    qmp = fleet.qmp(target)
    names = {e.name for e in _parse_info_snapshots(await qmp.hmp("info snapshots"))}
    if name not in names:
        raise TargetError(
            f"loadvm: no snapshot named {name!r}",
            data={"snapshots": sorted(names)},
        )
    out = await _run_keeping_vm_up(qmp, f"loadvm {name}", op="loadvm")
    return LoadvmResult(target=target, name=name, output=out)


async def qemu_list_snapshots(fleet: Fleet, target: str) -> ListSnapshotsResult:
    qmp = fleet.qmp(target)
    raw = await qmp.hmp("info snapshots")
    return ListSnapshotsResult(
        target=target,
        snapshots=_parse_info_snapshots(raw),
        raw=raw,
    )


async def qemu_delete_snapshot(
    fleet: Fleet, target: str, name: str
) -> DeleteSnapshotResult:
    qmp = fleet.qmp(target)
    out = await qmp.hmp(f"delvm {name}")
    _check_hmp_error(out, op="delvm")
    return DeleteSnapshotResult(target=target, name=name, output=out)
