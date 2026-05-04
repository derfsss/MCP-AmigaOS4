"""Continuously read a serial port, log raw + ascii to stdout and a file.

Run:
    python scripts/tail_serial.py --port COM7 --baud 115200 --out C:\\tmp\\com7.log
Stop with Ctrl-C or by killing the process.
"""

from __future__ import annotations

import argparse
import sys
import time

import serial


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    print(f"opening {args.port} @ {args.baud} 8N1, log -> {args.out}",
          flush=True)
    ser = serial.Serial(
        port=args.port, baudrate=args.baud,
        bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE, timeout=0.1,
        rtscts=False, dsrdtr=False, xonxoff=False,
    )
    ser.reset_input_buffer()

    pending = bytearray()
    total = 0
    with open(args.out, "wb") as fh:
        try:
            while True:
                n = ser.in_waiting
                if n:
                    chunk = ser.read(n)
                    fh.write(chunk); fh.flush()
                    total += len(chunk)
                    pending.extend(chunk)
                    while b"\n" in pending:
                        line, _, rest = pending.partition(b"\n")
                        pending = bytearray(rest)
                        text = line.rstrip(b"\r").decode("ascii", "replace")
                        print(f"  | {text}", flush=True)
                else:
                    time.sleep(0.02)
        except KeyboardInterrupt:
            pass
        finally:
            if pending:
                print(f"  | {pending.decode('ascii', 'replace')}",
                      flush=True)
            ser.close()
            print(f"closed. {total}B captured.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
