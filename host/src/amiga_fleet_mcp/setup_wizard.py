"""Interactive `--init` wizard that writes a validated config.toml.

Asks a small set of questions, builds a config dict, validates it
through the same Pydantic schema the server uses at startup, then
writes a hand-rolled TOML file. No third-party TOML writer dep.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .config import Config, default_config_path

# ---------- minimal TOML writer -----------------------------------

_TOML_SAFE_KEY = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)


def _toml_key(name: str) -> str:
    if name and all(c in _TOML_SAFE_KEY for c in name):
        return name
    return _toml_str(name)


def _toml_str(s: str) -> str:
    # basic-string form: quote " backslash, escape control chars
    out = ['"']
    for ch in s:
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, Path):
        return _toml_str(str(v))
    if isinstance(v, str):
        return _toml_str(v)
    raise TypeError(f"unsupported toml value: {v!r}")


def _emit_table(path: list[str], data: dict[str, Any], lines: list[str]) -> None:
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict)}
    tables = {k: v for k, v in data.items() if isinstance(v, dict)}
    if path or scalars:
        if path:
            lines.append(f"[{'.'.join(_toml_key(p) for p in path)}]")
        for k, v in scalars.items():
            lines.append(f"{_toml_key(k)} = {_toml_value(v)}")
        if tables:
            lines.append("")
    for k, v in tables.items():
        _emit_table([*path, k], v, lines)
        lines.append("")


def render_toml(data: dict[str, Any]) -> str:
    lines: list[str] = []
    _emit_table([], data, lines)
    # collapse trailing blank lines, force single trailing newline
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines) + "\n"


# ---------- prompt helpers ----------------------------------------

Prompt = Callable[[str], str]


def _ask(prompt: Prompt, question: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        ans = prompt(f"{question}{suffix}: ").strip()
        if ans:
            return ans
        if default is not None:
            return default
        print("(value required)")


def _ask_bool(prompt: Prompt, question: str, default: bool = False) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        ans = prompt(f"{question} [{suffix}]: ").strip().lower()
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        print("answer y or n")


def _ask_choice(
    prompt: Prompt, question: str, choices: list[str], default: str
) -> str:
    pretty = "/".join(choices)
    while True:
        ans = _ask(prompt, f"{question} ({pretty})", default)
        if ans in choices:
            return ans
        print(f"pick one of: {pretty}")


def _ask_optional(prompt: Prompt, question: str) -> str | None:
    """Blank input → None."""
    ans = prompt(f"{question} [skip]: ").strip()
    return ans or None


# ---------- detection -------------------------------------------

def _detect_existing_path(*candidates: Path) -> Path | None:
    for c in candidates:
        if c.exists():
            return c
    return None


def _projects_root() -> Path:
    return Path.home() / "Projects"


def _suggest(name: str) -> str | None:
    p = _detect_existing_path(_projects_root() / name)
    return str(p) if p else None


# ---------- minimal default config --------------------------------

def _stub_config() -> dict[str, Any]:
    base = Path(os.environ.get("APPDATA", "")) if sys.platform == "win32" \
        else Path.home() / ".local" / "share"
    if sys.platform == "win32" and not str(base):
        base = Path.home() / "AppData" / "Roaming"
    base = base / "amiga-fleet-mcp"
    return {
        "server": {
            "log_level": "info",
            "log_dir": str(base / "logs"),
            "archive_root": str(base / "archive"),
            "mcp_transport": "stdio",
        },
        "paths": {},
        "defaults": {},
        "targets": {},
    }


# ---------- interactive flow --------------------------------------

def _wizard_server(prompt: Prompt, cfg: dict[str, Any]) -> None:
    s = cfg["server"]
    print("\n[server]")
    s["log_dir"] = _ask(prompt, "log_dir", s["log_dir"])
    s["archive_root"] = _ask(prompt, "archive_root", s["archive_root"])
    s["log_level"] = _ask_choice(
        prompt, "log_level", ["debug", "info", "warn", "error"], s["log_level"]
    )
    s["mcp_transport"] = _ask_choice(
        prompt, "mcp_transport",
        ["stdio", "sse", "streamable-http"], s["mcp_transport"],
    )
    if s["mcp_transport"] != "stdio":
        s["mcp_http_addr"] = _ask(prompt, "mcp_http_addr", "127.0.0.1:7180")


def _wizard_paths(prompt: Prompt, cfg: dict[str, Any]) -> None:
    print("\n[paths] — optional helper paths. Leave any blank to skip.")
    print("  qemu_runner       → required by qemu.* + QMP transport")
    print("  amiga_qemu_tests  → required by tests.*")
    print("  qemu_binary       → required by qemu.start")
    p = cfg["paths"]
    qr = _ask_optional(
        prompt, f"qemu_runner path (default: {_suggest('qemu-runner') or 'none'})"
    ) or _suggest("qemu-runner")
    if qr:
        p["qemu_runner"] = qr
    aqt = _ask_optional(
        prompt,
        f"amiga_qemu_tests path (default: {_suggest('AmigaQemuTests') or 'none'})"
    ) or _suggest("AmigaQemuTests")
    if aqt:
        p["amiga_qemu_tests"] = aqt
    qb = _ask_optional(prompt, "qemu_binary path (e.g. /usr/bin/qemu-system-ppc)")
    if qb:
        p["qemu_binary"] = qb


def _wizard_target(prompt: Prompt) -> tuple[str, dict[str, Any]] | None:
    name = _ask(prompt, "target name (e.g. x5000-real, qemu-pegasos2)")
    ttype = _ask_choice(prompt, "type", ["qemu", "remote"], "remote")
    raw_tags = _ask(prompt, "tags (comma-separated)", ttype)
    t: dict[str, Any] = {
        "type": ttype,
        "display_name": _ask(prompt, "display_name", name),
        "tags": [s.strip() for s in raw_tags.split(",") if s.strip()],
        "channels": {},
    }
    if ttype == "qemu":
        t["machine"] = _ask_choice(
            prompt, "machine", ["pegasos2", "amigaone", "sam460ex"], "pegasos2"
        )
        t["qemu_config"] = _ask(prompt, "qemu_config (absolute path to config.json)")
        port_mcpd = _ask(prompt, "channels.mcpd hostfwd port", "4422")
        t["channels"]["mcpd"] = {
            "enabled": True,
            "endpoint": f"127.0.0.1:{port_mcpd}",
        }
        if _ask_bool(prompt, "enable channels.qmp?", True):
            t["channels"]["qmp"] = {
                "enabled": True,
                "endpoint": f"127.0.0.1:{_ask(prompt, 'qmp port', '14422')}",
            }
        if _ask_bool(prompt, "enable channels.gdb?", False):
            t["channels"]["gdb"] = {
                "enabled": True,
                "endpoint": f"127.0.0.1:{_ask(prompt, 'gdb port', '1234')}",
            }
    else:
        ip = _ask(prompt, "target IP")
        t["channels"]["mcpd"] = {
            "enabled": True,
            "endpoint": f"{ip}:{_ask(prompt, 'mcpd port', '4322')}",
        }
        if _ask_bool(
            prompt,
            "FTDI USB-TTL cable wired to internal MCU header (P18 / P15)?",
            False,
        ):
            default_port = "COM5" if sys.platform == "win32" else "/dev/ttyUSB1"
            t["channels"]["mcu"] = {
                "enabled": True,
                "port": _ask(prompt, "MCU serial port", default_port),
                "baud": 38400,
            }
    return name, t


def _wizard_targets(prompt: Prompt, cfg: dict[str, Any]) -> None:
    print("\n[targets.*] — add one or more targets.")
    while _ask_bool(prompt, "add a target?", default=not cfg["targets"]):
        out = _wizard_target(prompt)
        if out is None:
            continue
        name, t = out
        cfg["targets"][name] = t


def _wizard_default_target(prompt: Prompt, cfg: dict[str, Any]) -> None:
    targets = list(cfg["targets"])
    if len(targets) == 1:
        cfg["server"]["default_target"] = targets[0]
        return
    if not targets:
        return
    print(f"\nconfigured targets: {', '.join(targets)}")
    if _ask_bool(prompt, "set [server] default_target?", True):
        cfg["server"]["default_target"] = _ask_choice(
            prompt, "default_target", targets, targets[0]
        )


def _wizard_defaults(prompt: Prompt, cfg: dict[str, Any]) -> None:
    if not _ask_bool(prompt, "\nconfigure [defaults] (per-tool parameter defaults)?", False):
        return
    d = cfg["defaults"]
    for key, hint in [
        ("dest_volume", 'AmigaDOS volume to install into (e.g. "BootTest:")'),
        ("sources_dir", "host directory holding source ISOs / LHAs"),
        ("bootstrap_dir", "diskimage-bootstrap directory"),
        ("machine", 'canonical machine identifier (e.g. "X5000")'),
    ]:
        v = _ask_optional(prompt, f"{key} — {hint}")
        if v:
            d[key] = v


# ---------- entry point -------------------------------------------

def _strip_empty(cfg: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in cfg.items() if not (isinstance(v, dict) and not v)}


def run_init(
    *,
    out_path: Path | None = None,
    force: bool = False,
    non_interactive: bool = False,
    prompt: Prompt | None = None,
) -> int:
    """Run the wizard. Returns process exit code (0 = wrote a config)."""

    dest = out_path or default_config_path()
    print(f"amiga-fleet-mcp --init: writing to {dest}")

    if dest.exists() and not force:
        if non_interactive:
            print(f"refusing to overwrite existing config: {dest} (pass --force)")
            return 65
        if not _ask_bool(prompt or input, f"overwrite existing {dest}?", False):
            print("aborted.")
            return 0

    cfg = _stub_config()

    if not non_interactive:
        ask = prompt or input
        _wizard_server(ask, cfg)
        _wizard_paths(ask, cfg)
        _wizard_targets(ask, cfg)
        _wizard_default_target(ask, cfg)
        _wizard_defaults(ask, cfg)

    cfg = _strip_empty(cfg)

    try:
        Config.model_validate(cfg)
    except ValidationError as e:
        print(f"generated config failed validation:\n{e}")
        return 70

    body = render_toml(cfg)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(body, encoding="utf-8")

    print(f"\nwrote {dest} ({len(body)} bytes).")
    print("\nwire it into your MCP client:")
    print(f"  claude mcp add amiga-fleet -- amiga-fleet-mcp --config {dest}")
    print("\nverify with:")
    print(f"  amiga-fleet-mcp --config {dest} --inspect")
    return 0
