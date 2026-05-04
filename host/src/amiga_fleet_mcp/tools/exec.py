"""exec.cmd - run an AmigaDOS command via MCPd."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel

from ..fleet import Fleet


class ExecResult(BaseModel):
    output: str
    exit_code: int = 0
    truncated: bool = False


async def exec_cmd(
    fleet: Fleet,
    target: str,
    command: str,
    args: Sequence[str] | None = None,
    cwd: str | None = None,
    timeout_s: float = 30.0,
) -> ExecResult:
    """Run an AmigaDOS command on `target`.

    `args` are appended to `command` with conservative AmigaDOS quoting
    handled daemon-side. `cwd` (if given) is locked and swapped in for
    the duration of the call. `timeout_s` is the host-side request
    timeout; the daemon also receives `timeout_ms` so it can honour
    advisory limits when the SystemTags-based runner gains kill support.
    """
    t = fleet.mcpd(target)
    params: dict[str, object] = {
        "command": command,
        "timeout_ms": int(timeout_s * 1000),
    }
    if args:
        params["args"] = list(args)
    if cwd:
        params["cwd"] = cwd
    raw = await t.request(
        "exec.cmd",
        params,
        timeout_s=max(timeout_s + 5.0, 30.0),
    )
    return ExecResult.model_validate(raw)
