"""Validate round-2 §7-gap additions on the live X5000:

  proto.capabilities    enrichment (build/methods objects/method_names/
                        features/limits/board)
  fs.read               offset + length partial reads
  fs.list               recursive + max_depth
  fs.delete             recursive (Delete ALL QUIET shellout)
  exec.cmd              args[] + cwd

Drops a small tree under RAM: and exercises the enriched surface.
"""

from __future__ import annotations

import asyncio
import base64
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
from amiga_fleet_mcp.tools import exec as exec_tool  # noqa: E402
from amiga_fleet_mcp.tools import fs as fs_tool  # noqa: E402


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
    archive_root = Path(__file__).parent.parent / "tmp" / "round2-archive"
    archive_root.mkdir(parents=True, exist_ok=True)
    fleet = Fleet(make_config(archive_root))
    archive = Archive(archive_root)
    print(f"[setup] archive at {archive.run_dir}")

    target = "x5000-real"
    failures = 0

    print("\n[v] proto.capabilities (enriched)")
    try:
        caps = await fleet.mcpd(target).request("proto.capabilities")
        archive.log_call("proto.capabilities", target, {}, result=caps)
        for k in (
            "server", "protocol", "build", "methods", "method_names",
            "features", "limits", "board",
        ):
            if k not in caps:
                print(f"    FAIL: missing key {k!r}")
                failures += 1
        if "build" in caps:
            print(f"    build: {caps['build']}")
        if "limits" in caps:
            print(f"    limits: {caps['limits']}")
        if "features" in caps:
            print(f"    features: {caps['features']}")
        if "method_names" in caps:
            mn = caps["method_names"]
            print(f"    method_names: {len(mn)} entries; "
                  f"sample={mn[:6]}")
        if "methods" in caps and caps["methods"]:
            m0 = caps["methods"][0]
            if not isinstance(m0, dict) or "name" not in m0:
                print(f"    FAIL: methods[0] not an object: {m0!r}")
                failures += 1
        if "board" in caps:
            print(f"    board: {caps['board']}")
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    print("[v] fs.read offset+length partial read")
    try:
        body = bytes(range(256)) * 4   # 1024 bytes, 0..255 repeated
        TEST = "RAM:r2_read.bin"
        await fs_tool.fs_write(fleet, target, TEST,
                               base64.b64encode(body).decode())
        # Read [200..208) -> 8 bytes starting with 200,201,202,...
        rd = await fs_tool.fs_read(
            fleet, target, TEST, offset=200, length=8,
        )
        archive.log_call("fs.read", target,
                         {"path": TEST, "offset": 200, "length": 8},
                         result=rd.model_dump())
        got = base64.b64decode(rd.content_b64)
        want = body[200:208]
        print(f"    got={got.hex()}  want={want.hex()}")
        print(f"    size={rd.size}  total_size={rd.total_size}  "
              f"offset={rd.offset}")
        if got != want:
            print("    FAIL: content mismatch")
            failures += 1
        if rd.size != 8:
            print(f"    FAIL: size {rd.size} != 8")
            failures += 1
        if rd.total_size != len(body):
            print(f"    FAIL: total_size {rd.total_size} != {len(body)}")
            failures += 1
        await fs_tool.fs_delete(fleet, target, TEST)
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    print("[v] fs.list recursive + max_depth")
    try:
        ROOT = "RAM:r2_tree"
        # Clean any leftover, then build:
        # r2_tree/sub1/leaf.txt
        # r2_tree/sub2/inner/deep.txt
        try: await fs_tool.fs_delete(fleet, target, ROOT, recursive=True)
        except Exception: pass
        await fs_tool.fs_makedir(fleet, target, ROOT)
        await fs_tool.fs_makedir(fleet, target, ROOT + "/sub1")
        await fs_tool.fs_makedir(fleet, target, ROOT + "/sub2")
        await fs_tool.fs_makedir(fleet, target, ROOT + "/sub2/inner")
        leaf = base64.b64encode(b"leaf!").decode()
        deep = base64.b64encode(b"deep!").decode()
        await fs_tool.fs_write(fleet, target, ROOT + "/sub1/leaf.txt", leaf)
        await fs_tool.fs_write(
            fleet, target, ROOT + "/sub2/inner/deep.txt", deep,
        )

        flat = await fs_tool.fs_list(fleet, target, ROOT)
        archive.log_call("fs.list", target, {"path": ROOT},
                         result=[e.model_dump() for e in flat])
        print(f"    flat: {len(flat)} entry/ies "
              f"({[e.name for e in flat]})")
        if len(flat) != 2:
            print(f"    FAIL: expected 2 top-level entries, got {len(flat)}")
            failures += 1

        deep_list = await fs_tool.fs_list(
            fleet, target, ROOT, recursive=True, max_depth=8,
        )
        archive.log_call("fs.list", target,
                         {"path": ROOT, "recursive": True, "max_depth": 8},
                         result=[e.model_dump() for e in deep_list])
        names = sorted(e.name for e in deep_list)
        print(f"    recursive: {len(deep_list)} entry/ies")
        for n in names:
            print(f"      {n}")
        # Expect at least: sub1, sub1/leaf.txt, sub2, sub2/inner,
        # sub2/inner/deep.txt
        wanted = {"sub1/leaf.txt", "sub2/inner/deep.txt"}
        found = set(names)
        if not wanted.issubset(found):
            print(f"    FAIL: missing {wanted - found}")
            failures += 1

        # Recursive delete cleans the tree.
        await fs_tool.fs_delete(fleet, target, ROOT, recursive=True)
        try:
            still = await fs_tool.fs_list(fleet, target, ROOT)
            print(f"    FAIL: tree still exists after recursive delete "
                  f"({len(still)} entries)")
            failures += 1
        except Exception:
            pass  # expected: tree is gone

    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    print("[v] exec.cmd with args[] + cwd")
    try:
        # Echo a tricky arg with spaces+stars+quotes; verify daemon
        # quotes it back through Echo and our captured stdout matches.
        tricky = 'hello world * "quoted" end'
        r = await exec_tool.exec_cmd(
            fleet, target, "Echo",
            args=[tricky], cwd="RAM:",
        )
        archive.log_call(
            "exec.cmd", target,
            {"command": "Echo", "args": [tricky], "cwd": "RAM:"},
            result=r.model_dump(),
        )
        print(f"    rc={r.exit_code}  out={r.output!r}")
        if tricky not in r.output:
            print(f"    FAIL: expected stripped output to contain {tricky!r}")
            failures += 1

        # cwd swap: `Cd` (no args) prints current dir. Verify it ends
        # in RAM (or RAM Disk) when we passed cwd="RAM:".
        r2 = await exec_tool.exec_cmd(
            fleet, target, "Cd", cwd="RAM:",
        )
        archive.log_call(
            "exec.cmd", target, {"command": "Cd", "cwd": "RAM:"},
            result=r2.model_dump(),
        )
        print(f"    Cd-in-RAM:  rc={r2.exit_code}  out={r2.output!r}")
        if "RAM" not in r2.output and "Ram" not in r2.output:
            print("    FAIL: Cd output didn't mention RAM:")
            failures += 1
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    await fleet.close_all()
    print(f"\n[result] failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
