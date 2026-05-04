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


class FleetDiscoverResult(BaseModel):
    timeout_ms: int
    targets: list[DiscoveredTarget]


async def fleet_discover(
    fleet: Fleet,  # accepted for symmetry / future use
    timeout_ms: int = 1500,
) -> FleetDiscoverResult:
    """Send a UDP discovery broadcast, collect MCPd announcements
    for `timeout_ms`. Returns each daemon as
    {ip, tcp_port, endpoint, server, protocol, host, methods,
    latency_ms}, sorted fastest-first."""
    out = await discovery_transport.discover(timeout_s=timeout_ms / 1000.0)
    return FleetDiscoverResult(
        timeout_ms=timeout_ms,
        targets=[DiscoveredTarget(**r) for r in out],
    )
