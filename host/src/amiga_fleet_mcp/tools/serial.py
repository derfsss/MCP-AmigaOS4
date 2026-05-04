"""serial.* tools — host-side capture of a target's UART stream.

Each target may declare one or more serial channels in config.toml:

    [targets.x5000.channels.uboot]
    enabled = true
    port    = "COM7"
    baud    = 115200

`serial.start` opens the port and starts a background reader that
appends raw bytes to `<log_dir>/serial/<target>_<channel>.log`.
`serial.read` and `serial.tail` pull bytes back as base64 (so the
JSON wire is binary-safe).

Channel name defaults to `"uboot"` since that's the rear-panel
(U-Boot + AOS4 kernel debug) UART — the most common case. Pass
`channel="mcu"` for the MCU UART once it's wired.
"""

from __future__ import annotations

import base64
from typing import Any

from pydantic import BaseModel

from ..config import SerialChannel
from ..errors import InvalidParams, NotCapable
from ..fleet import Fleet


class SerialChannelInfo(BaseModel):
    target: str
    channel: str
    port: str
    baud: int
    log_path: str
    running: bool
    started_at: float
    total_bytes: int
    file_size: int
    last_error: str | None = None


class SerialStartResult(BaseModel):
    target: str
    channel: str
    port: str
    baud: int
    log_path: str
    was_running: bool
    truncated: bool


class SerialStopResult(BaseModel):
    target: str
    channel: str
    stopped: bool
    total_bytes: int
    duration_s: float


class SerialReadResult(BaseModel):
    target: str
    channel: str
    bytes_b64: str
    offset: int
    next_offset: int
    file_size: int
    running: bool


class SerialStatusResult(BaseModel):
    captures: list[SerialChannelInfo]


def _channel_config(fleet: Fleet, target: str, channel: str) -> SerialChannel:
    cfg = fleet.target_config(target)
    ch = getattr(cfg.channels, channel, None)
    if not isinstance(ch, SerialChannel) or not ch.enabled:
        raise NotCapable(
            f"target {target!r} has no enabled serial channel {channel!r}",
            data={
                "target": target,
                "channel": channel,
                "configured_channels": [
                    n for n in ("uboot", "mcu")
                    if isinstance(getattr(cfg.channels, n, None), SerialChannel)
                ],
            },
        )
    if not ch.port:
        raise InvalidParams(
            f"channel {channel!r} on {target!r} has no port set in config",
            data={"target": target, "channel": channel},
        )
    return ch


def _info(cap: Any) -> SerialChannelInfo:
    info = cap.info()
    return SerialChannelInfo(
        target=info.target,
        channel=info.channel,
        port=info.port,
        baud=info.baud,
        log_path=str(info.log_path),
        running=info.running,
        started_at=info.started_at,
        total_bytes=info.total_bytes,
        file_size=cap.file_size(),
        last_error=info.last_error,
    )


async def serial_start(
    fleet: Fleet, target: str, *,
    channel: str = "uboot",
    truncate: bool = False,
) -> SerialStartResult:
    """Open the configured serial port and start a background reader."""
    ch = _channel_config(fleet, target, channel)
    cap = fleet.serial_captures.get_or_create(
        target=target, channel=channel, port=ch.port, baud=ch.baud,
    )
    was_running = cap.running
    if not was_running:
        cap.start(truncate=truncate)
    return SerialStartResult(
        target=target, channel=channel, port=ch.port, baud=ch.baud,
        log_path=str(cap.log_path),
        was_running=was_running,
        truncated=bool(truncate and not was_running),
    )


async def serial_stop(
    fleet: Fleet, target: str, *,
    channel: str = "uboot",
) -> SerialStopResult:
    """Stop a running capture; idempotent (returns stopped=False if no
    capture was active)."""
    import time as _time
    cap = fleet.serial_captures.get(target, channel)
    if cap is None or not cap.running:
        return SerialStopResult(
            target=target, channel=channel,
            stopped=False, total_bytes=cap.total_bytes if cap else 0,
            duration_s=0.0,
        )
    started = cap.started_at
    total = cap.total_bytes
    cap.stop()
    return SerialStopResult(
        target=target, channel=channel, stopped=True,
        total_bytes=total,
        duration_s=max(0.0, _time.time() - started),
    )


async def serial_status(
    fleet: Fleet, target: str | None = None, *,
    channel: str | None = None,
) -> SerialStatusResult:
    """List captures. If target is given, filter to that target;
    if channel is also given, narrow further."""
    out: list[SerialChannelInfo] = []
    for cap in fleet.serial_captures._captures.values():
        if target and cap.target != target:
            continue
        if channel and cap.channel != channel:
            continue
        out.append(_info(cap))
    return SerialStatusResult(captures=out)


async def serial_read(
    fleet: Fleet, target: str, *,
    channel: str = "uboot",
    offset: int = 0,
    max_bytes: int = 65536,
) -> SerialReadResult:
    """Read up to `max_bytes` from the capture log, starting at `offset`.
    Returns base64-encoded bytes plus the next offset for incremental
    polling. Works whether or not the capture is currently running."""
    cap = fleet.serial_captures.get(target, channel)
    if cap is None:
        ch = _channel_config(fleet, target, channel)
        cap = fleet.serial_captures.get_or_create(
            target=target, channel=channel, port=ch.port, baud=ch.baud,
        )
    if max_bytes <= 0 or max_bytes > 4 * 1024 * 1024:
        max_bytes = 65536
    data = cap.read_at(offset, max_bytes)
    return SerialReadResult(
        target=target, channel=channel,
        bytes_b64=base64.b64encode(data).decode("ascii"),
        offset=offset,
        next_offset=offset + len(data),
        file_size=cap.file_size(),
        running=cap.running,
    )


async def serial_tail(
    fleet: Fleet, target: str, *,
    channel: str = "uboot",
    max_bytes: int = 8192,
) -> SerialReadResult:
    """Read the last `max_bytes` of the capture log. Convenience over
    `serial.read` for "what just happened" queries."""
    cap = fleet.serial_captures.get(target, channel)
    if cap is None:
        ch = _channel_config(fleet, target, channel)
        cap = fleet.serial_captures.get_or_create(
            target=target, channel=channel, port=ch.port, baud=ch.baud,
        )
    if max_bytes <= 0 or max_bytes > 4 * 1024 * 1024:
        max_bytes = 8192
    size = cap.file_size()
    start = max(0, size - max_bytes)
    data = cap.read_at(start, max_bytes)
    return SerialReadResult(
        target=target, channel=channel,
        bytes_b64=base64.b64encode(data).decode("ascii"),
        offset=start,
        next_offset=start + len(data),
        file_size=size,
        running=cap.running,
    )


async def serial_clear(
    fleet: Fleet, target: str, *,
    channel: str = "uboot",
) -> SerialChannelInfo:
    """Truncate the capture log. Capture must be stopped."""
    cap = fleet.serial_captures.get(target, channel)
    if cap is None:
        ch = _channel_config(fleet, target, channel)
        cap = fleet.serial_captures.get_or_create(
            target=target, channel=channel, port=ch.port, baud=ch.baud,
        )
    if cap.running:
        raise InvalidParams(
            "cannot clear log while capture is running; stop first",
            data={"target": target, "channel": channel},
        )
    cap.clear_log()
    return _info(cap)
