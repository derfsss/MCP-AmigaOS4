"""Validate round-3 SDK-survey additions on the live X5000:

  sys.hardware                CPU + AttnFlags + resource probes
  proto.capabilities.board    real (not "unknown") detection

Cross-checks:
  - sys.hardware.cpu.family/model match documented X5000 / Cyrus Plus
    config (Freescale P5020 = E5500 family, P50XX model).
  - proto.capabilities.board returns "x5000" (not "unknown").
"""

from __future__ import annotations

import asyncio
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
from amiga_fleet_mcp.tools import sys as sys_tool  # noqa: E402


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
    archive_root = Path(__file__).parent.parent / "tmp" / "round3-archive"
    archive_root.mkdir(parents=True, exist_ok=True)
    fleet = Fleet(make_config(archive_root))
    archive = Archive(archive_root)
    print(f"[setup] archive at {archive.run_dir}")

    target = "x5000-real"
    failures = 0

    print("\n[v] sys.hardware")
    try:
        hw = await sys_tool.sys_hardware(fleet, target)
        archive.log_call("sys.hardware", target, {}, result=hw.model_dump())
        print(f"    attn_flags=0x{hw.attn_flags:08x} "
              f"({', '.join(hw.attn_flags_decoded)})")
        print(f"    cpu: family={hw.cpu.family} ({hw.cpu.family_id}) "
              f"model={hw.cpu.model} ({hw.cpu.model_id})")
        print(f"    cpu_string: {hw.cpu.model_string!r}")
        print(f"    speed_hz={hw.cpu.speed_hz:>13}  fsb_hz={hw.cpu.fsb_hz:>13}  "
              f"timebase={hw.cpu.timebase_hz}")
        print(f"    L1={hw.cpu.l1_cache}  L2={hw.cpu.l2_cache}  "
              f"L3={hw.cpu.l3_cache}  cache_line={hw.cpu.cache_line}  "
              f"page={hw.cpu.page_size}")
        print(f"    resources: xena={hw.resources.xena}  "
              f"i2c={hw.resources.i2c}  acpi={hw.resources.acpi}  "
              f"perfmon={hw.resources.performancemonitor}  "
              f"fsldma={hw.resources.fsldma}")

        # Sanity: X5000 is Freescale P5020, E5500 family, model P50XX,
        # ~2 GHz, L1=32K, L2=512K.
        if hw.cpu.family != "E5500":
            print(f"    FAIL: expected family=E5500 on X5000, got "
                  f"{hw.cpu.family!r}")
            failures += 1
        if hw.cpu.model != "P50XX":
            print(f"    FAIL: expected model=P50XX on X5000, got "
                  f"{hw.cpu.model!r}")
            failures += 1
        if not (1.5e9 < hw.cpu.speed_hz < 2.5e9):
            print(f"    FAIL: speed {hw.cpu.speed_hz} not in 1.5-2.5GHz")
            failures += 1
        if hw.cpu.l1_cache <= 0 or hw.cpu.l2_cache <= 0:
            print("    FAIL: L1/L2 cache reported 0")
            failures += 1
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    print("[v] proto.capabilities.board")
    try:
        caps = await fleet.mcpd(target).request("proto.capabilities")
        board = caps.get("board", {})
        archive.log_call("proto.capabilities", target, {},
                         result={"board": board})
        print(f"    detected: {board.get('detected')!r}")
        if board.get("detected") != "x5000":
            print(f"    FAIL: expected 'x5000', got {board.get('detected')!r}")
            failures += 1
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    await fleet.close_all()
    print(f"\n[result] failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
