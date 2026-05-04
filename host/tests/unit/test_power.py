"""power.* tool tests with a fake P18 transport.

Replaces amiga_fleet_mcp.transports.p18 module functions with
stubs so the tests don't actually open a serial port. Verifies:

- read-only tools succeed without confirm
- hardware-destructive tools (on / off / toggle_stream / shell)
  reject calls without confirm=True
- a missing or disabled [channels.mcu] block raises NotCapable
- an active serial.* capture on the same port raises NotCapable
- toggle_stream(watch_s>0) takes the stream-capture path
"""

from __future__ import annotations

from unittest import mock

import pytest

from amiga_fleet_mcp.config import (
    Config,
    McpdChannel,
    SerialChannel,
    TargetChannels,
    TargetConfig,
)
from amiga_fleet_mcp.errors import InvalidParams, NotCapable
from amiga_fleet_mcp.fleet import Fleet
from amiga_fleet_mcp.tools import power as power_tool


def _fleet(*, mcu_port: str | None = "COM5",
           mcu_enabled: bool = True) -> Fleet:
    mcu = (
        SerialChannel(enabled=mcu_enabled, port=mcu_port, baud=38400)
        if mcu_port is not None else None
    )
    cfg = Config(targets={
        "x5000": TargetConfig(
            type="remote",
            channels=TargetChannels(
                mcpd=McpdChannel(endpoint="192.168.0.10:4322"),
                mcu=mcu,
            ),
        ),
    })
    return Fleet(cfg)


# ---- helpers --------------------------------------------------------


@pytest.fixture
def fake_send():
    """Replace p18.send / p18.stream_capture with stubs that record
    their args and return canned ASCII replies."""
    calls: list[tuple[str, str, str]] = []

    async def _fake_send(port: str, baud: int, cmd: str, *,
                         idle_s: float = 1.0) -> str:
        calls.append(("send", port, cmd))
        replies = {
            "help": "help / id / id date / v / q / p / s / >>",
            "id":   "Cyrus Plus / MCU FW v2.5 / Hardware v2.2 / >>",
            "id date": "MCU build: Jun 5 2015 / CPLD build: 2015-05-06 / >>",
            "v":    "cpld_3v3: 3.30V / cpu_temp: 53C / pcb_temp: 37C / >>",
            "q":    "Toggling printing.. OK >>",
            "p":    "Powering up.. Enabled PS_ON / >>",
            "s":    "Shutting down.. ATX supply turned off OK / >>",
        }
        return replies.get(cmd, f"ack {cmd} >>")

    async def _fake_stream(port: str, baud: int, watch_s: float) -> str:
        calls.append(("stream", port, f"watch={watch_s}s"))
        return f"<{watch_s}s of streamed sensor blocks>"

    with mock.patch.object(power_tool.p18, "send", _fake_send), \
         mock.patch.object(power_tool.p18, "stream_capture", _fake_stream):
        yield calls


# ---- read-only tools (no confirm) -----------------------------------


@pytest.mark.asyncio
async def test_power_help_reads_shell(fake_send):
    fleet = _fleet()
    res = await power_tool.power_help(fleet, "x5000")
    assert res.cmd == "help"
    assert res.port == "COM5"
    assert res.baud == 38400
    assert "help" in res.reply
    assert fake_send == [("send", "COM5", "help")]


@pytest.mark.asyncio
async def test_power_identify(fake_send):
    fleet = _fleet()
    res = await power_tool.power_identify(fleet, "x5000")
    assert res.cmd == "id"
    assert "Cyrus Plus" in res.reply


@pytest.mark.asyncio
async def test_power_identify_dates(fake_send):
    fleet = _fleet()
    res = await power_tool.power_identify_dates(fleet, "x5000")
    assert res.cmd == "id date"
    assert fake_send == [("send", "COM5", "id date")]


@pytest.mark.asyncio
async def test_power_sensors(fake_send):
    fleet = _fleet()
    res = await power_tool.power_sensors(fleet, "x5000")
    assert res.cmd == "v"
    assert "cpu_temp" in res.reply


# ---- destructive tools without confirm -----------------------------


@pytest.mark.asyncio
async def test_power_on_requires_confirm(fake_send):
    fleet = _fleet()
    with pytest.raises(InvalidParams):
        await power_tool.power_on(fleet, "x5000")
    assert fake_send == []  # no command sent


