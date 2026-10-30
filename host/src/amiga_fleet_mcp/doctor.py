"""`amiga-fleet-mcp --doctor`: check a setup end to end, with a fix
for every problem.

`--health-check` answers "do the targets answer?". The doctor asks the
questions a first-time user gets stuck on before and after that: was a
config found, and which one; does each target answer, and does its
MCPd match this host's version; is the server registered with an MCP
client at all, and with what command. Each check returns a status and,
when it isn't ok, the fix in plain words.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import __version__
from .config import Config, default_config_path, load_config
from .errors import JsonRpcError, NotCapable
from .fleet import Fleet

Status = Literal["ok", "warn", "fail"]


@dataclass
class Check:
    status: Status
    what: str
    fix: str | None = None


# ---------- helpers ------------------------------------------------


def _major_minor(version: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(p) for p in version.split(".")[:2])
    except ValueError:
        return None


def launch_command() -> list[str]:
    """The command an MCP client should run to start this install.

    An editable install from a checkout (the uv quick start) runs from
    `<checkout>/host`; anything else is a console script on the
    interpreter's own path, which a client must be given in full
    because it never activates a venv.
    """
    host_dir = Path(__file__).resolve().parents[2]
    if (host_dir / "pyproject.toml").is_file() and (host_dir / "src").is_dir():
        return ["uv", "--directory", host_dir.as_posix(), "run",
                "amiga-fleet-mcp"]
    scripts = Path(sys.executable).parent
    for name in ("amiga-fleet-mcp.exe", "amiga-fleet-mcp"):
        if (scripts / name).is_file():
            return [(scripts / name).as_posix()]
    return [shutil.which("amiga-fleet-mcp") or "amiga-fleet-mcp"]


def _claude_desktop_config() -> Path | None:
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA")
        return Path(base) / "Claude" / "claude_desktop_config.json" if base else None
    if system == "Darwin":
        return (Path.home() / "Library" / "Application Support" / "Claude"
                / "claude_desktop_config.json")
    return None


# ---------- checks -------------------------------------------------


def check_config(path: Path | None) -> tuple[Check, Config | None]:
    if path is not None:
        p, source = path, "--config"
    elif os.environ.get("AMIGA_FLEET_CONFIG"):
        p, source = default_config_path(), "$AMIGA_FLEET_CONFIG"
    else:
        p, source = default_config_path(), "platform default"
    if not p.exists():
        return Check(
            "fail", f"config: none at {p} ({source})",
            "Run `amiga-fleet-mcp --init` to write one there, or pass "
            "`--config <path>` if it lives elsewhere.",
        ), None
    try:
        cfg = load_config(p)
    except (JsonRpcError, ValueError) as e:
        first = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        return Check(
            "fail", f"config: {p} does not load: {first}",
            "Fix the line it names, or write a fresh one with "
            "`amiga-fleet-mcp --init --force`.",
        ), None
    return Check("ok", f"config: {p} ({source})"), cfg


def check_targets(cfg: Config) -> list[Check]:
    names = sorted(cfg.targets)
    if not names:
        return [Check(
            "fail", "targets: none configured",
            "Add your Amigas with `amiga-fleet-mcp --init`.",
        )]
    out = [Check("ok", f"targets: {', '.join(names)}")]
    default = cfg.server.default_target
    if default and default not in cfg.targets:
        out.append(Check(
            "fail", f"default_target {default!r} is not a configured target",
            f"Set `[server] default_target` to one of: {', '.join(names)}.",
        ))
    elif not default and len(names) > 1:
        out.append(Check(
            "warn", "no default_target: every tool call must name a target",
            "Set `[server] default_target = \"<name>\"` in config.toml so "
            "the AI can leave it out.",
        ))
    return out


async def _probe(fleet: Fleet, name: str) -> Check:
    cfg = fleet.target_config(name)
    mc = cfg.channels.mcpd
    if mc is None or not mc.enabled:
        return Check("warn", f"{name}: no MCPd channel configured",
                     "Add a `[targets.<name>.channels.mcpd]` endpoint, or "
                     "ignore this if the target is only driven over QMP / "
                     "serial.")
    where = f"{name} ({mc.endpoint})"
    try:
        caps = await asyncio.wait_for(
            fleet.mcpd(name).request("proto.capabilities"), 5.0)
    except (TimeoutError, OSError, JsonRpcError, NotCapable) as e:
        if isinstance(e, TimeoutError):
            reason = "no answer within 5 s"
        else:
            # The transport's own message repeats the endpoint and
            # appends the data dict; the socket error alone says it.
            data = getattr(e, "data", None)
            reason = str(data.get("error") if isinstance(data, dict)
                         and data.get("error") else e)
        if cfg.type == "qemu":
            # Guests are started on demand, so a stopped one is normal.
            return Check(
                "warn", f"{where}: not answering: {reason}",
                "Fine if the guest just isn't started: ask the AI to "
                "`qemu_start` it when you need it. If it is running, check "
                "that MCPd started inside it.")
        return Check(
            "fail", f"{where}: unreachable: {reason}",
            "Check that the Amiga is on and booted, that MCPd is running "
            "on it (it starts from SYS:System/MCPd/ at boot once "
            "installed), and that the address in config.toml is right.")
    server = str(caps.get("server", "?"))
    remote = server.split("/", 1)[1] if "/" in server else server
    theirs, ours = _major_minor(remote), _major_minor(__version__)
    if theirs is None or ours is None or theirs == ours:
        return Check("ok", f"{where}: MCPd {remote}")
    if theirs < ours:
        fix = (f"Install MCPd {__version__} from the latest release "
               "(MCPd-<version>.lha) on that Amiga.")
    else:
        fix = ("Update this host: `git pull`, then `uv sync` in host/ "
               "(or `pip install -e .`).")
    return Check("warn", f"{where}: MCPd {remote}, host {__version__}", fix)


async def check_reachability(cfg: Config) -> list[Check]:
    fleet = Fleet(cfg)
    try:
        return list(await asyncio.gather(
            *(_probe(fleet, n) for n in sorted(cfg.targets))))
    finally:
        await fleet.close_all()


Runner = Callable[[list[str]], str | None]


def _run(cmd: list[str]) -> str | None:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout + r.stderr


def check_clients(
    *,
    which: Callable[[str], str | None] = shutil.which,
    run: Runner = _run,
    desktop_config: Path | None = None,
) -> list[Check]:
    cmd = " ".join(launch_command())
    out: list[Check] = []
    found_client = False

    claude = which("claude")
    if claude:
        found_client = True
        listing = run([claude, "mcp", "list"])
        if listing is None:
            out.append(Check("warn", "Claude Code: `claude mcp list` failed or "
                             "timed out, so registration is unknown"))
        elif "amiga-fleet-mcp" in listing:
            out.append(Check("ok", "Claude Code: server registered"))
        else:
            out.append(Check(
                "warn", "Claude Code: server not registered here",
                f"Run: claude mcp add amiga-fleet -- {cmd}  (add "
                "`--scope user` after `add` to use it in every project).",
            ))

    desktop = desktop_config if desktop_config is not None else _claude_desktop_config()
    if desktop is not None and desktop.parent.is_dir():
        found_client = True
        try:
            data: dict[str, Any] = json.loads(desktop.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        servers = data.get("mcpServers") or {}
        registered = any(
            "amiga-fleet-mcp" in json.dumps(entry) for entry in servers.values())
        if registered:
            out.append(Check("ok", f"Claude Desktop: server registered in {desktop}"))
        else:
            argv = launch_command()
            entry = json.dumps({"command": argv[0], "args": argv[1:]})
            out.append(Check(
                "warn", "Claude Desktop: server not registered",
                f"Add to \"mcpServers\" in {desktop}: \"amiga-fleet\": "
                f"{entry}, then restart Claude Desktop.",
            ))

    if not found_client:
        out.append(Check(
            "warn", "no MCP client found (looked for Claude Code and Claude Desktop)",
            f"Give your MCP client this command to start the server: {cmd}. "
            "INSTALL.md, 'Wiring the server into an MCP client', has the "
            "steps for each client.",
        ))
    return out


# ---------- entry point --------------------------------------------


def _print(check: Check) -> None:
    label = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL"}[check.status]
    print(f"  {label}  {check.what}")
    if check.fix:
        print(f"        fix: {check.fix}")


def run_doctor(config_path: Path | None) -> int:
    """Run every check and print it. Exit code: the number of failures
    (warnings don't count)."""
    print(f"amiga-fleet-mcp {__version__} doctor "
          f"(Python {platform.python_version()})")
    checks: list[Check] = []

    def emit(c: Check) -> None:
        checks.append(c)
        _print(c)

    cfg_check, cfg = check_config(config_path)
    emit(cfg_check)
    if cfg is not None:
        for c in check_targets(cfg):
            emit(c)
        if cfg.targets:
            print("  ...   probing targets")
            for c in asyncio.run(check_reachability(cfg)):
                emit(c)
    print("  ...   looking for MCP clients")
    for c in check_clients():
        emit(c)

    fails = sum(c.status == "fail" for c in checks)
    warns = sum(c.status == "warn" for c in checks)
    if not fails and not warns:
        print("All checks passed.")
    else:
        print(f"{fails} failure(s), {warns} warning(s).")
    return fails
