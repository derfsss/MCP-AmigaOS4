"""Config loader + Pydantic schema."""

from __future__ import annotations

from pathlib import Path

import pytest

from amiga_fleet_mcp.config import Config, load_config

SAMPLE = """
[server]
log_level = "debug"
archive_root = "C:/tmp/archive"

[paths]
qemu_runner = "C:/qemu-runner"

[targets.qemu-pegasos2]
type = "qemu"
display_name = "QEMU Pegasos2"
machine = "pegasos2"

[targets.qemu-pegasos2.channels.mcpd]
enabled = true
endpoint = "127.0.0.1:4322"

[targets.qemu-pegasos2.channels.qmp]
enabled = true
endpoint = "127.0.0.1:14422"
"""


def test_load_minimal(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text(SAMPLE)
    cfg = load_config(p)
    assert cfg.server.log_level == "debug"
    assert "qemu-pegasos2" in cfg.targets
    t = cfg.targets["qemu-pegasos2"]
    assert t.type == "qemu"
    assert t.channels.mcpd is not None
    assert t.channels.mcpd.host == "127.0.0.1"
    assert t.channels.mcpd.port == 4322
    assert t.channels.qmp is not None
    assert t.channels.qmp.port == 14422


def test_unknown_target_raises() -> None:
    cfg = Config()
    with pytest.raises(KeyError):
        cfg.target("does-not-exist")


def test_endpoint_validation() -> None:
    from amiga_fleet_mcp.config import McpdChannel

    with pytest.raises(ValueError):
        McpdChannel(endpoint="bare")
    with pytest.raises(ValueError):
        McpdChannel(endpoint="host:abc")


def test_default_config_path_env(monkeypatch: pytest.MonkeyPatch,
                                 tmp_path: Path) -> None:
    from amiga_fleet_mcp.config import default_config_path

    p = tmp_path / "alt.toml"
    monkeypatch.setenv("AMIGA_FLEET_CONFIG", str(p))
    assert default_config_path() == p
