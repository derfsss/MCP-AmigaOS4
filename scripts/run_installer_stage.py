"""Direct-import wrapper around installer.stage.

Configuration:
  --target         Fleet target name (default: x5000)
  --dest-volume    AmigaDOS volume to stage into (default: BootTest:)
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

    res = await it.installer_stage(
        fleet, args.target,
        dest_volume=args.dest_volume,
        sources_dir=args.sources_dir,
        machine=args.machine,
        confirm=True,
    )

    print(f"=== installer.stage result ===")
    print(f"machine        : {res.machine}")
    print(f"iso_filename   : {res.iso_filename}")
    print(f"staging_dir    : {res.staging_dir}")
    print(f"files_staged   : {len(res.files_staged)}")
    print(f"total_bytes    : {res.total_bytes:,}")
    print(f"total_compr    : {res.total_compressed:,}")
    print(f"total_elapsed_s: {res.total_elapsed_s:.1f}")
    print(f"skipped        : {len(res.skipped)}")
    for s in res.skipped:
        print(f"   - {s}")
    print(f"\n  Per-file:")
    for f in res.files_staged:
        ratio = f.compression_ratio
        mb_total = f.bytes_total / (1024 * 1024)
        mb_compr = f.bytes_sent_compressed / (1024 * 1024)
        speed = (f.bytes_total / (1024 * 1024) / f.elapsed_s) \
                if f.elapsed_s > 0.01 else 0
        print(f"   [{f.role:9s}] {f.dst}")
        print(f"               {mb_total:7.1f} MiB raw  ->  "
              f"{mb_compr:7.1f} MiB on wire ({ratio:.2f})  "
              f"{f.elapsed_s:5.1f} s  {speed:5.1f} MiB/s")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
