"""End-to-end validation of the new serial.* tools against the live X5000.

Imports the same functions the MCP tool decorators wrap, drives a full
start -> reboot -> capture -> read -> stop cycle, and decodes the
captured bytes to check we see the U-Boot banner.
"""

from __future__ import annotations

import asyncio
import base64
import socket
import struct
import time
import sys

sys.path.insert(0, "host/src")

from amiga_fleet_mcp.config import load_config
from amiga_fleet_mcp.fleet import Fleet
from amiga_fleet_mcp.tools import serial as st


def _send_reboot_via_mcpd(host: str, port: int) -> None:
    """Hit the live MCPd directly via TCP (avoids needing the host-side
    MCP server running) and fire `Run >NIL: <NIL: C:Reboot`."""
    import json
    s = socket.create_connection((host, port), timeout=10)
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "exec.cmd",
        "params": {"command": "Run >NIL: <NIL: C:Reboot",
                   "timeout_ms": 5000},
    }, separators=(",", ":")).encode()
    s.sendall(struct.pack(">I", len(body)) + body)
    # Drain the response so the daemon flushes before reboot.
    hdr = s.recv(4)
    if len(hdr) == 4:
        (n,) = struct.unpack(">I", hdr)
        s.recv(n)
    s.close()


async def main() -> int:
    import argparse
    import os
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="x5000",
                    help="Fleet target name (default: x5000)")
    ap.add_argument("--mcpd-endpoint",
                    default=os.environ.get("MCPD_ENDPOINT"),
                    help="MCPd endpoint host:port for the reboot "
                         "trigger (default $MCPD_ENDPOINT; required)")
    args = ap.parse_args()
    if not args.mcpd_endpoint:
        ap.error("--mcpd-endpoint or $MCPD_ENDPOINT is required")
    reboot_host, _, reboot_port_s = args.mcpd_endpoint.partition(":")
    reboot_port = int(reboot_port_s) if reboot_port_s else 4322

    cfg = load_config()
    fleet = Fleet(cfg)

    target = args.target
    print(f"=== validating serial.* tools against {target} ===")

    # 1. start (truncate=True so we get a clean log)
    res = await st.serial_start(fleet, target, channel="uboot",
                                truncate=True)
    print(f"[start] port={res.port} baud={res.baud} "
          f"was_running={res.was_running} truncated={res.truncated}")
    print(f"        log_path={res.log_path}")

    # 2. status
    s_after_start = await st.serial_status(fleet, target=target)
    print(f"[status] {len(s_after_start.captures)} captures: "
          f"{[(c.channel, c.running) for c in s_after_start.captures]}")

    # 3. trigger reboot via direct MCPd TCP
    print("[reboot] firing C:Reboot via MCPd...")
    _send_reboot_via_mcpd(reboot_host, reboot_port)

    # 4. wait for boot output to settle
    print("[wait] watching log file size...")
    last = -1
    stable_ticks = 0
    deadline = time.monotonic() + 120.0
    cap = fleet.serial_captures.get(target, "uboot")
    assert cap is not None
    while time.monotonic() < deadline:
        await asyncio.sleep(3.0)
        size = cap.file_size()
        if size != last:
            print(f"  ... {size} bytes (running={cap.running})")
            last = size
            stable_ticks = 0
        else:
            stable_ticks += 1
            if stable_ticks >= 4 and size > 1000:
                break

    # 5. read first 4 KiB and decode
    rd = await st.serial_read(fleet, target, channel="uboot",
                              offset=0, max_bytes=4096)
    decoded = base64.b64decode(rd.bytes_b64)
    txt = decoded.decode("ascii", errors="replace")
    print(f"[read] returned {len(decoded)}B "
          f"(file_size={rd.file_size}, next_offset={rd.next_offset}, "
          f"running={rd.running})")
    print(f"[read] first 6 ascii lines:")
    for line in txt.splitlines()[:6]:
        print(f"  | {line}")

    # 6. tail last 1 KiB
    tl = await st.serial_tail(fleet, target, channel="uboot",
                              max_bytes=1024)
    tail_txt = base64.b64decode(tl.bytes_b64).decode("ascii",
                                                    errors="replace")
    print(f"[tail] last 1KiB ascii (final 4 lines):")
    for line in tail_txt.splitlines()[-4:]:
        print(f"  | {line}")

    # 7. stop
    sp = await st.serial_stop(fleet, target, channel="uboot")
    print(f"[stop] stopped={sp.stopped} total_bytes={sp.total_bytes} "
          f"duration_s={sp.duration_s:.1f}")

    # 8. status after stop (running should be False)
    s_after = await st.serial_status(fleet, target=target)
    print(f"[status] {len(s_after.captures)} captures: "
          f"{[(c.channel, c.running) for c in s_after.captures]}")

    # 9. simple verdict
    ok = ("U-Boot" in txt and rd.file_size > 1000
          and not s_after.captures[0].running)
    print(f"\n*** verdict: {'PASS' if ok else 'FAIL'} ***")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
