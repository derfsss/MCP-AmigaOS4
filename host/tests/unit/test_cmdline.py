"""Unit tests for the QEMU cmdline builder + hostfwd injection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from amiga_fleet_mcp.config import (
    McpdChannel,
    QmpChannel,
    TargetChannels,
    TargetConfig,
)
from amiga_fleet_mcp.qemu.cmdline import (
    _inject_hostfwd,  # type: ignore[attr-defined]
    build_cmdline,
)


def test_inject_hostfwd_appends() -> None:
    arg = "-netdev user,id=nic,hostname=foo"
    out = _inject_hostfwd(arg, ["hostfwd=tcp::4322-:4322"])
    assert "hostfwd=tcp::4322-:4322" in out


def test_inject_hostfwd_dedups() -> None:
    arg = "-netdev user,id=nic,hostfwd=tcp::4322-:4322"
    out = _inject_hostfwd(arg, ["hostfwd=tcp::4322-:4322"])
    assert out.count("hostfwd=tcp::4322-:4322") == 1


def test_build_cmdline_injects_mcpd_and_qmp(tmp_path: Path) -> None:
    cfg_path = tmp_path / "kyvos.json"
    cfg_path.write_text(json.dumps({
        "args": {
            "machine": "-M pegasos2",
            "memory": "-m 2048M",
            "network": "-device rtl8139,netdev=nic -netdev user,id=nic,hostname=h",
            "display": "-display sdl",
            "pidfile": "-pidfile junk",
        }
    }))
    target = TargetConfig(
        type="qemu",
        qemu_config=cfg_path,
        channels=TargetChannels(
            mcpd=McpdChannel(endpoint="127.0.0.1:4322"),
            qmp=QmpChannel(endpoint="127.0.0.1:14422"),
        ),
    )
    cmd, ports = build_cmdline(Path("qemu"), cfg_path, target)
    s = " ".join(cmd)
    assert "hostfwd=tcp::4322-:4322" in s
    assert "-qmp" in s and "tcp:127.0.0.1:14422" in s
    assert "junk" not in s   # pidfile dropped
    assert ports["mcpd"] == 4322
    assert ports["qmp"] == 14422


def test_build_cmdline_distinct_host_ports(tmp_path: Path) -> None:
    """Multi-target case: host port != guest port."""
    cfg_path = tmp_path / "kyvos.json"
    cfg_path.write_text(json.dumps({
        "args": {
            "machine": "-M pegasos2",
            "network": "-netdev user,id=nic",
        }
    }))
    target = TargetConfig(
        type="qemu", qemu_config=cfg_path,
        channels=TargetChannels(
            mcpd=McpdChannel(endpoint="127.0.0.1:4422"),
        ),
    )
    cmd, ports = build_cmdline(Path("qemu"), cfg_path, target)
    s = " ".join(cmd)
    assert "hostfwd=tcp::4422-:4322" in s
    assert ports["mcpd"] == 4422


def test_build_cmdline_headless(tmp_path: Path) -> None:
    cfg_path = tmp_path / "kyvos.json"
    cfg_path.write_text(json.dumps({"args": {
        "machine": "-M pegasos2",
        "display": "-display sdl,gl=off",
        "network": "-netdev user,id=nic",
    }}))
    target = TargetConfig(
        type="qemu", qemu_config=cfg_path,
        channels=TargetChannels(),
    )
    cmd, _ = build_cmdline(Path("qemu"), cfg_path, target, headless=True)
    s = " ".join(cmd)
    assert "-display none" in s
    assert "sdl" not in s


def test_build_cmdline_missing_kyvos(tmp_path: Path) -> None:
    from amiga_fleet_mcp.errors import InternalError

    target = TargetConfig(type="qemu", channels=TargetChannels())
    with pytest.raises(InternalError):
        build_cmdline(Path("qemu"), tmp_path / "missing.json", target)
