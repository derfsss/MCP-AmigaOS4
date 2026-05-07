"""Tests for the `--init` wizard."""

from __future__ import annotations

import tomllib

from amiga_fleet_mcp.config import Config, load_config
from amiga_fleet_mcp.setup_wizard import (
    render_toml,
    run_init,
)

# ---------- TOML emitter --------------------------------------------

def test_render_toml_round_trips_through_tomllib():
    data = {
        "server": {"log_level": "info", "archive_root": "C:/tmp/a"},
        "paths": {"qemu_runner": "/home/x/qemu-runner"},
        "targets": {
            "x5000": {
                "type": "remote",
                "tags": ["real-hw", "x5000"],
                "channels": {
                    "mcpd": {"enabled": True, "endpoint": "10.0.0.5:4322"},
                },
            },
        },
    }
    rendered = render_toml(data)
    parsed = tomllib.loads(rendered)
    assert parsed == data


def test_render_toml_escapes_quotes_and_backslashes():
    data = {"server": {"log_dir": 'C:\\path with "quotes"'}}
    rendered = render_toml(data)
    parsed = tomllib.loads(rendered)
    assert parsed["server"]["log_dir"] == 'C:\\path with "quotes"'


def test_render_toml_handles_quoted_keys():
    data = {"targets": {"weird name with spaces": {"type": "remote"}}}
    rendered = render_toml(data)
    parsed = tomllib.loads(rendered)
    assert parsed == data


# ---------- non-interactive end-to-end ------------------------------

def test_non_interactive_produces_loadable_config(tmp_path):
    out = tmp_path / "config.toml"
    rc = run_init(out_path=out, force=True, non_interactive=True)
    assert rc == 0
    assert out.exists()
    cfg = load_config(out)
    assert isinstance(cfg, Config)
    assert cfg.server.mcp_transport == "stdio"


def test_non_interactive_refuses_to_clobber_without_force(tmp_path):
    out = tmp_path / "config.toml"
    out.write_text("# preexisting\n", encoding="utf-8")
    rc = run_init(out_path=out, force=False, non_interactive=True)
    assert rc == 65
    assert out.read_text(encoding="utf-8") == "# preexisting\n"


def test_non_interactive_force_overwrites(tmp_path):
    out = tmp_path / "config.toml"
    out.write_text("garbage that won't parse as toml ===\n", encoding="utf-8")
    rc = run_init(out_path=out, force=True, non_interactive=True)
    assert rc == 0
    load_config(out)


# ---------- scripted interactive flow -------------------------------

class ScriptedPrompt:
    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.asked: list[str] = []

    def __call__(self, q: str) -> str:
        self.asked.append(q)
        if not self.answers:
            raise AssertionError(f"prompt ran out of answers at {q!r}")
        return self.answers.pop(0)


def test_interactive_qemu_target_round_trip(tmp_path):
    """Drive the wizard through one QEMU target and assert the
    resulting file matches what we asked for."""
    answers = [
        # server block: defaults for log_dir, archive_root, then choices
        "",            # log_dir → default
        "",            # archive_root → default
        "info",        # log_level
        "stdio",       # mcp_transport
        # paths block
        "",            # qemu_runner skip → fallback to suggestion (None on tmp)
        "",            # amiga_qemu_tests skip
        "",            # qemu_binary skip
        # targets loop
        "y",           # add a target?
        "qemu-pegasos2",
        "qemu",        # type
        "QEMU P2",     # display_name
        "qemu,pegasos2",  # tags
        "pegasos2",    # machine
        "C:/qemu/p2.json",  # qemu_config
        "4422",        # mcpd port
        "y", "14422",  # qmp on, port
        "n",           # gdb off
        "n",           # add another?
        # default_target picker is auto-set when only one target exists
        # defaults block
        "n",           # configure defaults?
    ]
    prompt = ScriptedPrompt(answers)
    out = tmp_path / "config.toml"
    rc = run_init(
        out_path=out, force=True, non_interactive=False, prompt=prompt,
    )
    assert rc == 0, prompt.asked
    cfg = load_config(out)
    assert "qemu-pegasos2" in cfg.targets
    t = cfg.targets["qemu-pegasos2"]
    assert t.type == "qemu"
    assert t.machine == "pegasos2"
    assert t.channels.mcpd is not None
    assert t.channels.mcpd.endpoint == "127.0.0.1:4422"
    assert t.channels.qmp is not None
    assert t.channels.qmp.endpoint == "127.0.0.1:14422"
    assert t.channels.gdb is None
    assert cfg.server.default_target == "qemu-pegasos2"


def test_interactive_remote_with_mcu_cable(tmp_path):
    answers = [
        # server
        "", "", "info", "stdio",
        # paths
        "", "", "",
        # target
        "y",
        "x5000",
        "remote",
        "X5000",
        "real-hw,x5000",
        "192.168.1.50",
        "4322",
        "y",                  # FTDI cable wired?
        "COM5",               # MCU port
        "n",                  # add another?
        "n",                  # configure defaults
    ]
    prompt = ScriptedPrompt(answers)
    out = tmp_path / "config.toml"
    rc = run_init(out_path=out, force=True, prompt=prompt)
    assert rc == 0, prompt.asked
    cfg = load_config(out)
    t = cfg.targets["x5000"]
    assert t.type == "remote"
    assert t.channels.mcu is not None
    assert t.channels.mcu.port == "COM5"
    assert t.channels.mcu.baud == 38400


def test_interactive_aborts_when_user_says_no_to_overwrite(tmp_path):
    out = tmp_path / "config.toml"
    out.write_text("# keep me\n", encoding="utf-8")
    prompt = ScriptedPrompt(["n"])
    rc = run_init(out_path=out, force=False, prompt=prompt)
    assert rc == 0
    assert out.read_text(encoding="utf-8") == "# keep me\n"


def test_interactive_zero_targets_still_validates(tmp_path):
    answers = [
        "", "", "info", "stdio",   # server
        "", "", "",                # paths
        "n",                       # add a target? -> no
        "n",                       # configure defaults
    ]
    prompt = ScriptedPrompt(answers)
    out = tmp_path / "config.toml"
    rc = run_init(out_path=out, force=True, prompt=prompt)
    assert rc == 0
    cfg = load_config(out)
    assert cfg.targets == {}
