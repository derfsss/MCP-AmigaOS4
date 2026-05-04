"""Probe the X5000 Cyrus MCU debug shell on P18 (host COM5).

The MCU debug interface is interactive (>> prompt) at 38400 8N1.
Send CR to wake the prompt, then run `help` to enumerate commands.
"""
from __future__ import annotations

import argparse
import sys
import time

import serial


def drain(ser: serial.Serial, until_idle_s: float = 0.5) -> bytes:
    """Read until the serial line goes idle for `until_idle_s`."""
    buf = bytearray()
    last = time.time()
    while time.time() - last < until_idle_s:
        chunk = ser.read(4096)
        if chunk:
            buf.extend(chunk)
            last = time.time()
        else:
            time.sleep(0.05)
    return bytes(buf)


def send_and_read(ser: serial.Serial, cmd: bytes, idle_s: float = 0.5) -> bytes:
    ser.write(cmd)
    ser.flush()
    return drain(ser, until_idle_s=idle_s)


def show(label: str, data: bytes) -> None:
    print(f"--- {label} ({len(data)} bytes) ---")
    if not data:
        print("  (no data)")
        return
    text = data.decode("ascii", errors="replace")
    for line in text.splitlines():
        print(f"  {line}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM5")
    ap.add_argument("--baud", type=int, default=38400)
    ap.add_argument("--cmds", nargs="*", default=["help"],
                    help="commands to run after waking the >> prompt")
    args = ap.parse_args()

    ser = serial.Serial(
        args.port, args.baud,
        bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE, timeout=0.2,
        rtscts=False, dsrdtr=False, xonxoff=False,
    )
    print(f"opened {args.port} @ {args.baud} 8N1")

    # Drain anything pending first.
    drain(ser, until_idle_s=0.3)

    # Wake the prompt.
    print("\n>>> sending CR to wake prompt")
    out = send_and_read(ser, b"\r", idle_s=0.5)
    show("wake response", out)

    # Run requested commands.
    for cmd in args.cmds:
        # Special: "watch:N" runs a quiet drain for N seconds (catches
        # continuous emission turned on by a prior `q` toggle).
        if cmd.startswith("watch:"):
            secs = float(cmd.split(":", 1)[1])
            print(f"\n>>> watching for {secs}s of unsolicited output")
            t0 = time.time()
            buf = bytearray()
            while time.time() - t0 < secs:
                chunk = ser.read(4096)
                if chunk:
                    buf.extend(chunk)
                else:
                    time.sleep(0.05)
            show(f"watch ({secs:.1f}s)", bytes(buf))
            continue
        print(f"\n>>> {cmd}")
        out = send_and_read(ser, cmd.encode("ascii") + b"\r", idle_s=1.0)
        show(f"reply to {cmd!r}", out)

    ser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
