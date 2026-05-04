"""Upload + launch MCPd on the X5000 via SerialShell.

The X5000 is a real network host - no QEMU hostfwd in the way -
so MCPd listens on the X5000's own IP at port 4322 directly.

Idempotent: if a stale MCPd from a previous run is still bound on
4322, we abort with a clear message rather than try to kill it.

Configuration:
  --host <ip>            X5000 IP (default $X5000_HOST or required)
  $QEMU_RUNNER_DIR       qemu-runner checkout (auto-detects if a
                         sibling checkout exists, see scripts/_paths.py)
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import socket
import sys
import time
from pathlib import Path

from _paths import find_qemu_runner, repo_root

QEMU_RUNNER = find_qemu_runner()
MCPD_BINARY = repo_root() / "mcpd" / "MCPd"

SS_PORT = 4321
MCPD_PORT = 4322
GUEST_DST = "RAM:MCPd"


def _serial_client(host: str):
    spec = importlib.util.spec_from_file_location(
        "_serial", QEMU_RUNNER / "serial_client.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SerialClient(host=host, port=SS_PORT)


def _port_reachable(host: str, port: int, timeout_s: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--host", default=os.environ.get("X5000_HOST"),
        help="X5000 IP (default $X5000_HOST; required if env unset)",
    )
    args = ap.parse_args()
    if not args.host:
        ap.error("--host or $X5000_HOST is required")
    x5000_host = args.host

    if not MCPD_BINARY.exists():
        print(f"FAIL: MCPd binary missing at {MCPD_BINARY}; "
              "run `cd mcpd && make docker-build` first")
        return 1

    print(f"[setup] MCPd binary: {MCPD_BINARY} "
          f"({MCPD_BINARY.stat().st_size} bytes)")

    if _port_reachable(x5000_host, MCPD_PORT, 1.0):
        print(f"[abort] something is already listening on "
              f"{x5000_host}:{MCPD_PORT}. Either reboot the X5000 or "
              "kill the existing MCPd before re-bootstrapping.")
        return 2

    sc = _serial_client(x5000_host)
    sc.connect(timeout=10.0, retries=3, retry_interval=3.0)
    try:
        size = MCPD_BINARY.stat().st_size
        print(f"[upload] {MCPD_BINARY} -> {GUEST_DST} ({size} bytes)")
        sc.upload_file(str(MCPD_BINARY), GUEST_DST, timeout=120.0)
        print("[upload] OK")

        print(f"[guest] Protect {GUEST_DST} +e")
        out = sc.send_command(f"Protect {GUEST_DST} +e", timeout=10)
        if out.strip():
            print(f"  output: {out.strip()[:200]!r}")

        print(f"[guest] Run >NIL: <NIL: {GUEST_DST}")
        out = sc.send_command(f"Run >NIL: <NIL: {GUEST_DST}", timeout=10)
        if out.strip():
            print(f"  output: {out.strip()[:200]!r}")
    finally:
        sc.close()

    print(f"\n[wait] for MCPd to bind {x5000_host}:{MCPD_PORT}")
    deadline = time.time() + 20.0
    ok = False
    while time.time() < deadline:
        if _port_reachable(x5000_host, MCPD_PORT, 1.0):
            ok = True
            break
        time.sleep(0.5)
    if not ok:
        print("[fail] MCPd never bound the port. Check the X5000's "
              "console for any startup output.")
        return 3
    print(f"[ok] MCPd reachable at {x5000_host}:{MCPD_PORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
