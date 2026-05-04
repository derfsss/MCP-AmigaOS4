"""Kill the running MCPd on the X5000 via SerialShell `Break N C`.

Used when bootstrap_mcpd.py refuses to overwrite a still-running
instance and we need to re-deploy a new build.

Configuration:
  --host <ip>            X5000 IP (default $X5000_HOST or required)
  $QEMU_RUNNER_DIR       qemu-runner checkout
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import socket
import sys
import time

from _paths import find_qemu_runner

QEMU_RUNNER = find_qemu_runner()
SS_PORT = 4321
MCPD_PORT = 4322


def _serial_client(host: str):
    spec = importlib.util.spec_from_file_location(
        "_serial", QEMU_RUNNER / "serial_client.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.SerialClient(host=host, port=SS_PORT)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--host", default=os.environ.get("x5000_host"),
        help="X5000 IP (default $X5000_HOST; required if env unset)",
    )
    args = ap.parse_args()
    if not args.host:
        ap.error("--host or $X5000_HOST is required")
    x5000_host = args.host

    sc = _serial_client(x5000_host)
    sc.connect(timeout=10.0, retries=3, retry_interval=3.0)
    try:
        # AmigaDOS Status output looks like:
        #   Process  1: Loaded as command: SerialShell
        #   Process  2: Loaded as command: MCPd
        # Brittle parse but adequate.
        out = sc.send_command("Status FULL", timeout=10)
        print("Status FULL output:")
        for line in out.splitlines()[:40]:
            print(f"  {line}")
        print()

        # Find MCPd process number.
        mcpd_pid = None
        for line in out.splitlines():
            if "MCPd" in line:
                m = re.search(r"\bProcess\s+(\d+)", line, re.IGNORECASE)
                if m:
                    mcpd_pid = int(m.group(1))
                    break

        if mcpd_pid is None:
            # Try a simpler grep.
            print("No MCPd process found via Status; trying basic Status")
            out = sc.send_command("Status", timeout=5)
            print(out)
            return 2

        print(f"Sending Break {mcpd_pid} C  (Ctrl-C to MCPd)")
        sc.send_command(f"Break {mcpd_pid} C", timeout=5)
    finally:
        sc.close()

    # Wait for the TCP port to release.
    deadline = time.time() + 10.0
    while time.time() < deadline:
        try:
            with socket.create_connection(
                (x5000_host, MCPD_PORT), timeout=1.0
            ):
                time.sleep(0.5)
                continue
        except OSError:
            print(f"MCPd on {x5000_host}:{MCPD_PORT} is gone.")
            return 0
    print("MCPd port still bound after 10s. The Break may not have taken; "
          "you may need to reboot the X5000.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
