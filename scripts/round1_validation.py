"""Validate round-1 §7-gap additions on the live X5000:

  sys.uptime / sys.memory / sys.volumes / sys.assigns
  wb.publicscreens / wb.frontmost
  fs.hash
  fleet.relay (between QEMU + X5000 - that needs both online; we
  exercise the X5000-only `fs.hash` here and leave fleet.relay
  for a separate cross-host test)
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOST_SRC = HERE.parent / "host" / "src"
sys.path.insert(0, str(HOST_SRC))

from amiga_fleet_mcp.archive import Archive  # noqa: E402
from amiga_fleet_mcp.config import (  # noqa: E402
    Config, McpdChannel, PathsConfig, ServerConfig,
    TargetChannels, TargetConfig,
)
from amiga_fleet_mcp.fleet import Fleet  # noqa: E402
from amiga_fleet_mcp.tools import fs as fs_tool  # noqa: E402
from amiga_fleet_mcp.tools import sys as sys_tool  # noqa: E402
from amiga_fleet_mcp.tools import wb as wb_tool  # noqa: E402


import os
X5000_ENDPOINT = os.environ.get("MCPD_ENDPOINT", "127.0.0.1:4322")


def make_config(archive_root: Path) -> Config:
    return Config(
        server=ServerConfig(archive_root=archive_root),
        paths=PathsConfig(),
        targets={
            "x5000-real": TargetConfig(
                type="remote", display_name="X5000",
                channels=TargetChannels(
                    mcpd=McpdChannel(endpoint=X5000_ENDPOINT),
                ),
            ),
        },
    )


async def amain() -> int:
    archive_root = Path(__file__).parent.parent / "tmp" / "round1-archive"
    archive_root.mkdir(parents=True, exist_ok=True)
    fleet = Fleet(make_config(archive_root))
    archive = Archive(archive_root)
    print(f"[setup] archive at {archive.run_dir}")

    target = "x5000-real"
    failures = 0

    print("\n[v] sys.uptime")
    try:
        u = await sys_tool.sys_uptime(fleet, target)
        archive.log_call("sys.uptime", target, {}, result=u.model_dump())
        print(f"    uptime: {u.seconds:.1f}s ({u.seconds/3600:.2f}h), "
              f"eclock_freq={u.eclock_freq}")
        if u.seconds <= 0 or u.eclock_freq <= 0:
            print("    FAIL: bogus uptime values")
            failures += 1
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    print("[v] sys.memory")
    try:
        m = await sys_tool.sys_memory(fleet, target)
        archive.log_call("sys.memory", target, {}, result=m.model_dump())
        print(f"    any: free={m.any.free:>10}  largest={m.any.largest:>10}  "
              f"total={m.any.total:>10}")
        print(f"    shared: free={m.shared.free:>10}  total={m.shared.total:>10}")
        print(f"    virtual: free={m.virtual.free:>10}  total={m.virtual.total:>10}")
        if m.any.free <= 0:
            print("    FAIL: any.free is zero")
            failures += 1
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    print("[v] sys.volumes")
    try:
        vols = await sys_tool.sys_volumes(fleet, target)
        archive.log_call("sys.volumes", target, {},
                         result=[v.model_dump() for v in vols])
        print(f"    {len(vols)} volume(s)")
        for v in vols[:8]:
            print(f"      {v.name:18}  type={v.type}  port=0x{v.port:08x}")
        if len(vols) < 1:
            print("    FAIL: at least one mounted volume expected")
            failures += 1
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    print("[v] sys.assigns")
    try:
        assigns = await sys_tool.sys_assigns(fleet, target)
        archive.log_call("sys.assigns", target, {},
                         result=[a.model_dump() for a in assigns])
        print(f"    {len(assigns)} assign(s)")
        for a in assigns[:8]:
            print(f"      {a.name:18}  type={a.type}")
        if len(assigns) < 5:
            print(f"    FAIL: surprisingly few assigns ({len(assigns)})")
            failures += 1
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    print("[v] wb.publicscreens")
    try:
        ps = await wb_tool.wb_publicscreens(fleet, target)
        archive.log_call("wb.publicscreens", target, {},
                         result=[p.model_dump() for p in ps])
        print(f"    {len(ps)} public screen(s)")
        for p in ps:
            print(f"      pri={p.priority:>4}  name={p.name!r}")
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    print("[v] wb.frontmost")
    try:
        fm = await wb_tool.wb_frontmost(fleet, target)
        archive.log_call("wb.frontmost", target, {}, result=fm.model_dump())
        if fm.frontmost_screen:
            fs = fm.frontmost_screen
            print(f"    frontmost: {fs.title!r}  {fs.width}x{fs.height}")
        if fm.active_window:
            w = fm.active_window
            print(f"    active window: {w.title!r}  {w.width}x{w.height} "
                  f"@ ({w.left},{w.top})")
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    print("[v] fs.hash sha256 round-trip")
    try:
        # Write a known blob, read back via fs.hash, compare with
        # local hashlib computation.
        test_blob = (b"hash test " * 100) + b"\x00\x01\x02\xff"
        TEST = "RAM:hash_test.bin"
        await fs_tool.fs_write(fleet, target, TEST,
                               base64.b64encode(test_blob).decode())
        h = await fs_tool.fs_hash(fleet, target, TEST)
        archive.log_call("fs.hash", target, {"path": TEST},
                         result=h.model_dump())
        local_hash = hashlib.sha256(test_blob).hexdigest()
        print(f"    daemon: {h.hash}  size={h.size}")
        print(f"    local : {local_hash}")
        if h.hash != local_hash:
            print("    FAIL: hash mismatch")
            failures += 1
        if h.size != len(test_blob):
            print(f"    FAIL: size {h.size} != {len(test_blob)}")
            failures += 1
        try: await fs_tool.fs_delete(fleet, target, TEST)
        except Exception: pass
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    await fleet.close_all()
    print(f"\n[result] failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
