"""Quick probe of the X5000 rear-panel DB9 (U-Boot / kernel sysdebug).

  1. Listen N seconds at 115200 8N1 for any traffic
     (AOS4 may be emitting kernel-debug output continuously).
  2. Send `\r` to provoke a U-Boot prompt (only meaningful if the
     X5000 is sat at the U-Boot prompt, not yet booted into AOS4).
  3. Send `version\r` and `help\r` -- U-Boot shell only.

Run:
    python scripts/probe_x5000_uboot.py [--port COM7] [--listen 5]

If silent and no prompt comes back: the cable may not be connected to
the rear DB9, or AOS4 may have grabbed the UART without emitting.
"""

from __future__ import annotations

import argparse
import sys
import time

import serial


def _drain(ser: serial.Serial, label: str, settle_s: float = 0.8) -> bytes:
    end = time.monotonic() + settle_s
    buf = bytearray()
    while time.monotonic() < end:
        n = ser.in_waiting
        if n:
            buf.extend(ser.read(n))
            end = time.monotonic() + 0.3
        else:
            time.sleep(0.02)
    if buf:
        print(f"[{label}] {len(buf)}B  raw={bytes(buf[:120])!r}"
              f"{'...' if len(buf) > 120 else ''}")
        text = buf.decode("ascii", errors="replace").rstrip()
        for line in text.splitlines():
            print(f"    {line}")
    else:
        print(f"[{label}] (no reply)")
    return bytes(buf)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM7")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--listen", type=float, default=5.0)
    args = ap.parse_args()

    print(f"opening {args.port} @ {args.baud} 8N1")
    try:
        ser = serial.Serial(
            port=args.port,
            baudrate=args.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.1,
            write_timeout=1.0,
            rtscts=False,
            dsrdtr=False,
            xonxoff=False,
        )
    except serial.SerialException as e:
        print(f"open failed: {e}")
        return 2

    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()

        print(f"\n[1] listening {args.listen:.1f}s for unsolicited output...")
        end = time.monotonic() + args.listen
        spontaneous = bytearray()
        while time.monotonic() < end:
            n = ser.in_waiting
            if n:
                spontaneous.extend(ser.read(n))
            else:
                time.sleep(0.05)
        if spontaneous:
            print(f"  {len(spontaneous)}B raw (first 200): "
                  f"{bytes(spontaneous[:200])!r}")
            text = spontaneous.decode("ascii", errors="replace").rstrip()
            for line in text.splitlines()[-30:]:
                print(f"    {line}")
        else:
            print("  (silent)")

        for cmd, label in (
            ("", "bare-CR"),
            ("version", "version"),
            ("help", "help"),
        ):
            payload = (cmd + "\r").encode("ascii")
            print(f"\n>>> {cmd!r}  ({payload!r})")
            ser.write(payload)
            ser.flush()
            _drain(ser, label)

        print("\ndone.")
        return 0
    finally:
        ser.close()


if __name__ == "__main__":
    sys.exit(main())
