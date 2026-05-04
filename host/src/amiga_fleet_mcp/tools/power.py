"""power.* — host-side MCU debug-shell driver.

These tools talk to the Amiga's internal MCU header (X5000 P18,
A1222 P15) via a host-side FTDI USB-TTL cable, NOT through MCPd.
The cable bypasses the SoC entirely, so the tools work even when
AOS / MCPd are off or wedged. `power.on` (the MCU's `p` command) is
the only software path to boot a fully-off X5000.

Per-target setup:

    [targets.x5000-real.channels.mcu]
    enabled = true
    port    = "COM5"            # or "/dev/ttyUSB1"
    baud    = 38400

If the channel isn't configured, the tools raise NotCapable.

The hardware-destructive tools (`power.on`, `power.off`,
`power.toggle_stream`, `power.shell`) require `confirm=True` --
same accidental-fire guard as `sys.cold_reboot` / `sys.mcu_cmd
cmd="s"`.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..config import SerialChannel
from ..errors import InvalidParams, NotCapable
from ..fleet import Fleet
from ..transports import p18

# ---- result models -------------------------------------------------


class ShellReply(BaseModel):
    target: str
    cmd: str
    port: str
    baud: int
    reply: str
    reply_len: int


class StreamCapture(BaseModel):
    target: str
    cmd: str = "q"
    port: str
    baud: int
    watch_s: float
    captured_bytes: int
    captured: str


# ---- helpers -------------------------------------------------------


def _resolve_mcu_channel(fleet: Fleet, target: str) -> SerialChannel:
    """Return the target's `[channels.mcu]` config, or raise
    NotCapable if it isn't configured / enabled / has no port.

    Also raises NotCapable if `serial.*` is currently capturing the
    same port -- driving the shell would collide with the capture
    reader."""
    cfg = fleet.target_config(target)
    ch = cfg.channels.mcu
    if ch is None or not ch.enabled or not ch.port:
        raise NotCapable(
            "target has no enabled MCU serial channel; configure "
            "[targets.<name>.channels.mcu] with `port` and `baud` "
            "(38400 for X5000 P18 / A1222 P15)",
            data={"target": target},
        )
    cap = fleet.serial_captures.get(target, "mcu")
    if cap is not None and cap.running:
        raise NotCapable(
            "MCU serial channel is currently being captured by "
            "serial.* on this target; stop the capture first via "
            "serial.stop(target=..., channel='mcu') before driving "
            "power.* commands",
            data={"target": target, "channel": "mcu"},
        )
    return ch


def _require_confirm(confirm: bool, op: str) -> None:
    if not confirm:
        raise InvalidParams(
            f"power.{op} requires confirm=True (hardware-destructive)",
            data={"hint": "pass confirm=True to acknowledge"},
        )


def _reply(target: str, cmd: str, ch: SerialChannel, reply: str) -> ShellReply:
    return ShellReply(
        target=target, cmd=cmd, port=ch.port, baud=ch.baud,
        reply=reply, reply_len=len(reply),
    )


# ---- read-only tools (no confirm) ----------------------------------


async def power_help(fleet: Fleet, target: str) -> ShellReply:
    """List the MCU debug shell's commands (`help`)."""
    ch = _resolve_mcu_channel(fleet, target)
    reply = await p18.send(ch.port, ch.baud, "help")
    return _reply(target, "help", ch, reply)


async def power_identify(fleet: Fleet, target: str) -> ShellReply:
    """MCU H/W + F/W revisions, build type (`id`)."""
    ch = _resolve_mcu_channel(fleet, target)
    reply = await p18.send(ch.port, ch.baud, "id")
    return _reply(target, "id", ch, reply)


async def power_identify_dates(fleet: Fleet, target: str) -> ShellReply:
    """MCU + CPLD build date and time (`id date`)."""
    ch = _resolve_mcu_channel(fleet, target)
    reply = await p18.send(ch.port, ch.baud, "id date")
    return _reply(target, "id date", ch, reply)


async def power_sensors(fleet: Fleet, target: str) -> ShellReply:
    """One-shot voltage + temperature read (`v`). Reply is
    human-formatted ASCII -- the wire `$vXXYY...` form is on UART1
    (`sys.mcu_cmd cmd="v"`), not on the debug shell."""
    ch = _resolve_mcu_channel(fleet, target)
    reply = await p18.send(ch.port, ch.baud, "v")
    return _reply(target, "v", ch, reply)


# ---- destructive tools (confirm: true) -----------------------------


async def power_toggle_stream(
    fleet: Fleet, target: str, *,
    watch_s: float = 0.0, confirm: bool = False,
) -> ShellReply | StreamCapture:
    """Toggle the MCU's continuous-emission state (`q`).

    With `watch_s > 0`, toggle on, capture stream for that many
    seconds, toggle back off, return the captured bytes.

    With `watch_s = 0` (default), just send `q` once and return its
    immediate response -- caller is responsible for sending `q` again
    to disable the stream when done. The bracketed form is usually
    safer.
    """
    _require_confirm(confirm, "toggle_stream")
    ch = _resolve_mcu_channel(fleet, target)
    if watch_s > 0:
        captured = await p18.stream_capture(ch.port, ch.baud, watch_s)
        return StreamCapture(
            target=target, port=ch.port, baud=ch.baud,
            watch_s=watch_s, captured_bytes=len(captured),
            captured=captured,
        )
    reply = await p18.send(ch.port, ch.baud, "q")
    return _reply(target, "q", ch, reply)


async def power_on(
    fleet: Fleet, target: str, *, confirm: bool = False,
) -> ShellReply:
    """Power up all supplies (`p`).

    Boots a powered-off X5000. **If the box is already on, this
    issues a hard reset.** Hardware-destructive."""
    _require_confirm(confirm, "on")
    ch = _resolve_mcu_channel(fleet, target)
    reply = await p18.send(ch.port, ch.baud, "p", idle_s=2.0)
    return _reply(target, "p", ch, reply)


async def power_off(
    fleet: Fleet, target: str, *, confirm: bool = False,
) -> ShellReply:
    """Shut down all supplies (`s`). Hardware-destructive."""
    _require_confirm(confirm, "off")
    ch = _resolve_mcu_channel(fleet, target)
    reply = await p18.send(ch.port, ch.baud, "s", idle_s=2.0)
    return _reply(target, "s", ch, reply)


async def power_shell(
    fleet: Fleet, target: str, *,
    cmd: str, confirm: bool = False,
) -> ShellReply:
    """Generic MCU debug-shell passthrough. Anything other than the
    documented `help` / `id` / `id date` / `v` / `q` / `p` / `s`
    commands is untested. Hardware-destructive (could be `s` etc.);
    requires `confirm=True`."""
    _require_confirm(confirm, "shell")
    if not cmd or not cmd.strip():
        raise InvalidParams("cmd must be a non-empty shell command string")
    ch = _resolve_mcu_channel(fleet, target)
    reply = await p18.send(ch.port, ch.baud, cmd)
    return _reply(target, cmd, ch, reply)


# Re-export so server.py's `from ..tools import power as power_tool`
# can reach all the result types in one import.
__all__ = [
    "ShellReply",
    "StreamCapture",
    "power_help",
    "power_identify",
    "power_identify_dates",
    "power_off",
    "power_on",
    "power_sensors",
    "power_shell",
    "power_toggle_stream",
]
