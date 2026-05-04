"""Quick probe of the X5000 MCU UART over the FTDI 3.3V USB-TTL cable.

Tier-A tests #1, #3, #4 from the MCU test list:
  1. Listen 3s at 38400 8N1 for unsolicited output (boot/alert traffic)
  3. Send `#t\r` and parse the temperature reply
  4. Send `#f\r` and parse the fan reply
Also probes `#v\r` (version) and `#?\r` (help) since they're free.

Run:
    python scripts/probe_x5000_mcu.py [--port COM5] [--baud 38400]

If nothing replies, the FTDI cable is open on the host but not yet wired
to the X5000 P18 internal header (phase-8 wiring gap).
"""

from __future__ import annotations

import argparse
import sys
import time

import serial


def _drain(ser: serial.Serial, label: str, settle_s: float = 0.6) -> bytes:
    """Read whatever is in the buffer for up to settle_s of idle."""
    end = time.monotonic() + settle_s
    buf = bytearray()
    while time.monotonic() < end:
        n = ser.in_waiting
        if n:
            chunk = ser.read(n)
            buf.extend(chunk)
            end = time.monotonic() + 0.2
        else:
            time.sleep(0.02)
    if buf:
        print(f"[{label}] {len(buf)}B  raw={bytes(buf)!r}")
        try:
            text = buf.decode("ascii", errors="replace").rstrip()
            if text:
                for line in text.splitlines():
                    print(f"    {line}")
        except Exception:
            pass
    else:
        print(f"[{label}] (no reply)")
    return bytes(buf)


def _send(ser: serial.Serial, cmd: str) -> None:
    payload = (cmd + "\r").encode("ascii")
    print(f"\n>>> {cmd!r}  ({payload!r})")
    ser.write(payload)
    ser.flush()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="COM5")
    ap.add_argument("--baud", type=int, default=38400)
    ap.add_argument("--listen", type=float, default=3.0,
                    help="seconds to listen before sending anything")
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
            print(f"  {len(spontaneous)}B  raw={bytes(spontaneous)!r}")
            text = spontaneous.decode("ascii", errors="replace").rstrip()
            for line in text.splitlines():
                print(f"    {line}")
        else:
            print("  (silent)")

        for cmd, label in (
            ("#t", "temperatures"),
            ("#f", "fan"),
            ("#v", "version"),
            ("#?", "help"),
        ):
            _send(ser, cmd)
            _drain(ser, label)

        print("\ndone.")
        return 0
    finally:
        ser.close()


if __name__ == "__main__":
    sys.exit(main())
