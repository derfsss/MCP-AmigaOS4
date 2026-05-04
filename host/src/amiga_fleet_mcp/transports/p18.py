"""Host-side driver for the X5000 / A1222 internal MCU debug shell.

The debug shell is reached via the FTDI USB-TTL header on the Amiga
motherboard (X5000 P18, A1222 P15), wired through the host port
configured under `[targets.<name>.channels.mcu]`. The shell speaks
38400 8N1 with a `>>` prompt; commands are ASCII text terminated
by CR.

This module is independent of MCPd: the cable bypasses the SoC
entirely, so it works even when AOS / MCPd are off or wedged. It
is in fact the only software path to power an X5000 ON from a
fully-off state -- the MCU's `p` command boots the supplies.
"""

from __future__ import annotations

import asyncio
import time

import serial


def _drain(ser: serial.Serial, until_idle_s: float) -> bytes:
    """Read until the line goes silent for `until_idle_s` seconds."""
    buf = bytearray()
    last = time.monotonic()
    while time.monotonic() - last < until_idle_s:
        chunk = ser.read(4096)
        if chunk:
            buf.extend(chunk)
            last = time.monotonic()
        else:
            time.sleep(0.05)
    return bytes(buf)


def _open(port: str, baud: int) -> serial.Serial:
    return serial.Serial(
        port, baud,
        bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE, timeout=0.2,
        rtscts=False, dsrdtr=False, xonxoff=False,
    )


def _send_blocking(
    port: str, baud: int, shell_cmd: str, *, idle_s: float = 1.0,
) -> bytes:
    """Synchronous: open, wake the prompt with a CR, send the command
    + CR, drain the reply for `idle_s` seconds of silence, close."""
    ser = _open(port, baud)
    try:
        # Drain anything pending from prior activity.
        _drain(ser, until_idle_s=0.3)
        # Wake the >> prompt.
        ser.write(b"\r")
        ser.flush()
        _drain(ser, until_idle_s=0.5)
        # Send command.
        ser.write(shell_cmd.encode("ascii") + b"\r")
        ser.flush()
        return _drain(ser, until_idle_s=idle_s)
    finally:
        ser.close()


def _stream_capture_blocking(
    port: str, baud: int, watch_s: float,
) -> bytes:
    """Synchronous: open, wake, toggle `q` on, capture continuous
    stream for `watch_s` seconds, toggle `q` back off, close."""
    ser = _open(port, baud)
    try:
        _drain(ser, until_idle_s=0.3)
        ser.write(b"\r")
        ser.flush()
        _drain(ser, until_idle_s=0.5)

        # Toggle on
        ser.write(b"q\r")
        ser.flush()
        _drain(ser, until_idle_s=0.5)

        # Capture
        buf = bytearray()
        t0 = time.monotonic()
        while time.monotonic() - t0 < watch_s:
            chunk = ser.read(4096)
            if chunk:
                buf.extend(chunk)
            else:
                time.sleep(0.05)

        # Toggle off
        ser.write(b"q\r")
        ser.flush()
        _drain(ser, until_idle_s=0.5)
        return bytes(buf)
    finally:
        ser.close()


async def send(port: str, baud: int, shell_cmd: str, *,
               idle_s: float = 1.0) -> str:
    """Async wrapper. Returns the captured ASCII reply."""
    raw = await asyncio.to_thread(
        _send_blocking, port, baud, shell_cmd, idle_s=idle_s,
    )
    return raw.decode("ascii", errors="replace")


async def stream_capture(port: str, baud: int, watch_s: float) -> str:
    """Async wrapper. Toggle `q` on, capture for `watch_s`, toggle
    off, return ASCII text of the captured stream."""
    raw = await asyncio.to_thread(
        _stream_capture_blocking, port, baud, watch_s,
    )
    return raw.decode("ascii", errors="replace")
