"""Fetch SYS:Kickstart/ tree from X5000 to host (for QEMU bring-up).

Walks the directory recursively via fs.list, downloads each file in
16 MiB base64 chunks via fs.read(offset/length), preserves the
directory hierarchy under <out_dir>/Kickstart/.

Usage:
    python scripts/fetch_x5000_kickstart.py [--out s:/temp/x5000_kickstart]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import sys
import time
from pathlib import Path

sys.path.insert(0, "host/src")

from amiga_fleet_mcp.config import (
    Config, McpdChannel, TargetChannels, TargetConfig,
)
from amiga_fleet_mcp.fleet import Fleet


CHUNK = 16 * 1024 * 1024  # 16 MiB raw per fs.read; well under 32 MiB frame cap


async def list_recursive(mcpd, root: str) -> list[dict]:
    """Walk the tree iteratively (fs.list returns a list of entries)."""
    out: list[dict] = []
    todo: list[str] = [root]
    seen: set[str] = set()
    while todo:
        d = todo.pop()
        if d in seen:
            continue
        seen.add(d)
        try:
            res = await mcpd.request("fs.list", {"path": d})
        except Exception as e:
            print(f"  ! fs.list {d}: {e}")
            continue
        # res is a list per fs.py
        entries = res if isinstance(res, list) else res.get("entries", [])
        for e in entries:
            name = e.get("name") or ""
            t = e.get("type") or ""
            sub = d.rstrip("/") + "/" + name if d.endswith(":") else f"{d}/{name}"
            # AmigaDOS path: VOL:Path/Sub  -- when d is "VOL:" don't add another /
            if d.endswith(":"):
                sub = f"{d}{name}"
            full = sub
            row = {"path": full, "type": t, "size": int(e.get("size") or 0)}
            out.append(row)
            if t == "dir":
                todo.append(full)
    return out


def amiga_to_local_rel(amiga_path: str, root: str) -> str:
    """Convert SYS:Kickstart/Modules/foo to Modules/foo relative path."""
    if amiga_path.startswith(root):
        rel = amiga_path[len(root):]
    else:
        rel = amiga_path
    rel = rel.lstrip("/").lstrip(":")
    return rel.replace(":", "_")


async def fetch_file(mcpd, amiga_path: str, dest: Path, size: int) -> int:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if size == 0:
        # zero-byte file -- just create it
        dest.write_bytes(b"")
        return 0
    # Download in chunks
    written = 0
    with open(dest, "wb") as f:
        while written < size:
            length = min(CHUNK, size - written)
            res = await mcpd.request(
                "fs.read",
                {"path": amiga_path, "offset": written, "length": length},
                timeout_s=180.0,
            )
            data = base64.b64decode(res["content_b64"])
            f.write(data)
            written += len(data)
            if len(data) == 0:
                # short read; stop to avoid infinite loop
                break
    return written


async def main() -> int:
    import os
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="x5000")
    ap.add_argument("--endpoint", default=os.environ.get("MCPD_ENDPOINT"),
                    help="MCPd endpoint (default $MCPD_ENDPOINT; required)")
    ap.add_argument("--root", default="SYS:Kickstart")
    ap.add_argument("--out", required=True,
                    help="Local directory to write the fetched tree")
    args = ap.parse_args()
    if not args.endpoint:
        ap.error("--endpoint or $MCPD_ENDPOINT is required")

    cfg = Config(targets={
        args.target: TargetConfig(
            type="remote",
            channels=TargetChannels(
                mcpd=McpdChannel(endpoint=args.endpoint),
            ),
        ),
    })
    fleet = Fleet(cfg)
    mcpd = fleet.mcpd(args.target)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"enumerating {args.root} (recursive)...")
    t0 = time.time()
    items = await list_recursive(mcpd, args.root)
    files = [i for i in items if i["type"] == "file"]
    dirs = [i for i in items if i["type"] == "dir"]
    total_bytes = sum(f["size"] for f in files)
    print(f"  {len(files)} files, {len(dirs)} dirs, {total_bytes:,} bytes total")
    print(f"  enumerate took {time.time()-t0:.1f}s")

    # Pre-create directories so we don't have to in fetch loop
    for d in dirs:
        rel = amiga_to_local_rel(d["path"], args.root)
        if rel:
            (out_dir / rel).mkdir(parents=True, exist_ok=True)

    print(f"\ndownloading {len(files)} files to {out_dir}/")
    t0 = time.time()
    fetched = 0
    bytes_done = 0
    for i, f in enumerate(files, 1):
        rel = amiga_to_local_rel(f["path"], args.root)
        dst = out_dir / rel
        try:
            n = await fetch_file(mcpd, f["path"], dst, f["size"])
            bytes_done += n
            fetched += 1
            print(f"  [{i:>3}/{len(files)}] {f['path']:<60} {f['size']:>12,} B")
        except Exception as e:
            print(f"  [{i:>3}/{len(files)}] {f['path']}  FAILED: {e}")

    elapsed = time.time() - t0
    print(f"\ndone: {fetched}/{len(files)} files, {bytes_done:,} bytes in {elapsed:.1f}s "
          f"({bytes_done/1024/1024/max(0.001,elapsed):.2f} MiB/s)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
