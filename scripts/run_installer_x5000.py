"""Direct-import wrapper around installer.run for X5000.

Streams per-step status as the install runs.

Configuration:
  --target         Fleet target name (default: x5000)
  --dest-volume    AmigaDOS volume to install to (default: BootTest:)
  --sources-dir    Host directory containing the ISO + LHAs +
                   MCPd / SerialShell / diskimage-bootstrap/
                   (default: $AMIGA_INSTALL_SOURCES, or required)
  --machine        Machine ID (default: X5000)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "host" / "src"))

from amiga_fleet_mcp.config import load_config
from amiga_fleet_mcp.fleet import Fleet
from amiga_fleet_mcp.tools import installer as it


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="x5000")
    ap.add_argument("--dest-volume", default="BootTest:")
    ap.add_argument("--sources-dir",
                    default=os.environ.get("AMIGA_INSTALL_SOURCES"),
                    help="Host dir with ISO + LHAs (default "
                         "$AMIGA_INSTALL_SOURCES; required)")
    ap.add_argument("--machine", default="X5000")
    args = ap.parse_args()
    if not args.sources_dir:
        ap.error("--sources-dir or $AMIGA_INSTALL_SOURCES is required")

    cfg = load_config()
    fleet = Fleet(cfg)

    print(f"=== installer.run {args.machine} -> {args.target}:"
          f"{args.dest_volume} (LIVE) ===\n")
    t0 = time.monotonic()

    res = await it.installer_run(
        fleet, args.target,
        dest_volume=args.dest_volume,
        machine=args.machine,
        sources_dir=args.sources_dir,
        dry_run=False, confirm=True,
    )

    elapsed = time.monotonic() - t0
    print(f"\n=== install run summary ===")
    print(f"machine        : {res.machine}")
    print(f"overall        : {res.overall}")
    print(f"total_duration : {res.total_duration_s:.1f} s "
          f"(wall: {elapsed:.1f} s)")
    print(f"failed_step    : {res.failed_step}")
    print(f"\n  Per-step:")
    for i, s in enumerate(res.steps):
        marker = "+" if s.status == "ok" else (
                 "X" if s.status == "fail" else "?")
        line = f"   [{marker}] {i+1:2d}. {s.name:30s}"
        line += f" {s.duration_s:6.1f}s"
        line += f"  status={s.status}"
        print(line)
        if s.error:
            print(f"        error: {s.error}")

    if res.overall == "fail":
        print(f"\n*** install FAILED at step {res.failed_step!r}")
        # Try to clean up the ISO mount if anything got that far
        try:
            await it.installer_unmount_iso(fleet, "x5000", unit=50)
            print(f"*** unmounted COMBI: unit 50 (cleanup)")
        except Exception:
            pass
        return 1

    print(f"\n*** install SUCCESS — BootTest: now contains a complete "
          f"AOS 4.1 FE install for X5000. Next: installer.verify.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
