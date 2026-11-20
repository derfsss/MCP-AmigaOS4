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


# Two user-mode NICs, each with its -device, as config_rs.json has.
_TWO_NICS = (
    "-netdev user,id=vnet,net=10.0.3.0/24,hostfwd=tcp::4425-10.0.3.15:4322,"
    "hostfwd=udp::4425-10.0.3.15:4323 "
    "-device virtio-net-pci,netdev=vnet,mac=52:54:00:12:34:57 "
    "-netdev user,id=vnet2,net=10.0.4.0/24 "
    "-device virtio-net-pci,netdev=vnet2,addr=0x0c"
)


def test_inject_hostfwd_never_onto_a_device() -> None:
    out = _inject_hostfwd(_TWO_NICS, ["hostfwd=tcp::4500-:4322"])
    toks = out.split(" ")
    assert toks[1].endswith(",hostfwd=tcp::4500-:4322")
    assert all("hostfwd" not in t for t in toks if t.startswith("virtio-net-pci"))
    assert out.count("hostfwd=tcp::4500-:4322") == 1


def test_inject_hostfwd_skips_a_host_port_already_forwarded() -> None:
    # The config forwards 4425 to a fixed guest address already; a
    # second rule on tcp/4425 or udp/4425 would make QEMU abort.
    out = _inject_hostfwd(
        _TWO_NICS, ["hostfwd=tcp::4425-:4322", "hostfwd=udp::4425-:4323"])
    assert out == _TWO_NICS


def test_inject_hostfwd_leaves_non_user_netdev_alone() -> None:
    arg = "-netdev tap,id=t0,ifname=tap0 -device rtl8139,netdev=t0"
    assert _inject_hostfwd(arg, ["hostfwd=tcp::4322-:4322"]) == arg


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
