"""Path / environment helpers shared across helper scripts.

Keeps the helper scripts free of hardcoded user-specific paths
and IPs. Every defaulting decision is overridable via a CLI flag
or an environment variable.

Conventions:

  $MCPD_ENDPOINT       — default <host>:<port> for `validate.py`-style scripts
  $AMIGA_INSTALL_SUPPORT_DIR — where to find diskimage-bootstrap/, SerialShell

Scripts that import `serial_client` / `qmp_client` need the qemu-runner
package, which is a host dependency: run them with host/.venv's Python
(or `uv run`), not a bare system `python`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def repo_root() -> Path:
    """Return the MCP-AmigaOS4 checkout root."""
    return Path(__file__).resolve().parent.parent


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
