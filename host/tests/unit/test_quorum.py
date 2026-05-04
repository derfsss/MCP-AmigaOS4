"""Unit tests for fleet.barrier + fleet.quorum_run (phase 11b)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from amiga_fleet_mcp.config import (
    Config,
    McpdChannel,
    TargetChannels,
    TargetConfig,
)
from amiga_fleet_mcp.errors import InvalidParams, TargetError
from amiga_fleet_mcp.fleet import Fleet
from amiga_fleet_mcp.tools import fleet as fleet_tool


class _SlowMcpd:
    """MCPd transport stub that sleeps before responding."""

    def __init__(self, delay: float, reply: Any = None,
                 fail: bool = False) -> None:
        self.delay = delay
        self.reply = reply or {"raw": "Kickstart 54.57", "kickstart": "54.57",
                               "workbench": None}
        self.fail = fail

    async def request(self, method: str, params: dict | None = None,
                      timeout_s: float = 30.0) -> Any:
        await asyncio.sleep(self.delay)
        if self.fail:
            raise TargetError("simulated", data={"method": method})
        return self.reply


def _fleet(transports: dict[str, object]) -> Fleet:
    cfg = Config(
        targets={
            n: TargetConfig(
                type="qemu",
                channels=TargetChannels(
                    mcpd=McpdChannel(endpoint=f"127.0.0.1:{4322 + i}"),
                ),
            )
            for i, n in enumerate(transports)
        }
    )
    fleet = Fleet(cfg)
    fleet._mcpd.update(transports)  # type: ignore[arg-type]
    return fleet


# ---------- barrier -----------------------------------------------


@pytest.mark.asyncio
async def test_barrier_per_target_timeout() -> None:
    fleet = _fleet({
        "fast": _SlowMcpd(0.01),
        "slow": _SlowMcpd(2.0),
    })
    r = await fleet_tool.fleet_barrier(
        fleet, "sys.version", per_target_timeout_s=0.2
    )
    assert r.results["fast"].error is None
    assert r.results["fast"].ok is not None
    assert r.results["slow"].error is not None
    assert r.results["slow"].error["code"] == -32002


@pytest.mark.asyncio
async def test_barrier_all_succeed() -> None:
    fleet = _fleet({"a": _SlowMcpd(0.01), "b": _SlowMcpd(0.01)})
    r = await fleet_tool.fleet_barrier(
        fleet, "sys.version", per_target_timeout_s=2.0
    )
    assert all(e.error is None for e in r.results.values())


# ---------- quorum_run --------------------------------------------


@pytest.mark.asyncio
async def test_quorum_returns_early_when_reached() -> None:
    """When quorum=1 and one target is fast, the slow one gets cancelled."""
    fast = _SlowMcpd(0.01)
    slow = _SlowMcpd(5.0)  # would block forever vs the test timeout
    fleet = _fleet({"fast": fast, "slow": slow})
    start = asyncio.get_event_loop().time()
    r = await fleet_tool.fleet_quorum_run(
        fleet, "sys.version", quorum=1, overall_timeout_s=2.0
    )
    elapsed = asyncio.get_event_loop().time() - start
    assert r.reached is True
    assert elapsed < 1.0, f"quorum=1 should return promptly, took {elapsed:.2f}s"
    assert r.results["fast"].error is None
    # slow should be cancelled
    assert "slow" in r.results
    assert r.results["slow"].error is not None


@pytest.mark.asyncio
async def test_quorum_unmet_due_to_failures() -> None:
    fleet = _fleet({
        "a": _SlowMcpd(0.01, fail=True),
        "b": _SlowMcpd(0.01, fail=True),
        "c": _SlowMcpd(0.01),
    })
    r = await fleet_tool.fleet_quorum_run(
        fleet, "sys.version", quorum=2, overall_timeout_s=2.0
    )
    # Only one target succeeds; quorum=2 not met.
    assert r.reached is False
    assert sum(1 for e in r.results.values() if e.error is None) == 1


@pytest.mark.asyncio
async def test_quorum_validates_inputs() -> None:
    fleet = _fleet({"a": _SlowMcpd(0)})
    with pytest.raises(InvalidParams):
        await fleet_tool.fleet_quorum_run(fleet, "sys.version", quorum=0)
    with pytest.raises(InvalidParams):
        await fleet_tool.fleet_quorum_run(fleet, "sys.version", quorum=5)
    with pytest.raises(InvalidParams):
        await fleet_tool.fleet_quorum_run(fleet, "wat.no", quorum=1)
