"""Assemble the MCPd release archive *on AmigaOS*, so the protection
bits are right in the file the user downloads.

Why not zip it on the host? Because AmigaOS protection bits are part
of the file, not the byte stream. A raw ELF fetched from a release
page and copied onto an Amiga over SMB, a USB stick, or a browser
download arrives with the `e` (executable) bit protected, and the
daemon then refuses to run for a reason that looks nothing like the
cause. Zip and tar have no concept of those bits either.

LhA does. Building the archive on a real AmigaOS filesystem, with the
bits already set, means `LhA x` restores them on the far side and the
binary is runnable straight out of the archive. The script bit on the
install scripts proves it survived: `s` is never a filesystem
default, so seeing `-s--rwed` after extraction means the flags came
out of the archive rather than from the volume.

The assembly happens on whichever target you point this at — a QEMU
guest is the obvious choice, since it needs no hardware and leaves no
trace.

Usage:
    python scripts/build_release_lha.py --target qemu-pegasos2-sm501
    python scripts/build_release_lha.py --target qemu-pegasos2-sm501 \\
        --version 1.4 --out dist/
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import repo_root

sys.path.insert(0, str(repo_root() / "host" / "src"))

from amiga_fleet_mcp.config import load_config
from amiga_fleet_mcp.fleet import Fleet
from amiga_fleet_mcp.tools import exec as exec_tool
from amiga_fleet_mcp.tools import fs as fs_tool

# Everything the archive ships, in the order it is staged.
SCRIPTS = (
    "MCPd-Install",
    "MCPd-Uninstall",
    "MCPd-Watchdog",
    "MCPd-Enable-Input",
    "MCPd-Disable-Input",
)

STAGE = "RAM:mcpd-dist"
DRAWER = "MCPd"


def daemon_version(root: Path) -> str:
    """Read the version out of rpc.h -- the single source of truth."""
    text = (root / "mcpd" / "src" / "rpc.h").read_text(encoding="utf-8")
    m = re.search(r'#define\s+MCPD_VERSION_STR\s+"([^"]+)"', text)
    if not m:
        raise SystemExit("could not find MCPD_VERSION_STR in mcpd/src/rpc.h")
    return m.group(1)


async def sh(fleet: Fleet, target: str, cmd: str, *,
             cwd: str | None = None, timeout: float = 120.0,
             allow_fail: bool = False) -> str:
    r = await exec_tool.exec_cmd(fleet, target, cmd, cwd=cwd,
                                 timeout_s=timeout)
    if r.exit_code != 0 and not allow_fail:
        raise SystemExit(f"{cmd!r} failed rc={r.exit_code}: {r.output[:400]}")
    return r.output


async def rm(fleet: Fleet, target: str, path: str, *,
             all_: bool = False) -> None:
    """Delete something that may or may not be there.

    AmigaDOS `Delete` returns a warning (5) for a missing object even
    with QUIET, so the tidy-up calls must not treat that as failure.
    """
    # Quoted: an AmigaOS path may legitimately contain a space
    # ("RAM Disk:" being the one everybody has).
    await sh(fleet, target,
             f'Delete >NIL: "{path}"{" ALL" if all_ else ""} QUIET',
             allow_fail=True)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--target", required=True,
                    help="a reachable AmigaOS target to build on")
    ap.add_argument("--version", help="override the version string "
                                      "(default: read from rpc.h)")
    ap.add_argument("--out", default="dist",
                    help="host directory for the finished archive")
    ap.add_argument("--readme",
                    help="README to include in the archive (default: "
                         "mcpd/install/Archive-README)")
    args = ap.parse_args()

    root = repo_root()
    version = args.version or daemon_version(root)
    archive = f"MCPd-{version}.lha"
    binary = root / "mcpd" / "MCPd"
    if not binary.is_file():
        raise SystemExit(f"{binary} not built -- run `make docker-build` "
                         "in mcpd/ first")

    fleet = Fleet(load_config())
    t = args.target
    print(f"building {archive} on {t}", flush=True)

    # Fresh staging tree every run: a leftover file from a previous
    # build would be archived silently.
    await rm(fleet, t, STAGE, all_=True)
    await fs_tool.fs_makedir(fleet, t, STAGE)
    await fs_tool.fs_makedir(fleet, t, f"{STAGE}/{DRAWER}")

    up = await fs_tool.fs_upload(
        fleet, t, local_path=str(binary),
        remote_path=f"{STAGE}/{DRAWER}/MCPd", verify=True)
    print(f"  MCPd {up.bytes_total} bytes, sha256 verified", flush=True)

    for name in SCRIPTS:
        await fs_tool.fs_upload(
            fleet, t, local_path=str(root / "mcpd" / "install" / name),
            remote_path=f"{STAGE}/{DRAWER}/{name}", verify=True)
    print(f"  {len(SCRIPTS)} install scripts", flush=True)

    readme = Path(args.readme) if args.readme else (
        root / "mcpd" / "install" / "Archive-README")
    if readme.is_file():
        await fs_tool.fs_upload(
            fleet, t, local_path=str(readme),
            remote_path=f"{STAGE}/{DRAWER}/README", verify=True)
        print(f"  README (from {readme.name})", flush=True)
    else:
        print(f"  no README at {readme} -- archive will omit it",
              flush=True)

    # The bits that have to survive the trip. The uploads already land
    # as ----rwed; `s` marks the AmigaDOS scripts executable by name.
    await sh(fleet, t, f'Protect {STAGE}/{DRAWER}/MCPd +rwed')
    await sh(fleet, t, f'Protect {STAGE}/{DRAWER}/#?-#? +rwed')
    await sh(fleet, t, f'Protect {STAGE}/{DRAWER}/#?-#? +s')

    await rm(fleet, t, f"RAM:{archive}")
    await sh(fleet, t, f'C:LhA -r -e a RAM:{archive} {DRAWER}', cwd=STAGE)

    # Prove the flags round-trip rather than assuming it: extract into
    # a clean directory and look at what comes out. `s` is never a
    # filesystem default, so it can only have come from the archive.
    await rm(fleet, t, "RAM:mcpd-verify", all_=True)
    await fs_tool.fs_makedir(fleet, t, "RAM:mcpd-verify")
    await sh(fleet, t, f'C:LhA x RAM:{archive} RAM:mcpd-verify/')
    listing = await sh(fleet, t, f'List RAM:mcpd-verify/{DRAWER}')
    print("\n  extracted flags:", flush=True)
    ok = True
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) < 3 or not parts[1].isdigit():
            continue
        name, _size, flags = parts[0], parts[1], parts[2]
        print(f"    {name:22s} {flags}", flush=True)
        if "e" not in flags.split("-")[-1]:
            ok = False
            print("      ^ NOT executable", flush=True)
        if name.startswith("MCPd-") and "s" not in flags:
            ok = False
            print("      ^ script bit missing", flush=True)
    if not ok:
        raise SystemExit("protection bits did not survive the archive")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    local = out_dir / archive
    dl = await fs_tool.fs_download(
        fleet, t, remote_path=f"RAM:{archive}",
        local_path=str(local), verify=True)

    # Leave the target as we found it.
    await rm(fleet, t, STAGE, all_=True)
    await rm(fleet, t, "RAM:mcpd-verify", all_=True)
    await rm(fleet, t, f"RAM:{archive}")

    print(f"\n  {local} ({dl.bytes_total} bytes)", flush=True)
    print(f"  sha256 {dl.sha256}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
