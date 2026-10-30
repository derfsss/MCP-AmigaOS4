"""`--doctor` checks: config discovery, target settings, MCPd version
comparison and MCP client registration, each with a fix."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from amiga_fleet_mcp import __version__, doctor
from amiga_fleet_mcp.config import (
    Config,
    McpdChannel,
    ServerConfig,
    TargetChannels,
    TargetConfig,
)
from amiga_fleet_mcp.fleet import Fleet


def _target(kind: str = "remote") -> TargetConfig:
    return TargetConfig(
        type=kind,  # type: ignore[arg-type]
        channels=TargetChannels(mcpd=McpdChannel(endpoint="127.0.0.1:1")))


# ---------- config ---------------------------------------------------


def test_missing_config_points_at_init(tmp_path: Path) -> None:
    check, cfg = doctor.check_config(tmp_path / "nope.toml")
    assert cfg is None
    assert check.status == "fail"
    assert "--init" in (check.fix or "")


def test_broken_config_fails_with_a_fix(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text("[server\n", encoding="utf-8")
    check, cfg = doctor.check_config(p)
    assert cfg is None and check.status == "fail"
    assert "--init --force" in (check.fix or "")


def test_good_config_names_its_source(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text('[targets.a]\ntype = "remote"\n', encoding="utf-8")
    check, cfg = doctor.check_config(p)
    assert check.status == "ok" and cfg is not None
    assert "--config" in check.what


# ---------- targets --------------------------------------------------


def test_no_targets_fails() -> None:
    [check] = doctor.check_targets(Config())
    assert check.status == "fail"


def test_several_targets_without_default_warns() -> None:
    checks = doctor.check_targets(Config(targets={"a": _target(), "b": _target()}))
    assert [c.status for c in checks] == ["ok", "warn"]
    assert "default_target" in (checks[1].fix or "")


def test_unknown_default_target_fails() -> None:
    cfg = Config(server=ServerConfig(default_target="zz"), targets={"a": _target()})
    assert doctor.check_targets(cfg)[-1].status == "fail"


# ---------- reachability and versions --------------------------------


class _FakeMcpd:
    def __init__(self, reply: dict[str, Any] | Exception) -> None:
        self.reply = reply

    async def request(self, method: str, *_a: Any, **_k: Any) -> dict[str, Any]:
        assert method == "proto.capabilities"
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


async def _probe(monkeypatch: pytest.MonkeyPatch, reply: dict[str, Any] | Exception,
                 kind: str = "remote") -> doctor.Check:
    fleet = Fleet(Config(targets={"t": _target(kind)}))
    monkeypatch.setattr(fleet, "mcpd", lambda _name: _FakeMcpd(reply))
    return await doctor._probe(fleet, "t")


@pytest.mark.asyncio
async def test_matching_mcpd_is_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    c = await _probe(monkeypatch, {"server": f"mcpd/{__version__}"})
    assert c.status == "ok"


@pytest.mark.asyncio
async def test_older_mcpd_says_update_the_amiga(monkeypatch: pytest.MonkeyPatch) -> None:
    c = await _probe(monkeypatch, {"server": "mcpd/1.3"})
    assert c.status == "warn"
    assert "MCPd-<version>.lha" in (c.fix or "")


@pytest.mark.asyncio
async def test_newer_mcpd_says_update_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    c = await _probe(monkeypatch, {"server": "mcpd/99.0"})
    assert c.status == "warn"
    assert "git pull" in (c.fix or "")


@pytest.mark.asyncio
async def test_stopped_qemu_guest_is_only_a_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = await _probe(monkeypatch, ConnectionRefusedError("refused"), kind="qemu")
    assert c.status == "warn"
    assert "qemu_start" in (c.fix or "")


@pytest.mark.asyncio
async def test_unreachable_real_machine_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    from amiga_fleet_mcp.errors import TargetError
    err = TargetError("MCPd not reachable at 127.0.0.1:1",
                      data={"endpoint": "127.0.0.1:1", "error": "refused"})
    c = await _probe(monkeypatch, err)
    assert c.status == "fail"
    assert c.what.endswith("unreachable: refused")


# ---------- MCP clients ----------------------------------------------


def test_no_client_found_gives_the_launch_command(tmp_path: Path) -> None:
    checks = doctor.check_clients(
        which=lambda _n: None, run=lambda _c: None,
        desktop_config=tmp_path / "absent" / "claude_desktop_config.json")
    [c] = checks
    assert c.status == "warn"
    assert "amiga-fleet-mcp" in (c.fix or "")


def test_claude_code_registration_is_detected(tmp_path: Path) -> None:
    absent = tmp_path / "absent" / "x.json"
    listed = doctor.check_clients(
        which=lambda _n: "claude",
        run=lambda _c: "amiga-fleet: uv --directory /h run amiga-fleet-mcp - Connected",
        desktop_config=absent)
    assert [c.status for c in listed] == ["ok"]
    missing = doctor.check_clients(
        which=lambda _n: "claude", run=lambda _c: "No MCP servers configured.",
        desktop_config=absent)
    assert missing[0].status == "warn"
    assert "claude mcp add amiga-fleet --" in (missing[0].fix or "")


def test_claude_desktop_registration_is_detected(tmp_path: Path) -> None:
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    [c] = doctor.check_clients(which=lambda _n: None, run=lambda _c: None,
                               desktop_config=cfg)
    assert c.status == "warn" and "mcpServers" in (c.fix or "")
    cfg.write_text(json.dumps({"mcpServers": {"amiga": {
        "command": "uv", "args": ["run", "amiga-fleet-mcp"]}}}), encoding="utf-8")
    [c] = doctor.check_clients(which=lambda _n: None, run=lambda _c: None,
                               desktop_config=cfg)
    assert c.status == "ok"


def test_launch_command_from_a_checkout_uses_uv() -> None:
    argv = doctor.launch_command()
    assert argv[0] == "uv" and argv[-1] == "amiga-fleet-mcp"
    assert Path(argv[2], "pyproject.toml").is_file()
