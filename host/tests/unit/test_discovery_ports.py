"""Discovery across QEMU's port forwarding.

A QEMU guest is reached through a `hostfwd` rule. The guest always
binds the default discovery port, but the *host* side of that rule
cannot also be the default for more than one guest: the second QEMU
refuses to start on a duplicate rule. So each guest gets its own host
port, which has two consequences the code has to handle, and which
went untested for long enough to ship half-broken:

1. A broadcast probe on the default port reaches no guest at all.
   Discovery has to probe each forwarded port directly, and only the
   configuration knows the numbers.
2. The daemon announces the TCP port it binds *inside* the guest.
   Reporting that back would hand the caller `127.0.0.1:4322` for a
   guest that is actually reachable on `127.0.0.1:4432` -- an endpoint
   that either fails outright or, on a workstation running several
   guests, quietly connects to the wrong machine.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from amiga_fleet_mcp.config import (
    Config,
    McpdChannel,
    TargetChannels,
    TargetConfig,
)
from amiga_fleet_mcp.fleet import Fleet
from amiga_fleet_mcp.qemu.cmdline import build_cmdline
from amiga_fleet_mcp.tools import fleet_discover as fd
from amiga_fleet_mcp.transports import discovery as disc


def _cfg_file(tmp_path: Path) -> Path:
    p = tmp_path / "kyvos.json"
    p.write_text(json.dumps({
        "args": {"machine": "-M pegasos2", "network": "-netdev user,id=nic"}
    }))
    return p


def _qemu_target(cfg_path: Path, endpoint: str,
                 discovery_port: int | None = None) -> TargetConfig:
    return TargetConfig(
        type="qemu", qemu_config=cfg_path,
        channels=TargetChannels(
            mcpd=McpdChannel(endpoint=endpoint,
                             discovery_port=discovery_port),
        ),
    )


# ---- the hostfwd rule ----------------------------------------------


def test_discovery_forward_defaults_to_the_mcpd_host_port(
    tmp_path: Path,
) -> None:
    """Two guests must not both claim the same host-side UDP port."""
    cfg_path = _cfg_file(tmp_path)
    a, _ = build_cmdline(Path("qemu"), cfg_path,
                         _qemu_target(cfg_path, "127.0.0.1:4422"))
    b, _ = build_cmdline(Path("qemu"), cfg_path,
                         _qemu_target(cfg_path, "127.0.0.1:4432"))
    sa, sb = " ".join(a), " ".join(b)
    assert "hostfwd=udp::4422-:4323" in sa
    assert "hostfwd=udp::4432-:4323" in sb
    # The guest side is always the fixed port -- only the host side moves.
    assert "-:4323" in sa and "-:4323" in sb


def test_discovery_forward_can_be_pinned(tmp_path: Path) -> None:
    cfg_path = _cfg_file(tmp_path)
    cmd, ports = build_cmdline(
        Path("qemu"), cfg_path,
        _qemu_target(cfg_path, "127.0.0.1:4422", discovery_port=4999))
    assert "hostfwd=udp::4999-:4323" in " ".join(cmd)
    assert ports["discovery"] == 4999
    assert ports["mcpd"] == 4422


def test_reported_ports_match_the_rules(tmp_path: Path) -> None:
    cfg_path = _cfg_file(tmp_path)
    _cmd, ports = build_cmdline(Path("qemu"), cfg_path,
                                _qemu_target(cfg_path, "127.0.0.1:4432"))
    assert ports["mcpd"] == 4432
    assert ports["discovery"] == 4432


# ---- what fleet.discover decides to probe ---------------------------


def _fleet(**targets: TargetConfig) -> Fleet:
    return Fleet(Config(targets=dict(targets)))


def test_probe_plan_covers_every_qemu_target(tmp_path: Path) -> None:
    cfg_path = _cfg_file(tmp_path)
    fleet = _fleet(
        one=_qemu_target(cfg_path, "127.0.0.1:4422"),
        two=_qemu_target(cfg_path, "127.0.0.1:4432", discovery_port=4999),
    )
    probes, endpoints = fd._qemu_probe_plan(fleet)
    assert ("127.0.0.1", 4422) in probes
    assert ("127.0.0.1", 4999) in probes
    # And a reply from either maps back to the reachable endpoint.
    assert endpoints[("127.0.0.1", 4422)]["endpoint"] == "127.0.0.1:4422"
    assert endpoints[("127.0.0.1", 4999)]["endpoint"] == "127.0.0.1:4432"
    assert endpoints[("127.0.0.1", 4999)]["target"] == "two"


def test_probe_plan_ignores_non_qemu_targets(tmp_path: Path) -> None:
    """Real hardware answers a broadcast; it needs no unicast probe."""
    cfg_path = _cfg_file(tmp_path)
    fleet = _fleet(
        guest=_qemu_target(cfg_path, "127.0.0.1:4422"),
        x5000=TargetConfig(
            type="remote",
            channels=TargetChannels(
                mcpd=McpdChannel(endpoint="192.168.0.26:4322")),
        ),
    )
    probes, endpoints = fd._qemu_probe_plan(fleet)
    assert probes == [("127.0.0.1", 4422)]
    assert all(ip == "127.0.0.1" for ip, _ in endpoints)


def test_probe_plan_skips_disabled_channels(tmp_path: Path) -> None:
    cfg_path = _cfg_file(tmp_path)
    t = _qemu_target(cfg_path, "127.0.0.1:4422")
    t.channels.mcpd.enabled = False  # type: ignore[union-attr]
    probes, _ = fd._qemu_probe_plan(_fleet(off=t))
    assert probes == []


# ---- the endpoint the caller is handed ------------------------------


@pytest.mark.asyncio
async def test_forwarded_reply_reports_the_reachable_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daemon announces 4322 from inside the guest; the caller must
    be handed the forwarded port it can actually connect to."""
    announced = {
        "mcp_discovery": 1, "v": 1, "server": "mcpd/1.3",
        "protocol": "1.0", "tcp_port": 4322, "host": "localhost",
        "methods": 59,
    }
    results = await _fake_discover(
        monkeypatch, announced,
        peer=("127.0.0.1", 4432),
        endpoint_map={("127.0.0.1", 4432): {
            "endpoint": "127.0.0.1:4432", "target": "guest"}},
    )
    assert len(results) == 1
    assert results[0]["endpoint"] == "127.0.0.1:4432"
    assert results[0]["tcp_port"] == 4432
    assert results[0]["target"] == "guest"


