"""Probe a target via SerialShell to learn what we're dealing with.

Discovers: AmigaOS Version, writable volumes, current task list,
free RAM. Used as a setup step before deploying MCPd - we want to
know whether the destination dir for MCPd is RAM: (lost on reboot,
fine for the first run) or a writable disk volume (persists).

Configuration:
  --host <ip>            Target IP (default $X5000_HOST or required)
"""

from __future__ import annotations

import argparse
import os
import sys

from serial_client import SerialClient  # from the qemu-runner package

PORT = 4321


def _serial_client(host: str):
    return SerialClient(host=host, port=PORT)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--host", default=os.environ.get("X5000_HOST"),
        help="Target IP (default $X5000_HOST; required if env unset)",
    )
    args = ap.parse_args()
    if not args.host:
        ap.error("--host or $X5000_HOST is required")

    sc = _serial_client(args.host)
    sc.connect(timeout=10.0, retries=3, retry_interval=3.0)
    try:
        for cmd in [
            "Version FULL",
            "Info",
            "Avail FLUSH",
        ]:
            print(f"\n=== {cmd} ===")
            try:
                out = sc.send_command(cmd, timeout=20)
                # Truncate Avail noise to ~20 lines
                lines = out.splitlines()
                for line in lines[:25]:
                    print(f"  {line}")
                if len(lines) > 25:
                    print(f"  ... ({len(lines) - 25} more lines)")
            except Exception as e:
                print(f"  ERR: {e}")
    finally:
        sc.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
