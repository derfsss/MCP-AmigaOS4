"""Drive QEMU pegasos2 and the real X5000 simultaneously.

Both targets must auto-start MCPd at boot. The script:
  1. Verifies the X5000 MCPd is up.
  2. Launches QEMU pegasos2.
  3. Waits for QEMU's MCPd on 127.0.0.1.
  4. Runs a fan-out across both via fleet.run_on_all.
  5. Confirms board detection differs.
  6. fleet.relay a file between them.
  7. fleet.barrier sync point.

Configuration:
  --x5000 <ip>           X5000 IP (default $X5000_HOST or required)
  --qemu-binary <path>   qemu-system-ppc binary (default $QEMU_BINARY
                         or `qemu-system-ppc` on PATH)
  --peg2-config <path>   QEMU pegasos2 config.json (required)
  $QEMU_RUNNER_DIR       qemu-runner checkout
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOST_SRC = HERE.parent / "host" / "src"
sys.path.insert(0, str(HOST_SRC))
sys.path.insert(0, str(HERE))

from _paths import find_qemu_runner  # noqa: E402
from amiga_fleet_mcp.archive import Archive  # noqa: E402
from amiga_fleet_mcp.config import (  # noqa: E402
    Config, McpdChannel, PathsConfig, QmpChannel,
    ServerConfig, TargetChannels, TargetConfig,
)
from amiga_fleet_mcp.fleet import Fleet  # noqa: E402
from amiga_fleet_mcp.tools import fleet as fleet_tool  # noqa: E402
from amiga_fleet_mcp.tools import fs as fs_tool  # noqa: E402
from amiga_fleet_mcp.tools import sys as sys_tool  # noqa: E402

QEMU_RUNNER = find_qemu_runner()

X5000_MCPD_PORT = 4322
QEMU_MCPD_PORT = 4422  # host-side hostfwd; -> guest 4322
QMP_PORT = 14422

# Populated from CLI in main().
QEMU_BINARY: Path = Path("qemu-system-ppc")
PEG2_CONFIG: Path = Path()
X5000_HOST: str = ""


def make_config(archive_root: Path) -> Config:
    return Config(
        server=ServerConfig(archive_root=archive_root),
        paths=PathsConfig(qemu_runner=QEMU_RUNNER, qemu_binary=QEMU_BINARY),
        targets={
            "qemu-pegasos2": TargetConfig(
                type="qemu", display_name="QEMU Pegasos2",
                machine="pegasos2", qemu_config=PEG2_CONFIG,
                channels=TargetChannels(
                    mcpd=McpdChannel(endpoint=f"127.0.0.1:{QEMU_MCPD_PORT}"),
                    qmp=QmpChannel(endpoint=f"127.0.0.1:{QMP_PORT}"),
                ),
            ),
            "x5000-real": TargetConfig(
                type="remote", display_name="X5000 (real hardware)",
                channels=TargetChannels(
                    mcpd=McpdChannel(endpoint=f"{X5000_HOST}:{X5000_MCPD_PORT}"),
                ),
            ),
        },
    )


def build_qemu_cmd(fleet: Fleet) -> list[str]:
    from amiga_fleet_mcp.qemu.cmdline import build_cmdline
    cfg = fleet.target_config("qemu-pegasos2")
    cmd, _ports = build_cmdline(QEMU_BINARY, PEG2_CONFIG, cfg)
    return cmd


def wait_for_mcpd(host: str, port: int,
                  timeout_s: int = 240) -> tuple[bool, float]:
    t0 = time.time()
    deadline = t0 + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2.0):
                return True, time.time() - t0
        except OSError:
            pass
        time.sleep(2.0)
    return False, time.time() - t0


def quit_qemu(proc: subprocess.Popen[bytes]) -> None:
    try:
        with socket.create_connection(("127.0.0.1", QMP_PORT), timeout=5) as s:
            s.settimeout(5)
            s.recv(4096)
            s.sendall(b'{"execute":"qmp_capabilities"}\n')
            s.recv(4096)
            s.sendall(b'{"execute":"quit"}\n')
            try: s.recv(4096)
            except OSError: pass
    except OSError:
        pass
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill()


async def amain() -> int:
    global QEMU_BINARY, PEG2_CONFIG, X5000_HOST
    ap = argparse.ArgumentParser()
    ap.add_argument("--x5000", default=os.environ.get("X5000_HOST"),
                    help="X5000 IP (default $X5000_HOST; required)")
    ap.add_argument("--qemu-binary",
                    default=os.environ.get("QEMU_BINARY")
                    or shutil.which("qemu-system-ppc")
                    or shutil.which("qemu-system-ppc.exe")
                    or "qemu-system-ppc",
                    help="qemu-system-ppc binary path")
    ap.add_argument("--peg2-config", required=True,
                    help="QEMU pegasos2 config.json path")
    args = ap.parse_args()
    if not args.x5000:
        ap.error("--x5000 or $X5000_HOST is required")
    QEMU_BINARY = Path(args.qemu_binary)
    PEG2_CONFIG = Path(args.peg2_config).expanduser().resolve()
    X5000_HOST = args.x5000

    archive_root = HERE.parent / "tmp" / "concurrent-archive"
    archive_root.mkdir(parents=True, exist_ok=True)
    fleet = Fleet(make_config(archive_root))
    archive = Archive(archive_root)
    print(f"[setup] archive at {archive.run_dir}")

    failures = 0

    # --- preflight: X5000 already up via auto-start ---------------
    print(f"\n[1/7] X5000 preflight at {X5000_HOST}:{X5000_MCPD_PORT}")
    ok, elapsed = wait_for_mcpd(X5000_HOST, X5000_MCPD_PORT, timeout_s=10)
    if not ok:
        print(f"      FAIL: X5000 MCPd not reachable ({elapsed:.0f}s)")
        return 1
    print(f"      reachable")

    # --- launch QEMU + wait for auto-start ------------------------
    print("\n[2/7] Launch QEMU pegasos2")
    cmd = build_qemu_cmd(fleet)
    log = open(archive.run_dir / "qemu.log", "wb")
    proc = subprocess.Popen(
        cmd, cwd=str(QEMU_BINARY.parent),
        stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
    )
    fleet.register_qemu_process("qemu-pegasos2", proc)
    print(f"      pid {proc.pid}")

    try:
        print("\n[3/7] Wait for QEMU MCPd auto-start at "
              f"127.0.0.1:{QEMU_MCPD_PORT}")
        ok, elapsed = wait_for_mcpd("127.0.0.1", QEMU_MCPD_PORT, timeout_s=240)
        if not ok:
            print(f"      FAIL after {elapsed:.0f}s")
            return 2
        print(f"      auto-started after {elapsed:.0f}s")

        # --- concurrent calls --------------------------------------
        print("\n[4/7] fleet.run_on_all sys.version (parallel)")
        t0 = time.time()
        roa = await fleet_tool.fleet_run_on_all(fleet, "sys.version", {})
        elapsed = time.time() - t0
        archive.log_call(
            "fleet.run_on_all", "qemu+x5000",
            {"method": "sys.version"}, result=roa.model_dump(),
        )
        ok_count = sum(1 for e in roa.results.values() if e.ok is not None)
        print(f"      elapsed={elapsed:.2f}s  ok={ok_count}/"
              f"{len(roa.results)}")
        for name, entry in roa.results.items():
            if entry.ok:
                kick = entry.ok.get("kickstart")
                wb = entry.ok.get("workbench")
                print(f"        {name:18}  kickstart={kick}  workbench={wb}")
            else:
                print(f"        {name:18}  ERROR  {entry.error}")
        if ok_count != 2:
            print("      FAIL: expected 2/2 ok")
            failures += 1

        # --- per-target board detection diverges -------------------
        print("\n[5/7] sys.hardware on each target")
        hw_q = await sys_tool.sys_hardware(fleet, "qemu-pegasos2")
        hw_x = await sys_tool.sys_hardware(fleet, "x5000-real")
        archive.log_call("sys.hardware", "qemu-pegasos2", {},
                         result=hw_q.model_dump())
        archive.log_call("sys.hardware", "x5000-real", {},
                         result=hw_x.model_dump())
        print(f"      QEMU pegasos2:  family={hw_q.cpu.family}  "
              f"model={hw_q.cpu.model}  speed={hw_q.cpu.speed_hz}")
        print(f"      X5000:          family={hw_x.cpu.family}  "
              f"model={hw_x.cpu.model}  speed={hw_x.cpu.speed_hz}")
        if hw_q.cpu.family == hw_x.cpu.family and \
           hw_q.cpu.model == hw_x.cpu.model:
            print("      FAIL: QEMU and X5000 reported the same CPU")
            failures += 1

        # --- fleet.relay (cross-host file copy) --------------------
        print("\n[6/7] fleet.relay: copy a blob from QEMU -> X5000")
        # Write a known blob on QEMU, relay to X5000, hash both sides.
        blob = b"concurrent-test-" + bytes(range(256)) * 4
        SRC = "RAM:relay_src.bin"
        DST = "RAM:relay_dst.bin"
        await fs_tool.fs_write(
            fleet, "qemu-pegasos2", SRC,
            base64.b64encode(blob).decode(),
        )
        rel = await fleet_tool.fleet_relay(
            fleet,
            src_target="qemu-pegasos2", src_path=SRC,
            dst_target="x5000-real",  dst_path=DST,
        )
        archive.log_call("fleet.relay", "qemu->x5000",
                         {"src": SRC, "dst": DST}, result=rel.model_dump())
        print(f"      bytes={rel.bytes}  src={rel.src_target}:{rel.src_path}  "
              f"dst={rel.dst_target}:{rel.dst_path}")
        # Verify hashes match on both sides.
        h_src = await fs_tool.fs_hash(fleet, "qemu-pegasos2", SRC)
        h_dst = await fs_tool.fs_hash(fleet, "x5000-real", DST)
        print(f"      src hash: {h_src.hash}")
        print(f"      dst hash: {h_dst.hash}")
        if h_src.hash != h_dst.hash:
            print("      FAIL: hashes differ post-relay")
            failures += 1
        # Cleanup
        try: await fs_tool.fs_delete(fleet, "qemu-pegasos2", SRC)
        except Exception: pass
        try: await fs_tool.fs_delete(fleet, "x5000-real", DST)
        except Exception: pass

        # --- fleet.barrier with per-target timeout ----------------
        print("\n[7/7] fleet.barrier sys.version on both targets")
        t0 = time.time()
        br = await fleet_tool.fleet_barrier(
            fleet, "sys.version", {},
            targets=["qemu-pegasos2", "x5000-real"],
            per_target_timeout_s=10.0,
        )
        elapsed = time.time() - t0
        archive.log_call("fleet.barrier", "qemu+x5000",
                         {"method": "sys.version"},
                         result=br.model_dump())
        ok_b = sum(1 for e in br.results.values() if e.ok is not None)
        print(f"      elapsed={elapsed:.2f}s  ok={ok_b}/{len(br.results)}")
        if ok_b != 2:
            print("      FAIL: expected ok=2")
            failures += 1

    finally:
        print("\n[cleanup] stop QEMU")
        quit_qemu(proc)
        try: log.close()
        except Exception: pass
        await fleet.close_all()

    print(f"\n[result] failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
