"""Unit tests for fleet.run_on_all aggregation (phase 4 - MCPd transport)."""

from __future__ import annotations

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


class _RaisingMcpd:
    async def request(self, method: str, params: dict | None = None,
                      timeout_s: float = 30.0) -> Any:
        raise TargetError("simulated failure", data={"method": method})


class _OkMcpd:
    async def request(self, method: str, params: dict | None = None,
                      timeout_s: float = 30.0) -> Any:
        if method == "sys.version":
            return {"raw": "Kickstart 54.57, Workbench 53.21",
                    "kickstart": "54.57", "workbench": "53.21"}
        return {}


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


@pytest.mark.asyncio
async def test_run_on_all_partial_failure() -> None:
    fleet = _fleet({"a": _OkMcpd(), "b": _RaisingMcpd()})
    r = await fleet_tool.fleet_run_on_all(fleet, "sys.version")
    assert set(r.results.keys()) == {"a", "b"}
    assert r.results["a"].error is None
    assert r.results["a"].ok is not None
    assert r.results["b"].ok is None
    assert r.results["b"].error is not None
    assert r.results["b"].error["code"] == -32001


@pytest.mark.asyncio
async def test_run_on_all_unknown_method_raises() -> None:
    fleet = _fleet({"a": _OkMcpd()})
    with pytest.raises(InvalidParams):
        await fleet_tool.fleet_run_on_all(fleet, "wat.no")


@pytest.mark.asyncio
async def test_run_on_all_specific_targets_only() -> None:
    fleet = _fleet({"a": _OkMcpd(), "b": _OkMcpd(), "c": _OkMcpd()})
    r = await fleet_tool.fleet_run_on_all(fleet, "sys.version", targets=["b", "c"])
    assert set(r.results.keys()) == {"b", "c"}


@pytest.mark.asyncio
async def test_list_targets_summary() -> None:
    fleet = _fleet({"a": _OkMcpd(), "b": _OkMcpd()})
    out = await fleet_tool.fleet_list_targets(fleet)
    names = sorted(t.name for t in out.targets)
    assert names == ["a", "b"]
    assert all(t.mcpd is not None for t in out.targets)


@pytest.mark.asyncio
async def test_list_targets_tag_filter() -> None:
    cfg = Config(targets={
        "qemu-1": TargetConfig(
            type="qemu",
            tags=["qemu", "pegasos2"],
            channels=TargetChannels(
                mcpd=McpdChannel(endpoint="127.0.0.1:4322"),
            ),
        ),
        "real-x5000": TargetConfig(
            type="remote",
            tags=["real-hw", "x5000"],
            channels=TargetChannels(
                mcpd=McpdChannel(endpoint="192.168.0.25:4322"),
            ),
        ),
    })
    fleet = Fleet(cfg)
    fleet._mcpd["qemu-1"] = _OkMcpd()  # type: ignore[assignment]
    fleet._mcpd["real-x5000"] = _OkMcpd()  # type: ignore[assignment]

    # No filter: both.
    assert fleet.list_targets() == ["qemu-1", "real-x5000"]
    # Filter to qemu only.
    assert fleet.list_targets(tags=["qemu"]) == ["qemu-1"]
    assert fleet.list_targets(tags=["real-hw"]) == ["real-x5000"]
    # Tag that nobody carries.
    assert fleet.list_targets(tags=["nonexistent"]) == []
    # AND-match: both qemu AND pegasos2 must be set.
    assert fleet.list_targets(tags=["qemu", "pegasos2"]) == ["qemu-1"]


@pytest.mark.asyncio
async def test_run_on_all_with_tag_filter() -> None:
    cfg = Config(targets={
        "qemu-1": TargetConfig(
            type="qemu", tags=["qemu"],
            channels=TargetChannels(
                mcpd=McpdChannel(endpoint="127.0.0.1:4322"),
            ),
        ),
        "real-x5000": TargetConfig(
            type="remote", tags=["real-hw"],
            channels=TargetChannels(
                mcpd=McpdChannel(endpoint="192.168.0.25:4322"),
            ),
        ),
    })
    fleet = Fleet(cfg)
    fleet._mcpd["qemu-1"] = _OkMcpd()  # type: ignore[assignment]
    fleet._mcpd["real-x5000"] = _OkMcpd()  # type: ignore[assignment]

    r = await fleet_tool.fleet_run_on_all(
        fleet, "sys.version", tags=["qemu"]
    )
    assert set(r.results.keys()) == {"qemu-1"}
    r = await fleet_tool.fleet_run_on_all(
        fleet, "sys.version", tags=["real-hw"]
    )
    assert set(r.results.keys()) == {"real-x5000"}
