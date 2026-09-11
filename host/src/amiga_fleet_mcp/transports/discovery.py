"""LAN discovery for MCPd instances.

Sends a UDP broadcast probe to port 4323; collects JSON responses
for a configurable window. The daemon side lives in
mcpd/src/discovery.c.

The host *learns* each daemon's IP from `recvfrom`'s source address;
the daemon never includes its own IP in the response (which it
might not know). The host then composes
`<ip>:<announced tcp_port>` as the MCPd endpoint to use.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import socket
import time
from typing import Any

DISCOVERY_PORT = 4323
DEFAULT_TIMEOUT_S = 1.5
DEFAULT_BROADCAST = "255.255.255.255"

# Plus the common Class-C broadcast for the host's own subnet.
# Some networks block 255.255.255.255 broadcast but allow the
# directed subnet broadcast. We try a few sensible candidates.
_FALLBACK_BROADCASTS = [
    DEFAULT_BROADCAST,
]


def _local_subnet_broadcasts() -> list[str]:
    """Best-effort enumeration of broadcast addresses for IPv4
    interfaces on this host. Falls back to 255.255.255.255 if
    detection fails."""
    out: list[str] = [DEFAULT_BROADCAST]
    try:
        # Resolve our own hostname to discover local IPs.
        hostname = socket.gethostname()
        for entry in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip_field = entry[4][0]
            if not isinstance(ip_field, str):
                continue
            if ip_field.startswith("127."):
                continue
            parts = ip_field.split(".")
            if len(parts) == 4:
                bcast = ".".join([*parts[:3], "255"])
                if bcast not in out:
                    out.append(bcast)
    except (OSError, socket.gaierror):
        pass
    return out


def _make_probe(tag: str) -> bytes:
    return json.dumps({
        "mcp_discovery": 1,
        "v": 1,
        "client": "amiga-fleet-mcp",
        "tag": tag,
    }, separators=(",", ":")).encode("utf-8")


def _parse_response(data: bytes, expected_tag: str) -> dict[str, Any] | None:
    try:
        obj = json.loads(data.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    if obj.get("mcp_discovery") != 1:
        return None
    if obj.get("tag") != expected_tag:
        return None
    return obj


async def discover(
    timeout_s: float = DEFAULT_TIMEOUT_S,
    broadcast_addrs: list[str] | None = None,
    *,
    include_loopback: bool = True,
    extra_probes: list[tuple[str, int]] | None = None,
    endpoint_map: dict[tuple[str, int], dict[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """Send UDP discovery probes; collect responses for `timeout_s`.

    Returns a list of `{ip, tcp_port, endpoint, server, host, methods,
    latency_ms}` deduplicated by `(ip, tcp_port)`. Newer responses
    (lower latency) replace older entries.

    `broadcast_addrs` defaults to 255.255.255.255 plus the host's
    detected /24 subnet broadcasts. `include_loopback` adds
    127.0.0.1 on the default port.

    `extra_probes` are additional `(ip, port)` pairs to probe directly.
    A QEMU guest is reached through a `hostfwd` rule whose host-side
    port cannot be the default: two guests would both want to bind
    4323 and the second QEMU refuses to start. Each guest therefore
    listens on its own host port, and only the caller knows which --
    hence this list. Without it, a broadcast probe finds real hardware
    but never a local QEMU target.

    `endpoint_map` maps a responder's source `(ip, port)` to fields
    that override what it announced -- `{"endpoint": ..., "target":
    ...}`. This matters because the daemon announces the TCP port it
    binds *inside* the guest (4322), which is not where the host can
    reach it: a guest forwarded on 4432 would otherwise be reported as
    `127.0.0.1:4322`, an endpoint that either fails to connect or, on a
    busy workstation, silently lands on a different machine's daemon.
    The source port of the reply identifies the forward, so the caller
    can supply the endpoint it already knows is correct.
    """
    if broadcast_addrs is None:
        broadcast_addrs = _local_subnet_broadcasts()
    endpoint_map = endpoint_map or {}

    tag = secrets.token_hex(8)
    probe = _make_probe(tag)

    loop = asyncio.get_event_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setblocking(False)
    sock.bind(("", 0))  # any free local port

    # Send probes to broadcast addresses and (optionally) loopback,
    # all on the default port, plus any explicit (ip, port) pairs.
    probe_targets: list[tuple[str, int]] = [
        (addr, DISCOVERY_PORT) for addr in broadcast_addrs
    ]
    if include_loopback and ("127.0.0.1", DISCOVERY_PORT) not in probe_targets:
        probe_targets.append(("127.0.0.1", DISCOVERY_PORT))
    for pair in extra_probes or []:
        if pair not in probe_targets:
            probe_targets.append(pair)

    sent_at = time.monotonic()
    for addr, port in probe_targets:
        try:
            sock.sendto(probe, (addr, port))
        except OSError:
            # Some addresses (e.g. unreachable subnet broadcasts)
            # may fail; skip and keep trying others.
            pass

    deadline = sent_at + timeout_s
    seen: dict[tuple[str, int], dict[str, Any]] = {}

    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                data, peer = await asyncio.wait_for(
                    loop.sock_recvfrom(sock, 4096), remaining,
                )
            except TimeoutError:
                break
            except OSError:
                continue
            now = time.monotonic()
            obj = _parse_response(data, tag)
            if obj is None:
                continue
            tcp_port = int(obj.get("tcp_port") or 4322)
            # A reply forwarded out of a QEMU guest arrives from the
            # host-side port of its hostfwd rule, which identifies the
            # target far more reliably than the port the daemon
            # announced from inside the guest.
            override = endpoint_map.get((peer[0], peer[1]), {})
            endpoint = override.get("endpoint") or f"{peer[0]}:{tcp_port}"
            if override.get("endpoint"):
                tcp_port = int(endpoint.rsplit(":", 1)[1])
            key = (peer[0], tcp_port)
            seen[key] = {
                "ip": peer[0],
                "tcp_port": tcp_port,
                "server": obj.get("server", "?"),
                "protocol": obj.get("protocol", "?"),
                "host": obj.get("host", "?"),
                "methods": int(obj.get("methods") or 0),
                "latency_ms": int((now - sent_at) * 1000),
                "endpoint": endpoint,
                "target": override.get("target"),
            }
    finally:
        sock.close()

    # Sort by latency (fastest first), then ip lexicographically.
    return sorted(seen.values(), key=lambda r: (r["latency_ms"], r["ip"]))
