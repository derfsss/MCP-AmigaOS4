"""Install MCPd auto-start on a target via the running MCPd's fs.* methods.

Drops the binary at SYS:System/MCPd/MCPd, makes a one-time backup of
S:Network-Startup, and idempotently appends the launch line. No
SerialShell needed - the running MCPd does the file ops on itself.

Usage:
    python scripts/install_mcpd_autostart.py [host[:port]] [path_to_binary]

Defaults:
    host       $MCPD_ENDPOINT, or required if env unset
    binary     ./mcpd/MCPd
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOST_SRC = HERE.parent / "host" / "src"
sys.path.insert(0, str(HOST_SRC))

from amiga_fleet_mcp.config import (  # noqa: E402
    Config, McpdChannel, PathsConfig, ServerConfig,
    TargetChannels, TargetConfig,
)
from amiga_fleet_mcp.fleet import Fleet  # noqa: E402
from amiga_fleet_mcp.tools import fs as fs_tool  # noqa: E402


MARKER_BARE       = "Run >NIL: <NIL: SYS:System/MCPd/MCPd\n"
MARKER_WATCHDOG   = "Execute SYS:System/MCPd/MCPd-Watchdog"
APPEND_LINES = [
    "",
    "; MCPd - Model Context Protocol daemon (auto-installed, watchdog'd)",
    "Run >NIL: <NIL: Execute SYS:System/MCPd/MCPd-Watchdog",
]
WATCHDOG_LOCAL = HERE.parent / "mcpd" / "install" / "MCPd-Watchdog"


def make_config(endpoint: str) -> Config:
    return Config(
        server=ServerConfig(archive_root=HERE.parent / "tmp" / "install"),
        paths=PathsConfig(),
        targets={
            "target": TargetConfig(
                type="remote", display_name="target",
                channels=TargetChannels(
                    mcpd=McpdChannel(endpoint=endpoint),
                ),
            ),
        },
    )


async def _exists(fleet: Fleet, target: str, path: str) -> bool:
    try:
        await fs_tool.fs_stat(fleet, target, path)
        return True
    except Exception:
        return False


async def _read_text(fleet: Fleet, target: str, path: str) -> str:
    r = await fs_tool.fs_read(fleet, target, path)
    return base64.b64decode(r.content_b64).decode("latin-1")


async def _write_text(
    fleet: Fleet, target: str, path: str, text: str,
) -> None:
    b64 = base64.b64encode(text.encode("latin-1")).decode()
    await fs_tool.fs_write(fleet, target, path, b64)


async def install(
    fleet: Fleet, target: str, binary_path: Path,
) -> None:
    print(f"[install] target={target}  binary={binary_path}  "
          f"size={binary_path.stat().st_size}")

    print("[1/5] MakeDir SYS:System/MCPd")
    try:
        await fs_tool.fs_makedir(fleet, target, "SYS:System/MCPd")
    except Exception as e:
        # already-exists is OK on AOS DOS error 203
        msg = str(e)
        if "already" not in msg.lower() and "203" not in msg:
            # try fs_stat to confirm it exists; if so, swallow
            if not await _exists(fleet, target, "SYS:System/MCPd"):
                raise
        print(f"      (already exists or benign: {msg[:100]})")

    print("[2/5] Upload binary -> SYS:System/MCPd/MCPd")
    blob = binary_path.read_bytes()
    await fs_tool.fs_write(
        fleet, target, "SYS:System/MCPd/MCPd",
        base64.b64encode(blob).decode(),
    )
    # AmigaOS protection bits 0..3 are inverted (1 = protected). New
    # files default to E=protected, which makes them non-executable.
    # Clear it via the Protect +rwed shell command - simpler than
    # computing the right mask.
    from amiga_fleet_mcp.tools import exec as exec_tool
    rc = await exec_tool.exec_cmd(
        fleet, target, "Protect",
        args=["SYS:System/MCPd/MCPd", "+rwed"],
    )
    if rc.exit_code != 0:
        raise RuntimeError(
            f"Protect +rwed failed: rc={rc.exit_code} out={rc.output!r}"
        )
    print(f"      wrote {len(blob)} bytes, Protect +rwed (rc={rc.exit_code})")

    print("[3/6] Upload watchdog -> SYS:System/MCPd/MCPd-Watchdog")
    if not WATCHDOG_LOCAL.exists():
        raise RuntimeError(f"missing watchdog script {WATCHDOG_LOCAL}")
    wd_blob = WATCHDOG_LOCAL.read_bytes()
    await fs_tool.fs_write(
        fleet, target, "SYS:System/MCPd/MCPd-Watchdog",
        base64.b64encode(wd_blob).decode(),
    )
    print(f"      wrote {len(wd_blob)} bytes")

    print("[4/6] Backup S:Network-Startup -> S:Network-Startup.before-mcpd "
          "(if missing)")
    if await _exists(fleet, target, "S:Network-Startup.before-mcpd"):
        print("      backup already present")
    else:
        ns_text = await _read_text(fleet, target, "S:Network-Startup")
        await _write_text(
            fleet, target, "S:Network-Startup.before-mcpd", ns_text,
        )
        print("      backup written")

    print("[5/6] Patch S:Network-Startup (replace bare-MCPd with watchdog "
          "if present, else append)")
    cur = await _read_text(fleet, target, "S:Network-Startup")
    if MARKER_WATCHDOG in cur:
        print(f"      already using watchdog launcher - skip")
    elif MARKER_BARE in cur:
        # Old install: replace the bare line with the watchdog line
        new = cur.replace(MARKER_BARE, APPEND_LINES[2] + "\n")
        await _write_text(fleet, target, "S:Network-Startup", new)
        print(f"      replaced bare-MCPd line with watchdog launcher")
    else:
        new = cur.rstrip("\r\n") + "\n" + "\n".join(APPEND_LINES) + "\n"
        await _write_text(fleet, target, "S:Network-Startup", new)
        print(f"      appended {len(APPEND_LINES)-1} lines")

    print("[6/6] Verify")
    after = await _read_text(fleet, target, "S:Network-Startup")
    if MARKER_WATCHDOG not in after:
        raise RuntimeError(
            "post-write S:Network-Startup is missing watchdog marker"
        )
    print("      OK - S:Network-Startup uses the watchdog launcher")

    print()
    print("[done] Install complete.")
    print("       To activate now: reboot the target.")
    print("       To activate without rebooting:")
    print("         Run >NIL: <NIL: SYS:System/MCPd/MCPd  (kill any "
          "existing instance first)")


async def amain(argv: list[str]) -> int:
    endpoint = (
        argv[1] if len(argv) > 1
        else os.environ.get("MCPD_ENDPOINT")
    )
    if not endpoint:
        print("ERROR: pass <host[:port]> as first argument or set "
              "$MCPD_ENDPOINT.")
        return 2
    if ":" not in endpoint:
        endpoint += ":4322"
    binary = (Path(argv[2]) if len(argv) > 2
              else HERE.parent / "mcpd" / "MCPd")
    if not binary.exists():
        print(f"ERROR: binary not found at {binary}")
        return 2

    fleet = Fleet(make_config(endpoint))
    try:
        await install(fleet, "target", binary)
    finally:
        await fleet.close_all()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain(sys.argv)))
