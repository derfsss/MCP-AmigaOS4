"""Use SerialShell on the running X5000 install to move the bad
emu10kx.audio file out of BootTest:DEVS/AHI/ so BootTest: can boot.

SerialShell protocol:
  - On connect, server sends `SERIALSHELL_READY\n`
  - Client sends `<cmd>\n`
  - Server runs it, emits stdout, ends with `___SERIALSHELL_DONE___\n`
  - Client sends `SERIALSHELL_QUIT\n` to disconnect cleanly
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time

DEFAULT_PORT = 4321
READY = b"SERIALSHELL_READY\n"
DONE = b"___SERIALSHELL_DONE___\n"
QUIT = b"SERIALSHELL_QUIT\n"


class SerialShell:
    def __init__(self, host: str, port: int) -> None:
        self.s = socket.create_connection((host, port), timeout=10)
        self._buf = b""
        # Wait for the READY marker
        end = time.monotonic() + 10.0
        while time.monotonic() < end:
            self.s.settimeout(end - time.monotonic())
            try:
                chunk = self.s.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            self._buf += chunk
            if READY in self._buf:
                self._buf = self._buf.split(READY, 1)[1]
                return
        raise RuntimeError(f"no READY in {self._buf!r}")

    def send(self, cmd: str, timeout: float = 30.0) -> str:
        self.s.sendall((cmd + "\n").encode("ascii"))
        end = time.monotonic() + timeout
        while DONE not in self._buf and time.monotonic() < end:
            self.s.settimeout(end - time.monotonic())
            try:
                chunk = self.s.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            self._buf += chunk
        if DONE not in self._buf:
            raise TimeoutError(
                f"no DONE marker, partial output: {self._buf!r}"
            )
        out, self._buf = self._buf.split(DONE, 1)
        return out.decode("ascii", errors="replace")

    def close(self) -> None:
        try:
            self.s.sendall(QUIT)
        except Exception:
            pass
        try:
            self.s.close()
        except Exception:
            pass


def run(c: SerialShell, cmd: str) -> str:
    print(f"\n>>> {cmd}")
    out = c.send(cmd)
    for line in out.splitlines():
        line = line.rstrip()
        if line:
            print(f"    {line}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("X5000_HOST"),
                    help="X5000 IP (default $X5000_HOST; required)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"SerialShell TCP port (default {DEFAULT_PORT})")
    args = ap.parse_args()
    if not args.host:
        ap.error("--host or $X5000_HOST is required")

    print(f"connecting to SerialShell at {args.host}:{args.port}...")
    c = SerialShell(args.host, args.port)
    print("ready.")

    try:
        # Confirm BootTest: visible + the bad file is there
        run(c, 'List BootTest:DEVS/AHI/ FILES NOHEAD QUICK')
        # Make sure dest dir exists
        run(c, 'MakeDir BootTest:Storage/AHI ALL')
        # Move
        run(c, ('Rename "BootTest:DEVS/AHI/emu10kx.audio" '
                '"BootTest:Storage/AHI/emu10kx.audio"'))

        print("\n=== verify ===")
        out_devs = run(c, 'List BootTest:DEVS/AHI/ FILES NOHEAD QUICK')
        out_stor = run(c, 'List BootTest:Storage/AHI/ FILES NOHEAD QUICK')
    finally:
        c.close()

    moved_out = "emu10kx.audio" not in out_devs
    moved_in = "emu10kx.audio" in out_stor
    print(f"\n*** verdict: "
          f"{'PASS' if (moved_out and moved_in) else 'FAIL'} "
          f"(moved_out={moved_out}, moved_in={moved_in}) ***")
    return 0 if (moved_out and moved_in) else 1


if __name__ == "__main__":
    sys.exit(main())
