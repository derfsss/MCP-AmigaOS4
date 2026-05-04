"""Path / environment helpers shared across helper scripts.

Keeps the helper scripts free of hardcoded user-specific paths
and IPs. Every defaulting decision is overridable via a CLI flag
or an environment variable.

Conventions:

  $QEMU_RUNNER_DIR     — where to find https://github.com/derfsss/qemu-runner
  $MCPD_ENDPOINT       — default <host>:<port> for `validate.py`-style scripts
  $AMIGA_INSTALL_SUPPORT_DIR — where to find diskimage-bootstrap/, SerialShell
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def repo_root() -> Path:
    """Return the MCP-AmigaOS4 checkout root."""
    return Path(__file__).resolve().parent.parent


def find_qemu_runner() -> Path:
    """Resolve qemu-runner.

    Lookup order:
      1. $QEMU_RUNNER_DIR if set and a directory.
      2. ../qemu-runner relative to this repo (sibling checkout).
      3. ~/Projects/qemu-runner.
      4. ~/qemu-runner.

    Returns the first hit, or raises FileNotFoundError with a
    clear message naming the env var to set.
    """
    env = os.environ.get("QEMU_RUNNER_DIR")
    if env:
        p = Path(env).expanduser().resolve()
        if p.is_dir():
            return p
    candidates = [
        repo_root().parent / "qemu-runner",
        Path.home() / "Projects" / "qemu-runner",
        Path.home() / "qemu-runner",
    ]
    for c in candidates:
        if c.is_dir():
            return c
    raise FileNotFoundError(
        "qemu-runner checkout not found. Clone "
        "https://github.com/derfsss/qemu-runner and either:\n"
        "  - place it next to MCP-AmigaOS4/ as a sibling checkout, or\n"
        "  - set $QEMU_RUNNER_DIR to its path.\n"
        f"Searched: {[str(c) for c in candidates]}"
    )


def default_mcpd_endpoint(fallback: str = "127.0.0.1:4322") -> str:
    """Default MCPd endpoint for argparse `--endpoint` flags.

    Reads $MCPD_ENDPOINT first; falls back to the supplied value
    (typically "127.0.0.1:4322" for QEMU-shaped scripts; explicit
    real-hardware scripts should require the user to pass an IP).
    """
    return os.environ.get("MCPD_ENDPOINT", fallback)


def host_python() -> str:
    """The host-package Python interpreter to spawn the MCP server with.

    Prefer `sys.executable` (the interpreter currently running this
    script — typically the right one if the user invoked the script
    via `uv run` or after activating the venv). Fall back to a
    plain `python` on PATH.
    """
    if sys.executable:
        return sys.executable
    return "python"
