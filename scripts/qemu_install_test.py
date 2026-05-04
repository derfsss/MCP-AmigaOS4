"""Verify the auto-start installer on a QEMU pegasos2 guest.

Procedure:
  1. Launch QEMU pegasos2 (mcpd-peg2-upd3 image) with SerialShell + MCPd
     + QMP hostfwds.
  2. Wait for SerialShell at 127.0.0.1:4321.
  3. One-shot bootstrap: SerialShell launches MCPd from SHARED:MCPd.
  4. Wait for MCPd at 127.0.0.1:4422.
  5. Run scripts/install_mcpd_autostart.py against the running MCPd.
  6. Reboot the QEMU guest (Reboot via SerialShell - same as the X5000
     test).
  7. Wait for SerialShell to come back, then for MCPd to auto-launch
     from SYS:System/MCPd/MCPd. Time the boot-to-bind interval.
  8. Run all four validation rounds (round1..round4) against the
     auto-launched MCPd.
  9. QMP-quit the QEMU process.

Expected exit: 0 with all rounds passing 0/0.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
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

QEMU_RUNNER = find_qemu_runner()
MCPD_BINARY = HERE.parent / "mcpd" / "MCPd"

# Populated from CLI in main().
QEMU_BINARY: Path = Path("qemu-system-ppc")
PEG2_CONFIG: Path = Path()
SHARED_DIR: Path = Path()

# QEMU side
SERIALSHELL_PORT = 4321
MCPD_PORT = 4422   # host-side; forwards to guest :4322
QMP_PORT = 14422


def make_config(archive_root: Path) -> Config:
    return Config(
        server=ServerConfig(archive_root=archive_root),
        paths=PathsConfig(qemu_runner=QEMU_RUNNER, qemu_binary=QEMU_BINARY),
        targets={
            "qemu-pegasos2": TargetConfig(
                type="qemu", display_name="QEMU Pegasos2",
                machine="pegasos2", qemu_config=PEG2_CONFIG,
                channels=TargetChannels(
                    mcpd=McpdChannel(endpoint=f"127.0.0.1:{MCPD_PORT}"),
                    qmp=QmpChannel(endpoint=f"127.0.0.1:{QMP_PORT}"),
                ),
            ),
        },
    )


def build_qemu_cmd(fleet: Fleet) -> list[str]:
    from amiga_fleet_mcp.qemu.cmdline import build_cmdline
    cfg = fleet.target_config("qemu-pegasos2")
    cmd, _ports = build_cmdline(QEMU_BINARY, PEG2_CONFIG, cfg)
    for i, arg in enumerate(cmd):
        if arg.startswith("user,") and "id=nic" in arg:
            cmd[i] = arg + f",hostfwd=tcp::{SERIALSHELL_PORT}-:{SERIALSHELL_PORT}"
            break
    return cmd


def wait_for_port(port: int, host: str = "127.0.0.1",
                  timeout_s: int = 240,
                  marker: bytes = b"") -> tuple[bool, float]:
    """Connect-loop. If `marker` is non-empty, also wait for those
    bytes to appear in the first 5s of the connection (used to
    distinguish QEMU slirp accepting on a forwarded port vs. the
    guest service actually being up). """
    t0 = time.time()
    deadline = t0 + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=2.0) as s:
                if not marker:
                    return True, time.time() - t0
                s.settimeout(3.0)
                buf = b""
                end = time.time() + 5.0
                while marker not in buf and time.time() < end:
                    try:
                        chunk = s.recv(1024)
                    except socket.timeout:
                        break
                    if not chunk: break
                    buf += chunk
                if marker in buf:
                    return True, time.time() - t0
        except (ConnectionRefusedError, OSError, socket.timeout):
            pass
        time.sleep(2.0)
    return False, time.time() - t0


def _serial_client():
    spec = importlib.util.spec_from_file_location(
        "_serial", QEMU_RUNNER / "serial_client.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.SerialClient(  # type: ignore[attr-defined]
        host="127.0.0.1", port=SERIALSHELL_PORT,
    )


def bootstrap_guest_mcpd() -> None:
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copy2(MCPD_BINARY, SHARED_DIR / "MCPd")
    sc = _serial_client()
    sc.connect(timeout=10.0, retries=3, retry_interval=5.0)
    try:
        sc.send_command("Protect SHARED:MCPd +rwed", timeout=10)
        sc.send_command("Run >NIL: <NIL: SHARED:MCPd", timeout=10)
    finally:
        sc.close()


def quit_qemu_process(proc: subprocess.Popen[bytes]) -> None:
    """Stop QEMU cleanly via QMP `quit`, falling back to terminate +
    kill. Used because QMP system_reset and guest-issued `Reboot`
    leave AOS4 guests in a broken state - kill+relaunch is the only
    reliable way to reboot. (See memory feedback_qemu_reset.md.)"""
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


def run_validation_round(name: str, endpoint: str) -> int:
    """Run a roundN_validation.py script with the X5000 endpoint
    monkey-patched to point at the QEMU's MCPd."""
    script = HERE / f"{name}_validation.py"
    if not script.exists():
        print(f"  [skip] {name}_validation.py not found")
        return 0
    print(f"  [run] {name} against {endpoint}")
    env = {**os.environ, "MCPD_ENDPOINT": endpoint}
    rc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, env=env,
    )
    last = rc.stdout.strip().splitlines()[-1] if rc.stdout else ""
    print(f"    {last}")
    if rc.returncode != 0:
        print(f"    --- {name} stdout tail ---")
        for line in rc.stdout.splitlines()[-12:]:
            print(f"    {line}")
        if rc.stderr.strip():
            print(f"    --- stderr tail ---")
            for line in rc.stderr.splitlines()[-6:]:
                print(f"    {line}")
    return rc.returncode


