"""Build a QEMU command line from a kyvos-style config.json + our
target config (which knows the SerialShell / QMP / MCPd port-forwards
to inject).

Mirrors qemu-runner/qemu_manager.py:build_qemu_cmdline + _split_arg_string
but lives in this codebase so phase-2 tests don't depend on the
neighbour project being importable. Kept deliberately simple — no
auto-restart loop, no idle watchdog.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import TargetConfig
from ..errors import InternalError


def _split_arg_string(s: str) -> list[str]:
    """Split a QEMU arg string respecting quoted paths."""
    result: list[str] = []
    current: list[str] = []
    in_quote = False
    quote_char: str | None = None
    for ch in s:
        if ch in ('"', "'") and not in_quote:
            in_quote = True
            quote_char = ch
        elif ch == quote_char and in_quote:
            in_quote = False
            quote_char = None
        elif ch == " " and not in_quote:
            if current:
                result.append("".join(current))
                current = []
        else:
            current.append(ch)
    if current:
        result.append("".join(current))
    return result


# Drop kyvos-specific args that we'll override (we always replace
# the pidfile, and we add our own qmp). User-provided values for
# these in the kyvos config are ignored.
_DROPPED_ARG_KEYS = {"pidfile"}

# In-guest listen port for MCPd. The host port (from config.toml)
# varies per target so multiple QEMU instances can coexist; the guest
# port is fixed by the daemon binary.
GUEST_PORT_MCPD = 4322

# In-guest UDP port for the LAN discovery responder. Same port as
# the daemon binds; the host's discover() targets 127.0.0.1:4323 so
# the QEMU hostfwd path picks it up.
GUEST_PORT_DISCOVERY = 4323


def _inject_hostfwd(netdev_arg: str, fwds: list[str]) -> str:
    """Inject `,hostfwd=tcp::H-:G` clauses into a `-netdev user,...` arg.

    Idempotent on duplicate hostfwd entries (de-dup by exact string).
    """
    if not fwds:
        return netdev_arg
    parts = netdev_arg.split(",")
    existing = {p for p in parts if p.startswith("hostfwd=")}
    extra = [f for f in fwds if f not in existing]
    if not extra:
        return netdev_arg
    return ",".join([*parts, *extra])


def build_cmdline(
    qemu_binary: Path,
    kyvos_config_path: Path,
    target: TargetConfig,
    *,
    headless: bool | None = None,
) -> tuple[list[str], dict[str, int | None]]:
    """Build a fully-formed QEMU command line.

    Returns the argv plus a `ports` dict listing the ports we injected
    (`serialshell`, `mcpd`, `qmp`) so callers can probe them.
    """
    if not kyvos_config_path.exists():
        raise InternalError(f"kyvos config not found: {kyvos_config_path}")
    with kyvos_config_path.open("r", encoding="utf-8") as fh:
        kyvos = json.load(fh)
    args: dict[str, str] = kyvos.get("args", {})

    # Compute hostfwd clauses + qmp arg from our own target channel
    # configuration.
    fwds: list[str] = []
    ports: dict[str, int | None] = {
        "mcpd": None,
        "qmp": None,
        "gdb": None,
        "discovery": None,
    }
    if target.channels.mcpd is not None and target.channels.mcpd.enabled:
        mcpd_host = target.channels.mcpd.port
        fwds.append(f"hostfwd=tcp::{mcpd_host}-:{GUEST_PORT_MCPD}")
        ports["mcpd"] = mcpd_host
        # Also forward the UDP discovery port so the host's
        # `fleet.discover` probe (which sends to 127.0.0.1:4323)
        # reaches the guest's MCPd discovery responder.
        fwds.append(
            f"hostfwd=udp::{GUEST_PORT_DISCOVERY}-:{GUEST_PORT_DISCOVERY}"
        )
        ports["discovery"] = GUEST_PORT_DISCOVERY

    qmp_arg: str | None = None
    if target.channels.qmp is not None and target.channels.qmp.enabled:
        qch = target.channels.qmp
        qmp_arg = f"-qmp tcp:{qch.host}:{qch.port},server=on,wait=off"
        ports["qmp"] = qch.port

    gdb_arg: str | None = None
    if target.channels.gdb is not None and target.channels.gdb.enabled:
        gch = target.channels.gdb
        gdb_arg = f"-gdb tcp::{gch.port}"
        ports["gdb"] = gch.port

    use_headless = target.headless if headless is None else headless

    cmdline: list[str] = [str(qemu_binary)]
    for key, value in args.items():
        if not value or not value.strip():
            continue
        if key in _DROPPED_ARG_KEYS:
            continue
        if key == "network":
            value = _inject_hostfwd(value, fwds)
        if key == "display" and use_headless:
            value = "-display none"
        cmdline.extend(_split_arg_string(value))

    if qmp_arg is not None:
        cmdline.extend(_split_arg_string(qmp_arg))
    if gdb_arg is not None:
        cmdline.extend(_split_arg_string(gdb_arg))

    return cmdline, ports


# ---------- exposed for tests ---------------------------------------

def _public_inject_hostfwd(arg: str, fwds: list[str]) -> str:
    """Test-only helper export."""
    return _inject_hostfwd(arg, fwds)
