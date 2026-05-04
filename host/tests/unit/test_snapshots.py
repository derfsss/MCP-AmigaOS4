"""Unit tests for the snapshot tool surface (phase 11)."""

from __future__ import annotations

from typing import Any

import pytest

from amiga_fleet_mcp.config import (
    Config,
    QmpChannel,
    TargetChannels,
    TargetConfig,
)
from amiga_fleet_mcp.errors import TargetError
from amiga_fleet_mcp.fleet import Fleet
from amiga_fleet_mcp.tools import snapshots as snap_tool

SAMPLE_INFO_SNAPSHOTS = """\
List of snapshots present on all disks:
ID        TAG               VM SIZE                DATE       VM CLOCK
1         before-test        4.2 GiB 2026-04-29 09:01:23   00:01:03.213
2         after-build       12.0 MiB 2026-04-29 09:15:01   00:14:42.108

List of partial (non-loadable) snapshots on 'vd1':
"""


def test_parse_info_snapshots_two_entries() -> None:
    entries = snap_tool._parse_info_snapshots(SAMPLE_INFO_SNAPSHOTS)
    names = [e.name for e in entries]
    assert names == ["before-test", "after-build"]
    assert entries[0].vm_size == "4.2 GiB"
    assert entries[1].vm_clock == "00:14:42.108"


def test_parse_info_snapshots_empty() -> None:
    out = "There is no snapshot available.\n"
    assert snap_tool._parse_info_snapshots(out) == []


def test_check_hmp_error_triggers() -> None:
    with pytest.raises(TargetError):
        snap_tool._check_hmp_error("Error: cannot do that\n", op="savevm")
    with pytest.raises(TargetError):
        snap_tool._check_hmp_error(
            "qemu-system-ppc: snapshot foo not found", op="loadvm"
        )


def test_check_hmp_error_silent_on_clean_output() -> None:
    snap_tool._check_hmp_error("", op="savevm")
    snap_tool._check_hmp_error("ID 1 created\n", op="savevm")


# ---- end-to-end via FakeQmp -------------------------------------


class FakeQmp:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.responses: dict[str, str] = {}

    async def hmp(self, command: str) -> str:
        self.calls.append(command)
        return self.responses.get(command, "")


def _fleet_with_qmp(qmp: Any) -> Fleet:
    cfg = Config(
        targets={
            "qemu-pegasos2": TargetConfig(
                type="qemu",
                channels=TargetChannels(
                    qmp=QmpChannel(endpoint="127.0.0.1:14422"),
                ),
            )
        }
    )
    fleet = Fleet(cfg)
    fleet._qmp["qemu-pegasos2"] = qmp  # type: ignore[assignment]
    return fleet


@pytest.mark.asyncio
async def test_savevm_round_trip() -> None:
    q = FakeQmp()
    fleet = _fleet_with_qmp(q)
    r = await snap_tool.qemu_savevm(fleet, "qemu-pegasos2", "snap1")
    assert q.calls == ["savevm snap1"]
    assert r.name == "snap1"


@pytest.mark.asyncio
async def test_savevm_detects_error() -> None:
    q = FakeQmp()
    q.responses["savevm bad"] = "Error: virtfs is mounted, cannot snapshot"
    fleet = _fleet_with_qmp(q)
    with pytest.raises(TargetError) as exc:
        await snap_tool.qemu_savevm(fleet, "qemu-pegasos2", "bad")
    assert "savevm failed" in exc.value.message


@pytest.mark.asyncio
async def test_list_snapshots_parses() -> None:
    q = FakeQmp()
    q.responses["info snapshots"] = SAMPLE_INFO_SNAPSHOTS
    fleet = _fleet_with_qmp(q)
    r = await snap_tool.qemu_list_snapshots(fleet, "qemu-pegasos2")
    assert [s.name for s in r.snapshots] == ["before-test", "after-build"]


@pytest.mark.asyncio
async def test_loadvm_round_trip() -> None:
    q = FakeQmp()
    fleet = _fleet_with_qmp(q)
    r = await snap_tool.qemu_loadvm(fleet, "qemu-pegasos2", "snap1")
    assert q.calls == ["loadvm snap1"]
    assert r.name == "snap1"


@pytest.mark.asyncio
async def test_delete_snapshot_round_trip() -> None:
    q = FakeQmp()
    fleet = _fleet_with_qmp(q)
    r = await snap_tool.qemu_delete_snapshot(fleet, "qemu-pegasos2", "snap1")
    assert q.calls == ["delvm snap1"]
    assert r.name == "snap1"