@pytest.mark.asyncio
async def test_power_off_requires_confirm(fake_send):
    fleet = _fleet()
    with pytest.raises(InvalidParams):
        await power_tool.power_off(fleet, "x5000")
    assert fake_send == []


@pytest.mark.asyncio
async def test_power_toggle_stream_requires_confirm(fake_send):
    fleet = _fleet()
    with pytest.raises(InvalidParams):
        await power_tool.power_toggle_stream(fleet, "x5000")
    assert fake_send == []


@pytest.mark.asyncio
async def test_power_shell_requires_confirm(fake_send):
    fleet = _fleet()
    with pytest.raises(InvalidParams):
        await power_tool.power_shell(fleet, "x5000", cmd="help")
    assert fake_send == []


# ---- destructive tools with confirm -------------------------------


@pytest.mark.asyncio
async def test_power_on_with_confirm(fake_send):
    fleet = _fleet()
    res = await power_tool.power_on(fleet, "x5000", confirm=True)
    assert res.cmd == "p"
    assert "Powering up" in res.reply
    assert fake_send == [("send", "COM5", "p")]


@pytest.mark.asyncio
async def test_power_off_with_confirm(fake_send):
    fleet = _fleet()
    res = await power_tool.power_off(fleet, "x5000", confirm=True)
    assert res.cmd == "s"
    assert fake_send == [("send", "COM5", "s")]


@pytest.mark.asyncio
async def test_power_toggle_stream_immediate_with_confirm(fake_send):
    fleet = _fleet()
    res = await power_tool.power_toggle_stream(
        fleet, "x5000", confirm=True,
    )
    assert isinstance(res, power_tool.ShellReply)
    assert res.cmd == "q"
    assert fake_send == [("send", "COM5", "q")]


@pytest.mark.asyncio
async def test_power_toggle_stream_watch_returns_capture(fake_send):
    fleet = _fleet()
    res = await power_tool.power_toggle_stream(
        fleet, "x5000", watch_s=2.5, confirm=True,
    )
    assert isinstance(res, power_tool.StreamCapture)
    assert res.watch_s == 2.5
    assert res.captured_bytes > 0
    assert "2.5s" in res.captured
    assert fake_send == [("stream", "COM5", "watch=2.5s")]


@pytest.mark.asyncio
async def test_power_shell_passthrough(fake_send):
    fleet = _fleet()
    res = await power_tool.power_shell(
        fleet, "x5000", cmd="some-debug-cmd", confirm=True,
    )
    assert res.cmd == "some-debug-cmd"
    assert fake_send == [("send", "COM5", "some-debug-cmd")]


@pytest.mark.asyncio
async def test_power_shell_rejects_empty_cmd(fake_send):
    fleet = _fleet()
    with pytest.raises(InvalidParams):
        await power_tool.power_shell(
            fleet, "x5000", cmd="   ", confirm=True,
        )
    assert fake_send == []


# ---- channel resolution edge cases ---------------------------------


@pytest.mark.asyncio
async def test_no_mcu_channel_raises_not_capable(fake_send):
    fleet = _fleet(mcu_port=None)
    with pytest.raises(NotCapable):
        await power_tool.power_help(fleet, "x5000")


@pytest.mark.asyncio
async def test_disabled_mcu_channel_raises_not_capable(fake_send):
    fleet = _fleet(mcu_enabled=False)
    with pytest.raises(NotCapable):
        await power_tool.power_help(fleet, "x5000")


@pytest.mark.asyncio
async def test_empty_port_raises_not_capable(fake_send):
    fleet = _fleet(mcu_port="")
    with pytest.raises(NotCapable):
        await power_tool.power_help(fleet, "x5000")


@pytest.mark.asyncio
async def test_active_capture_blocks_power_call(fake_send):
    fleet = _fleet()
    # Stub a capture on the same target+channel that reports running.
    class _FakeCapture:
        running = True
    fleet.serial_captures._captures[("x5000", "mcu")] = _FakeCapture()  # type: ignore[assignment]
    with pytest.raises(NotCapable, match="being captured"):
        await power_tool.power_help(fleet, "x5000")
