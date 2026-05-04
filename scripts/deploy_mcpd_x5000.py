"""Deploy the freshly-built MCPd binary to a target's
SYS:System/MCPd/ install location.

After this script, REBOOT the target so the new binary loads. AOS4's
SmartFilesystem allows overwriting a running binary on disk; the
running process keeps using the in-memory copy until next launch.

Configuration:
  --endpoint <host:port>   MCPd endpoint (default $MCPD_ENDPOINT; required)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "host" / "src"))

from amiga_fleet_mcp.config import (
    Config, McpdChannel, TargetChannels, TargetConfig,
)
from amiga_fleet_mcp.fleet import Fleet
from amiga_fleet_mcp.upload import chunked_upload


LOCAL = HERE.parent / "mcpd" / "MCPd"
REMOTE = "SYS:System/MCPd/MCPd"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default=os.environ.get("MCPD_ENDPOINT"),
                    help="MCPd endpoint (default $MCPD_ENDPOINT; required)")
    args = ap.parse_args()
    if not args.endpoint:
        ap.error("--endpoint or $MCPD_ENDPOINT is required")

    if not LOCAL.is_file():
        print(f"local binary missing: {LOCAL}")
        return 2

    cfg = Config(targets={
        "x5000": TargetConfig(
            type="remote",
            channels=TargetChannels(
                mcpd=McpdChannel(endpoint=args.endpoint),
            ),
        )
    })
    fleet = Fleet(cfg)
    target = "x5000"

    print(f"deploying {LOCAL} -> {target}:{REMOTE}")
    print(f"  size: {LOCAL.stat().st_size:,} bytes")

    stats = await chunked_upload(
        fleet, target, str(LOCAL), REMOTE,
        chunk_size=16 * 1024 * 1024,
        compression="auto",
    )
    print(f"  done: {stats.bytes_total:,}B raw / "
          f"{stats.bytes_sent_compressed:,}B on wire / "
          f"{stats.elapsed_s:.2f}s "
          f"(ratio={stats.compression_ratio:.2f})")

    # Verify
    mcpd = fleet.mcpd(target)
    res = await mcpd.request("fs.stat", {"path": REMOTE})
    print(f"\n  verified: {res}")

    print("\n*** REBOOT X5000 to load the new MCPd ***")
    print("    After reboot, sys.read_ccsr will be available.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
