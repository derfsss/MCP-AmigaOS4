"""QEMU snapshot management (savevm / loadvm / list / delete).

QEMU's `savevm` / `loadvm` aren't directly exposed as QMP commands -
we drive them via QMP `human-monitor-command`, which executes an HMP
line and returns its textual output. The output is then parsed for
success / failure.
"""

from __future__ import annotations

import re

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


async def qemu_savevm(fleet: Fleet, target: str, name: str) -> SavevmResult:
    """Save the running VM state under `name` (in the qcow2 backing
    store)."""
    qmp = fleet.qmp(target)
    out = await qmp.hmp(f"savevm {name}")
    _check_hmp_error(out, op="savevm")
    return SavevmResult(target=target, name=name, output=out)


async def qemu_loadvm(fleet: Fleet, target: str, name: str) -> LoadvmResult:
    """Restore VM state from the snapshot `name`."""
    qmp = fleet.qmp(target)
    out = await qmp.hmp(f"loadvm {name}")
    _check_hmp_error(out, op="loadvm")
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
