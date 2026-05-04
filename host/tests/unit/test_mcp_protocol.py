"""End-to-end MCP protocol smoke test.

Spins up the FastMCP server with no real targets configured, then
acts as an MCP client over the in-memory transport. Exercises:
  - tools/list
  - resources/list (templates + concrete URIs)
  - reading amiga://fleet/targets

Verifies the wire works without needing QEMU + MCPd up. Doesn't
exercise the daemon-bound tools (those need a Fleet with a live
MCPd transport - covered by the QEMU validation scripts).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from amiga_fleet_mcp.archive import Archive
from amiga_fleet_mcp.config import (
    Config,
    McpdChannel,
    QmpChannel,
    TargetChannels,
    TargetConfig,
)
from amiga_fleet_mcp.fleet import Fleet
from amiga_fleet_mcp.server import build_server


def _build_with_two_targets(tmp_path: Path) -> tuple[Fleet, Archive]:
    cfg = Config(
        targets={
            "qemu-pegasos2": TargetConfig(
                type="qemu", display_name="QEMU Pegasos2",
                channels=TargetChannels(
                    mcpd=McpdChannel(endpoint="127.0.0.1:4322"),
                    qmp=QmpChannel(endpoint="127.0.0.1:14422"),
                ),
            ),
            "x5000-real": TargetConfig(
                type="remote", display_name="X5000 (real hardware)",
                channels=TargetChannels(
                    mcpd=McpdChannel(endpoint="192.168.0.25:4322"),
                ),
            ),
        }
    )
    fleet = Fleet(cfg)
    archive = Archive(tmp_path / "archive")
    return fleet, archive


@pytest.mark.asyncio
async def test_mcp_tools_list_includes_phase_method_set(
    tmp_path: Path,
) -> None:
    fleet, archive = _build_with_two_targets(tmp_path)
    server = build_server(fleet, archive)
    async with create_connected_server_and_client_session(server) as client:
        await client.initialize()
        tools = await client.list_tools()
        names = {t.name for t in tools.tools}
        # Spot-check tools from each phase that we registered.
        for required in [
            "fs_list", "fs_read", "fs_write", "fs_rename", "fs_copy",
            "exec_cmd", "sys_version", "sys_tasks", "sys_libraries",
            "wb_screens", "wb_windows",
            "qemu_start", "qemu_stop", "qemu_screenshot",
            "fleet_list_targets", "fleet_run_on_all",
            "fleet_barrier", "fleet_quorum_run",
            "qemu_savevm", "qemu_loadvm",
            "tests_list_suites", "tests_run_standard_dos_tests",
            "debug_read_registers", "debug_backtrace",
            "debug_task_snapshot", "events_wait",
            "proto_capabilities",
        ]:
            assert required in names, f"tool {required} missing from tools/list"


@pytest.mark.asyncio
async def test_mcp_resources_include_fleet_targets(
    tmp_path: Path,
) -> None:
    fleet, archive = _build_with_two_targets(tmp_path)
    server = build_server(fleet, archive)
    async with create_connected_server_and_client_session(server) as client:
        await client.initialize()
        resources = await client.list_resources()
        # Concrete (non-template) resources should include
        # amiga://runs/ and amiga://fleet/targets.
        uris = {str(r.uri) for r in resources.resources}
        # FastMCP normalises URIs - allow either with or without
        # trailing slash.
        assert any("amiga://runs/" in u for u in uris) or \
               any("amiga://runs" in u for u in uris)
        assert any("amiga://fleet/targets" in u for u in uris)


@pytest.mark.asyncio
async def test_mcp_read_serial_log(tmp_path: Path) -> None:
    """qemu.serial_log resource returns whatever fleet has stored,
    truncated to the last 1 MiB."""
    fleet, archive = _build_with_two_targets(tmp_path)
    # Pre-populate a serial log path on the fleet.
    log_path = tmp_path / "serial.log"
    log_path.write_text("hello qemu\nline 2\n", encoding="utf-8")
    fleet._serial_log_paths["qemu-pegasos2"] = log_path
    server = build_server(fleet, archive)
    async with create_connected_server_and_client_session(server) as client:
        await client.initialize()
        r = await client.read_resource("amiga://qemu-pegasos2/qemu/serial_log")
        text = r.contents[0].text  # type: ignore[union-attr]
        assert "hello qemu" in text
        assert "line 2" in text
        # Empty for a target with no stored path.
        r2 = await client.read_resource("amiga://x5000-real/qemu/serial_log")
        assert r2.contents[0].text == ""  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_mcp_read_amiga_fleet_targets(tmp_path: Path) -> None:
    fleet, archive = _build_with_two_targets(tmp_path)
    server = build_server(fleet, archive)
    async with create_connected_server_and_client_session(server) as client:
        await client.initialize()
        result = await client.read_resource("amiga://fleet/targets")
        assert result.contents
        # Parse the JSON content.
        text = result.contents[0].text  # type: ignore[union-attr]
        rows = json.loads(text)
        assert isinstance(rows, list)
        names = {r["name"] for r in rows}
        assert names == {"qemu-pegasos2", "x5000-real"}
        x5000 = next(r for r in rows if r["name"] == "x5000-real")
        assert x5000["mcpd_endpoint"] == "192.168.0.25:4322"
        assert x5000["qmp_endpoint"] is None
