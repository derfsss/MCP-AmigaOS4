"""Tiny AmigaOS-side fs helpers used by the installer.

Two pitfalls when staging install dirs from MCP:
1. MCPd's `fs.makedir` rejects paths with a trailing slash.
2. MCPd's `fs.makedir` doesn't create intermediate parents — it
   maps to one IDOS->CreateDir call. Native AmigaDOS `MakeDir <path>
   ALL` does create parents, so we fall through to `exec.cmd
   MakeDir <path> ALL` for the recursive case.

`amiga_makedir` normalises the path, retries via exec.cmd MakeDir ALL
on failure, and treats already-existing dirs as success.
"""

from __future__ import annotations

from typing import Any


async def amiga_makedir(mcpd: Any, path: str) -> None:
    """Idempotent recursive mkdir on the target Amiga.

    - Strips trailing slash (fs.makedir rejects it).
    - Tries fs.makedir first (cheap, single round-trip).
    - On failure, falls back to `MakeDir <path> ALL QUIET` via
      exec.cmd which creates intermediate parents.
    - Treats already-existing dirs as success.
    """
    p = path.rstrip("/")
    # `BootTest:` itself isn't something to mkdir.
    if p.endswith(":") or not p:
        return

    # Fast path: already exists?
    try:
        await mcpd.request("fs.stat", {"path": p})
        return
    except Exception:
        pass

    # Try fs.makedir (single-level only — fails if parent missing)
    try:
        await mcpd.request("fs.makedir", {"path": p})
        return
    except Exception:
        pass

    # Fall back to AmigaDOS MakeDir with ALL flag (creates parents).
    cmd = f'MakeDir "{p}" ALL'
    raw = await mcpd.request("exec.cmd", {
        "command": cmd, "timeout_ms": 30000,
    }, timeout_s=35.0)
    # Re-stat to confirm; if MakeDir failed, raise.
    try:
        await mcpd.request("fs.stat", {"path": p})
    except Exception as e:
        rc = (raw or {}).get("exit_code", "?")
        out = (raw or {}).get("output", "")
        raise RuntimeError(
            f"amiga_makedir failed for {p!r}: "
            f"MakeDir exit_code={rc} output={out!r}"
        ) from e
