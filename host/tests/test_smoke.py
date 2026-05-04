"""Smoke tests.

Verifies the package imports, the version is exposed, and a FastMCP
instance can be constructed against a minimal config (no real targets).
"""

from __future__ import annotations

from pathlib import Path

from amiga_fleet_mcp import __version__
from amiga_fleet_mcp.archive import Archive
from amiga_fleet_mcp.config import Config
from amiga_fleet_mcp.fleet import Fleet
from amiga_fleet_mcp.server import SERVER_NAME, build_server


def test_version_is_a_string() -> None:
    assert isinstance(__version__, str)
    # Project uses AmigaOS-style major.minor versioning (e.g. "1.0").
    assert __version__.count(".") >= 1


def test_server_name() -> None:
    assert SERVER_NAME == "amiga-fleet"


def test_build_server_returns_instance(tmp_path: Path) -> None:
    config = Config()
    fleet = Fleet(config)
    archive = Archive(tmp_path / "archive")
    server = build_server(fleet, archive)
    assert server is not None
    assert server.name == SERVER_NAME