@pytest.mark.asyncio
async def test_unknown_responder_keeps_what_it_announced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A daemon we have no forward for -- real hardware on the LAN --
    is still reported, using the port it announced."""
    announced = {
        "mcp_discovery": 1, "v": 1, "server": "mcpd/1.3",
        "protocol": "1.0", "tcp_port": 4322, "host": "x5000",
        "methods": 59,
    }
    results = await _fake_discover(
        monkeypatch, announced, peer=("192.168.0.26", 4323), endpoint_map={})
    assert results[0]["endpoint"] == "192.168.0.26:4322"
    assert results[0]["target"] is None


async def _fake_discover(monkeypatch, announced, peer, endpoint_map):
    """Drive discover() against one canned UDP reply."""
    sent: list[tuple[bytes, tuple[str, int]]] = []
    state: dict[str, object] = {}

    class FakeSock:
        def setsockopt(self, *a): pass
        def setblocking(self, *a): pass
        def bind(self, *a): pass
        def close(self): pass

        def sendto(self, data, addr):
            sent.append((data, addr))
            state["tag"] = json.loads(data.decode())["tag"]

    monkeypatch.setattr(disc.socket, "socket", lambda *a, **k: FakeSock())

    replied = {"done": False}

    async def fake_recvfrom(_sock, _n):
        if replied["done"]:
            raise TimeoutError
        replied["done"] = True
        payload = dict(announced, tag=state["tag"])
        return json.dumps(payload).encode(), peer

    class FakeLoop:
        sock_recvfrom = staticmethod(fake_recvfrom)

    monkeypatch.setattr(disc.asyncio, "get_event_loop", lambda: FakeLoop())
    return await disc.discover(
        timeout_s=0.3, broadcast_addrs=[], include_loopback=False,
        extra_probes=[peer], endpoint_map=endpoint_map)