async def amain() -> int:
    global QEMU_BINARY, PEG2_CONFIG, SHARED_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--qemu-binary",
                    default=os.environ.get("QEMU_BINARY")
                    or shutil.which("qemu-system-ppc")
                    or shutil.which("qemu-system-ppc.exe")
                    or "qemu-system-ppc")
    ap.add_argument("--peg2-config", required=True,
                    help="QEMU pegasos2 config.json path")
    ap.add_argument("--shared-dir", required=True,
                    help="Host directory exposed to the QEMU guest "
                         "as SHARED: (must contain MCPd staged for "
                         "first-boot bootstrap)")
    args = ap.parse_args()
    QEMU_BINARY = Path(args.qemu_binary)
    PEG2_CONFIG = Path(args.peg2_config).expanduser().resolve()
    SHARED_DIR = Path(args.shared_dir).expanduser().resolve()

    archive_root = Path(__file__).parent.parent / "tmp" / "qemu-install-archive"
    archive_root.mkdir(parents=True, exist_ok=True)
    fleet = Fleet(make_config(archive_root))
    archive = Archive(archive_root)
    print(f"[setup] archive at {archive.run_dir}")

    if not MCPD_BINARY.exists():
        print(f"FAIL: MCPd binary missing at {MCPD_BINARY}")
        return 2

    cmd = build_qemu_cmd(fleet)

    def launch_qemu(label: str) -> tuple[subprocess.Popen[bytes], object]:
        log_path = archive.run_dir / f"qemu-{label}.log"
        log = open(log_path, "wb")
        proc = subprocess.Popen(
            cmd, cwd=str(QEMU_BINARY.parent),
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
        )
        fleet.register_qemu_process("qemu-pegasos2", proc)
        print(f"      pid {proc.pid}, log {log_path}")
        return proc, log

    print("\n[1/9] Launch QEMU pegasos2 (first boot)")
    proc, log = launch_qemu("first-boot")

    failures = 0
    try:
        print("\n[2/9] Wait for SerialShell at :4321 (READY marker)")
        ok, elapsed = wait_for_port(
            SERIALSHELL_PORT, timeout_s=240, marker=b"READY",
        )
        if not ok:
            print(f"      FAIL after {elapsed:.0f}s")
            return 3
        print(f"      ready after {elapsed:.0f}s")

        print("\n[3/9] Bootstrap MCPd via SerialShell + SHARED:MCPd")
        bootstrap_guest_mcpd()

        print("\n[4/9] Wait for MCPd at :4422")
        ok, elapsed = wait_for_port(MCPD_PORT, timeout_s=60)
        if not ok:
            print(f"      FAIL after {elapsed:.0f}s")
            return 4
        print(f"      reachable after {elapsed:.0f}s")

        print("\n[5/9] Run install_mcpd_autostart.py against 127.0.0.1:4422")
        rc = subprocess.run(
            [sys.executable, str(HERE / "install_mcpd_autostart.py"),
             f"127.0.0.1:{MCPD_PORT}", str(MCPD_BINARY)],
            capture_output=True, text=True,
        )
        print(rc.stdout)
        if rc.returncode != 0:
            print(f"      FAIL installer rc={rc.returncode}")
            print(rc.stderr)
            return 5

        print("\n[6/9] Stop QEMU + relaunch (AOS4 reset is unreliable; "
              "we kill the process and start fresh)")
        quit_qemu_process(proc)
        log.close()
        time.sleep(2.0)
        proc, log = launch_qemu("after-install")

        print("\n[7/9] Wait for SerialShell on the relaunched guest")
        ok, elapsed_ss = wait_for_port(
            SERIALSHELL_PORT, timeout_s=240, marker=b"READY",
        )
        if not ok:
            print(f"      FAIL after {elapsed_ss:.0f}s")
            return 7
        print(f"      ready after {elapsed_ss:.0f}s")

        print("\n[8/9] Wait for MCPd to AUTO-LAUNCH at :4422 "
              "(no manual bootstrap)")
        ok, elapsed_mcpd = wait_for_port(MCPD_PORT, timeout_s=120)
        if not ok:
            print(f"      FAIL: MCPd never auto-bound after "
                  f"{elapsed_mcpd:.0f}s - installer didn't take effect")
            return 8
        print(f"      AUTO-BOUND after {elapsed_mcpd:.0f}s "
              f"(SerialShell+MCPd total: {elapsed_ss + elapsed_mcpd:.0f}s)")

        print("\n[9/9] Run round1..round4 against auto-launched MCPd")
        endpoint = f"127.0.0.1:{MCPD_PORT}"
        for r in ("round1", "round2", "round3", "round4"):
            rc = run_validation_round(r, endpoint)
            if rc != 0:
                failures += 1

    finally:
        print("\n[cleanup] stop QEMU")
        quit_qemu_process(proc)
        try: log.close()
        except Exception: pass
        await fleet.close_all()

    print(f"\n[result] failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
