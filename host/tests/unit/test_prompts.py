"""MCP prompts: listed over the wire, rendered with their arguments, and
naming only tools that exist."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from mcp import Client

from amiga_fleet_mcp.archive import Archive
from amiga_fleet_mcp.config import Config
from amiga_fleet_mcp.fleet import Fleet
from amiga_fleet_mcp.prompts import PROMPTS
from amiga_fleet_mcp.server import build_server


def _server(tmp_path: Path):  # type: ignore[no-untyped-def]
    return build_server(Fleet(Config()), Archive(tmp_path / "archive"))


@pytest.mark.asyncio
async def test_prompts_are_listed(tmp_path: Path) -> None:
    async with Client(_server(tmp_path), mode="legacy") as client:  # type: ignore[arg-type]
        listed = await client.list_prompts()
        by_name = {p.name: p for p in listed.prompts}
        assert set(by_name) == set(PROMPTS)
        deploy = by_name["deploy_and_run"]
        required = {a.name for a in deploy.arguments or [] if a.required}
        assert required == {"local_path"}
        assert all(p.description for p in listed.prompts)


@pytest.mark.asyncio
async def test_prompt_renders_its_arguments(tmp_path: Path) -> None:
    async with Client(_server(tmp_path), mode="legacy") as client:  # type: ignore[arg-type]
        r = await client.get_prompt(
            "deploy_and_run",
            {"local_path": "build/hello", "target": "a1222", "remote_dir": "RAM:T"})
        text = r.messages[0].content.text  # type: ignore[union-attr]
        assert "`build/hello`" in text
        assert "target `a1222`" in text
        assert "`RAM:T`" in text

        r = await client.get_prompt("investigate_crash", {})
        text = r.messages[0].content.text  # type: ignore[union-attr]
        assert "fleet_list_targets" in text
        assert "no details" in text


@pytest.mark.asyncio
async def test_prompts_name_only_registered_tools(tmp_path: Path) -> None:
    async with Client(_server(tmp_path), mode="legacy") as client:  # type: ignore[arg-type]
        tools = {t.name for t in (await client.list_tools()).tools}
    for name, (fn, _title, _desc) in PROMPTS.items():
        text = fn(local_path="x") if name == "deploy_and_run" else fn()
        # Result fields the prompts point at share the tool-name shape.
        named = set(re.findall(r"`([a-z]+_[a-z_]+)`", text)) - {"raw_size"}
        assert named, name
        missing = named - tools
        assert not missing, f"{name} names unknown tools: {sorted(missing)}"
