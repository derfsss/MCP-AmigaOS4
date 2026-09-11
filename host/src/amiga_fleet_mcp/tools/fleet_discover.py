"""fleet.discover - find MCPd instances on the LAN.

Wraps the UDP discovery transport so it's reachable both as an MCP
tool and from the CLI. Daemons announce themselves; the host learns
each daemon's IP from `recvfrom`'s source-address.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..fleet import Fleet
from ..transports import discovery as discovery_transport


class DiscoveredTarget(BaseModel):
    ip: str
    tcp_port: int
    endpoint: str
    server: str
    protocol: str
    host: str
    methods: int
    latency_ms: int
    #: Name of the configured target this responder turned out to be,
    #: when it answered on a port we forward for one. `None` means a
    #: daemon we found but do not have in the config -- which is the
    #: interesting case for "what else is on this network?".
    target: str | None = None


class FleetDiscoverResult(BaseModel):
    timeout_ms: int
    targets: list[DiscoveredTarget]


def _qemu_probe_plan(
    fleet: Fleet,
) -> tuple[list[tuple[str, int]], dict[tuple[str, int], dict[str, str]]]:
    """Work out where this host's own QEMU guests can be found.

    A guest answers discovery through a `hostfwd` rule, and its
    host-side port cannot be the default one -- two guests would both
    want to bind it and the second QEMU would refuse to start. So each
    guest has its own port, a broadcast never reaches any of them, and
    only the config knows the numbers.

    Returns the extra `(ip, port)` pairs to probe, plus a map from a
    reply's source address back to the endpoint the host should
    actually use, since the daemon announces the port it binds inside
    the guest rather than the forwarded one.
    """
    probes: list[tuple[str, int]] = []
    endpoints: dict[tuple[str, int], dict[str, str]] = {}
    for name, cfg in fleet.config.targets.items():
        if cfg.type != "qemu":
            continue
        ch = cfg.channels.mcpd
        if ch is None or not ch.enabled:
            continue
        host = ch.host or "127.0.0.1"
        disc_port = ch.discovery_port if ch.discovery_port is not None             else ch.port
        probes.append((host, disc_port))
        endpoints[(host, disc_port)] = {
            "endpoint": ch.endpoint,
            "target": name,
        }
    return probes, endpoints


async def fleet_discover(
    fleet: Fleet,
    timeout_ms: int = 1500,
) -> FleetDiscoverResult:
    """Send UDP discovery probes, collect MCPd announcements for
    `timeout_ms`. Returns each daemon as {ip, tcp_port, endpoint,
    server, protocol, host, methods, latency_ms, target},
    sorted fastest-first.

    Probes go to the LAN broadcast addresses *and* to the forwarded
    port of every configured QEMU target, because a guest behind
    `hostfwd` cannot hear a broadcast. A responder that matches one of
    those forwards is reported with the endpoint the host can actually
    reach and the name of the configured target; `target` is `None`
    for anything discovered that the config doesn't already know
    about."""
    probes, endpoints = _qemu_probe_plan(fleet)
    out = await discovery_transport.discover(
        timeout_s=timeout_ms / 1000.0,
        extra_probes=probes,
        endpoint_map=endpoints,
    )
    return FleetDiscoverResult(
        timeout_ms=timeout_ms,
        targets=[DiscoveredTarget(**r) for r in out],
    )
