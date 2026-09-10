"""sys.debug_ring unit tests via fake transport.

The tool is a thin wrapper over `c:DumpDebugBuffer`; the interesting
behaviour is line-trim + truncation flag + UTC timestamp. Real ring
output isn't worth fixturing (target-specific) — synthetic line
sequences are sufficient.
"""

from __future__ import annotations

import re

import pytest

from amiga_fleet_mcp.config import (
    Config,
    McpdChannel,
    TargetChannels,
    TargetConfig,
)
from amiga_fleet_mcp.fleet import Fleet
from amiga_fleet_mcp.tools import sys as sys_tool


class FakeMcpd:
    def __init__(self, output: str = "") -> None:
        self.output = output
        self.calls: list[tuple[str, dict]] = []

    async def request(self, method, params=None, timeout_s=30.0):
        self.calls.append((method, params or {}))
        if method == "exec.cmd":
            return {"output": self.output, "exit_code": 0}
        raise AssertionError(f"unexpected method: {method!r}")


def _fleet(output: str = "") -> tuple[Fleet, FakeMcpd]:
    fleet = Fleet(Config(targets={
        "tgt": TargetConfig(
            type="remote",
            channels=TargetChannels(
                mcpd=McpdChannel(endpoint="192.168.0.99:4322"),
            ),
        ),
    }))
    fake = FakeMcpd(output)
    fleet._mcpd["tgt"] = fake  # type: ignore[assignment]
    return fleet, fake


@pytest.mark.asyncio
async def test_debug_ring_empty_output():
    fleet, fake = _fleet("")

    res = await sys_tool.sys_debug_ring(fleet, "tgt")

    assert res.lines == []
    assert res.truncated is False
    assert res.raw_size == 0
    assert fake.calls[0][0] == "exec.cmd"
    assert fake.calls[0][1]["command"] == "C:DumpDebugBuffer"


@pytest.mark.asyncio
async def test_debug_ring_returns_all_lines_when_under_limit():
    body = "line1\nline2\nline3\n"
    fleet, _ = _fleet(body)

    res = await sys_tool.sys_debug_ring(fleet, "tgt", max_lines=500)

    assert res.lines == ["line1", "line2", "line3"]
    assert res.truncated is False
    assert res.raw_size == len(body)


@pytest.mark.asyncio
async def test_debug_ring_trims_to_most_recent_max_lines():
    # 50 lines, ask for last 10 — should keep 41..50 and flag truncated.
    body = "\n".join(f"l{i}" for i in range(1, 51)) + "\n"
    fleet, _ = _fleet(body)

    res = await sys_tool.sys_debug_ring(fleet, "tgt", max_lines=10)

    assert len(res.lines) == 10
    assert res.lines[0] == "l41"
    assert res.lines[-1] == "l50"
    assert res.truncated is True
    assert res.raw_size == len(body)


@pytest.mark.asyncio
async def test_debug_ring_max_lines_floor_one():
    # max_lines=0 is silently bumped to 1 — surprises caused by an
    # empty result list when the ring had content are worse than
    # an off-by-one.
    body = "a\nb\nc\n"
    fleet, _ = _fleet(body)

    res = await sys_tool.sys_debug_ring(fleet, "tgt", max_lines=0)

    assert res.lines == ["c"]
    assert res.truncated is True


@pytest.mark.asyncio
async def test_debug_ring_captured_at_is_iso8601_utc():
    fleet, _ = _fleet("x\n")

    res = await sys_tool.sys_debug_ring(fleet, "tgt")

    # 2026-05-10T12:34:56Z shape, UTC suffix.
    assert re.match(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", res.captured_at,
    )


@pytest.mark.asyncio
async def test_debug_ring_handles_crlf_line_endings():
    """AmigaDOS occasionally emits \\r\\n; splitlines() handles both."""
    body = "alpha\r\nbeta\r\ngamma\r\n"
    fleet, _ = _fleet(body)

    res = await sys_tool.sys_debug_ring(fleet, "tgt")

    assert res.lines == ["alpha", "beta", "gamma"]


@pytest.mark.asyncio
async def test_debug_ring_since_s_currently_accepted_but_ignored():
    """The plan reserves `since_s` for a future timestamp-aware
    trim but doesn't act on it today. Pin the contract: passing
    `since_s` doesn't change the output shape."""
    body = "\n".join(f"x{i}" for i in range(20)) + "\n"
    fleet, _ = _fleet(body)

    a = await sys_tool.sys_debug_ring(fleet, "tgt", since_s=1.0,
                                      max_lines=5)
    b = await sys_tool.sys_debug_ring(fleet, "tgt", since_s=600.0,
                                      max_lines=5)

    assert a.lines == b.lines
    assert len(a.lines) == 5
