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
) -> list[dict[str, Any]]:
    """Send UDP discovery probes; collect responses for `timeout_s`.

    Returns a list of `{ip, tcp_port, server, host, methods,
    latency_ms, source_broadcast}` deduplicated by `(ip, tcp_port)`.
    Newer responses (lower latency) replace older entries.

    `broadcast_addrs` defaults to 255.255.255.255 plus the host's
    detected /24 subnet broadcasts. `include_loopback` adds
    127.0.0.1 directly so QEMU hostfwd-bound MCPds also show up.
    """
    if broadcast_addrs is None:
        broadcast_addrs = _local_subnet_broadcasts()

    tag = secrets.token_hex(8)
    probe = _make_probe(tag)

    loop = asyncio.get_event_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.setblocking(False)
    sock.bind(("", 0))  # any free local port

    # Send probes to broadcast addresses and (optionally) loopback.
    targets: list[str] = list(broadcast_addrs)
    if include_loopback and "127.0.0.1" not in targets:
        targets.append("127.0.0.1")

    sent_at = time.monotonic()
    for addr in targets:
        try:
            sock.sendto(probe, (addr, DISCOVERY_PORT))
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
            key = (peer[0], tcp_port)
            seen[key] = {
                "ip": peer[0],
                "tcp_port": tcp_port,
                "server": obj.get("server", "?"),
                "protocol": obj.get("protocol", "?"),
                "host": obj.get("host", "?"),
                "methods": int(obj.get("methods") or 0),
                "latency_ms": int((now - sent_at) * 1000),
                "endpoint": f"{peer[0]}:{tcp_port}",
            }
    finally:
        sock.close()

    # Sort by latency (fastest first), then ip lexicographically.
    return sorted(seen.values(), key=lambda r: (r["latency_ms"], r["ip"]))
