"""Cmdline injection check: -gdb tcp::PORT when gdb channel is set."""

from __future__ import annotations

import json
from pathlib import Path

from amiga_fleet_mcp.config import (
    GdbChannel,
    QmpChannel,
    TargetChannels,
    TargetConfig,
)
from amiga_fleet_mcp.qemu.cmdline import build_cmdline


def test_build_cmdline_injects_gdb(tmp_path: Path) -> None:
    cfg_path = tmp_path / "kyvos.json"
    cfg_path.write_text(json.dumps({"args": {
        "machine": "-M pegasos2",
        "network": "-netdev user,id=nic",
    }}))
    target = TargetConfig(
        type="qemu", qemu_config=cfg_path,
        channels=TargetChannels(
            qmp=QmpChannel(endpoint="127.0.0.1:14422"),
            gdb=GdbChannel(endpoint="127.0.0.1:1234"),
        ),
    )
    cmd, ports = build_cmdline(Path("qemu"), cfg_path, target)
    s = " ".join(cmd)
    assert "-gdb tcp::1234" in s
    assert "-qmp tcp:127.0.0.1:14422" in s
    assert ports["gdb"] == 1234
    assert ports["qmp"] == 14422


def test_build_cmdline_no_gdb_when_disabled(tmp_path: Path) -> None:
    cfg_path = tmp_path / "kyvos.json"
    cfg_path.write_text(json.dumps({"args": {"machine": "-M pegasos2"}}))
    target = TargetConfig(
        type="qemu", qemu_config=cfg_path,
        channels=TargetChannels(
            gdb=GdbChannel(endpoint="127.0.0.1:1234", enabled=False),
        ),
    )
    cmd, ports = build_cmdline(Path("qemu"), cfg_path, target)
    s = " ".join(cmd)
    assert "-gdb" not in s
    assert ports["gdb"] is None
