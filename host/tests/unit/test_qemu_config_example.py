"""The shipped QEMU machine template (host/qemu-config.example.json)
must parse and produce a working command line. See QEMU_CONFIG.md."""

from __future__ import annotations

import json
from pathlib import Path

from amiga_fleet_mcp.config import (
    GdbChannel,
    McpdChannel,
    QmpChannel,
    TargetChannels,
    TargetConfig,
)
from amiga_fleet_mcp.qemu.cmdline import build_cmdline

EXAMPLE = Path(__file__).resolve().parents[2] / "qemu-config.example.json"


def _target(headless: bool = False) -> TargetConfig:
    return TargetConfig(
        type="qemu",
        qemu_config=EXAMPLE,
        headless=headless,
        channels=TargetChannels(
            mcpd=McpdChannel(endpoint="127.0.0.1:4422"),
            qmp=QmpChannel(endpoint="127.0.0.1:14422"),
            gdb=GdbChannel(endpoint="127.0.0.1:1234"),
        ),
    )


def test_example_is_args_only_without_server_owned_options() -> None:
    data = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    assert set(data) == {"args"}
    text = json.dumps(data["args"])
    for owned in ("hostfwd", "-qmp", "-gdb", "-pidfile"):
        assert owned not in text, owned


def test_example_builds_with_injected_forwards() -> None:
    cmd, ports = build_cmdline(Path("qemu-system-ppc"), EXAMPLE, _target())
    netdev = cmd[cmd.index("-netdev") + 1]
    assert netdev.startswith("user,")
    assert "hostfwd=tcp::4422-:4322" in netdev
    assert "hostfwd=udp::4422-:4323" in netdev
    assert "tcp:127.0.0.1:14422,server=on,wait=off" in cmd
    assert "tcp::1234" in cmd
    assert cmd[cmd.index("-display") + 1].startswith("sdl")
    # Quoted options come through whole, quotes removed.
    assert "serial debuglevel=1" in cmd
    assert ports == {"mcpd": 4422, "qmp": 14422, "gdb": 1234,
                     "discovery": 4422}


def test_example_headless_replaces_display() -> None:
    cmd, _ = build_cmdline(Path("qemu-system-ppc"), EXAMPLE, _target(True))
    assert cmd[cmd.index("-display") + 1] == "none"
