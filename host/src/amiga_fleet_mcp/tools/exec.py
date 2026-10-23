"""exec.* - run AmigaDOS commands via MCPd, as tracked jobs.

From daemon 2.0 every exec.cmd is a job with an id: a caller whose
wait ran out can still ask for its status and output, or stop it.
Older daemons ignore the job parameters and return only output and
exit_code, which the models below accept.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..errors import InvalidParams, NotCapable
from ..fleet import Fleet


class SandboxJobInfo(BaseModel):
    """What `exec.cmd sandbox=True` adds to its result."""

    name: str
    """SandboxVM ``-n`` name; also names the ``T:sandboxvm-<name>.*``
    capture files."""
    #: How the guest ended: exit (returned, its own rc), trap, aborted
    #: (exec_kill force / CTRL-C) or timeout (sandbox_time_limit_s).
    end_reason: str | None = None
    trap_kind: str | None = None
    trap_fingerprint: str | None = None
    #: SandboxVM's trap report from the kernel debug ring (trap type,
    #: ip, dar, dsisr, registers), when this job's guest trapped.
    trap_lines: list[str] = Field(default_factory=list)
    #: False when the debug ring could not be read for a trap (see
    #: sandbox.last_trap ring_usable): "no trap" is then unknown.
    ring_usable: bool | None = None
    stdout: str = ""
    stderr: str = ""
    capture_paths: list[str] = Field(default_factory=list)
    """The guest's capture files. Read once the job has exited, then
    deleted; still listed (and not yet written) while it runs."""


class ExecResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    output: str
    #: None while the job is still running (return_running=True).
    exit_code: int | None = 0
    truncated: bool = False
    #: Job fields; absent from daemons older than 2.0.
    job_id: int | None = None
    state: str = "exited"
    result2: int | None = None
    pid: int | None = None
    priority: int | None = None
    elapsed_ms: int | None = None
    #: Set for `sandbox=True`: the guest's own output and any trap.
    sandbox: SandboxJobInfo | None = None


class JobStatus(BaseModel):
    model_config = ConfigDict(extra="allow")

    job_id: int
    state: str
    exit_code: int | None = None
    result2: int | None = None
    pid: int | None = None
    priority: int | None = None
    elapsed_ms: int | None = None
    command: str | None = None
    output_available: bool | None = None
    mem_free_before: int | None = None
    mem_free_after: int | None = None
    signals_sent: list[str] = []


class JobOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    job_id: int
    state: str
    exit_code: int | None = None
    output: str
    offset: int
    bytes: int
    total_bytes: int
    eof: bool


class KillResult(BaseModel):
    model_config = ConfigDict(extra="allow")

    job_id: int
    state: str
    exit_code: int | None = None
    signal: str
    sent: bool
    exited: bool
    forced: bool = False


class JobList(BaseModel):
    model_config = ConfigDict(extra="allow")

    jobs: list[dict[str, Any]]
    worker_priority: int | None = None
    default_priority: int | None = None
    max_priority: int | None = None


def job_priority(fleet: Fleet, priority: int | None) -> int | None:
    """The priority to ask for: the call's, else `[defaults]
    exec_priority`, else None (the daemon's own default, -2)."""
    if priority is not None:
        return priority
    return fleet.config.defaults.exec_priority


async def exec_cmd(
    fleet: Fleet,
    target: str,
    command: str,
    args: Sequence[str] | None = None,
    cwd: str | None = None,
    timeout_s: float = 30.0,
    priority: int | None = None,
    return_running: bool = False,
    sandbox: bool = False,
    sandbox_extmem_mb: int | None = None,
    sandbox_window_mb: int | None = None,
    sandbox_deny_libs: Sequence[str] | None = None,
    sandbox_time_limit_s: int | None = None,
) -> ExecResult:
    """Run an AmigaDOS command on `target` and wait up to `timeout_s`.

    `args` are appended to `command` with conservative AmigaDOS quoting
    handled daemon-side. `cwd` (if given) is locked and swapped in for
    the start. `priority` is the job's task priority (default from
    `[defaults] exec_priority`, else the daemon's -2; capped below the
    daemon's workers).

    If the command is still running at the deadline: with
    `return_running` the result has `state="running"`, the `job_id`
    and any output so far; without it a TargetError is raised whose
    data carries the `job_id`. Either way the command keeps running --
    see exec_status / exec_kill.

    `sandbox=True` runs `command` -- then the path of a program, not a
    shell line -- under SandboxVM, so a crash in it is contained. The
    job is the same kind (id, status, kill), the result gains a
    `sandbox` block with the guest's own output and any trap, and
    `exec_kill(force=True)` becomes possible where SandboxVM supports
    aborting a guest. Needs an ExtMem machine (not Pegasos II).
    """
    t = fleet.mcpd(target)
    sandbox_name: str | None = None
    if sandbox:
        from . import sandbox as sandbox_tool  # sandbox imports this module
        command, sandbox_name = await sandbox_tool.sandbox_job_command(
            fleet, target, guest=command, args=args,
            extmem_mb=sandbox_extmem_mb, window_mb=sandbox_window_mb,
            deny_libs=sandbox_deny_libs, time_limit_s=sandbox_time_limit_s)
        args = None  # passed to the guest inside the SandboxVM command
    params: dict[str, object] = {
        "command": command,
        "timeout_ms": int(timeout_s * 1000),
    }
    if sandbox:
        params["sandboxed"] = True
    if args:
        params["args"] = list(args)
    if cwd:
        params["cwd"] = cwd
    pri = job_priority(fleet, priority)
    if pri is not None:
        params["priority"] = pri
    if return_running:
        params["return_running"] = True
    raw = await t.request(
        "exec.cmd",
        params,
        timeout_s=max(timeout_s + 5.0, 30.0),
    )
    result = ExecResult.model_validate(raw)
    if sandbox_name is not None:
        from . import sandbox as sandbox_tool
        result.sandbox = await sandbox_tool.sandbox_job_result(
            fleet, target, sandbox_name,
            result.exit_code if result.state == "exited" else None)
    return result


async def exec_status(fleet: Fleet, target: str, job_id: int) -> JobStatus:
    """State of a job started by exec.cmd, without its output."""
    raw = await fleet.mcpd(target).request("exec.status", {"job_id": job_id})
    return JobStatus.model_validate(raw)


async def exec_output(
    fleet: Fleet,
    target: str,
    job_id: int,
    offset: int = 0,
    max_bytes: int = 65536,
) -> JobOutput:
    """A slice of a job's output. Works while the job runs, but AmigaDOS
    often flushes a command's output only when it ends."""
    raw = await fleet.mcpd(target).request("exec.output", {
        "job_id": job_id, "offset": offset, "max_bytes": max_bytes,
    })
    return JobOutput.model_validate(raw)


async def exec_kill(
    fleet: Fleet,
    target: str,
    job_id: int,
    signal: str = "c",
    wait_s: float = 2.0,
    force: bool = False,
) -> KillResult:
    """Send a running job a break signal (c, d, e or f) and wait up to
    `wait_s` for it to exit. A program that ignores breaks keeps
    running and comes back with `exited=False`; nothing is forced.

    `force=True` is for jobs started with `sandbox=True` only: it asks
    SandboxVM to abort the guest, which frees everything the guest
    opened. It needs a SandboxVM that supports that (sandbox.probe
    `abort_supported`; requested in derfsss/SandboxVM#1)."""
    params: dict[str, object] = {
        "job_id": job_id, "signal": signal, "wait_ms": int(wait_s * 1000),
    }
    if force:
        from . import sandbox as sandbox_tool
        probe = await sandbox_tool.sandbox_probe(fleet, target)
        if not probe.available:
            raise InvalidParams(
                "exec.kill force=True needs SandboxVM on the target",
                data={"code": probe.code, "target": target})
        if not probe.abort_supported:
            raise NotCapable(
                "exec.kill force=True needs a SandboxVM that aborts a guest "
                "on request; the installed one "
                f"({probe.sandboxvm_version or 'unknown version'}) does not. "
                "See derfsss/SandboxVM#1. A break signal (force=False) "
                "still works.",
                data={"target": target,
                      "sandboxvm_version": probe.sandboxvm_version})
        params["force"] = True
    raw = await fleet.mcpd(target).request(
        "exec.kill", params, timeout_s=max(wait_s + 10.0, 30.0),
    )
    return KillResult.model_validate(raw)


async def exec_list(fleet: Fleet, target: str) -> JobList:
    """Every job record the daemon holds, newest first, with its
    worker and job priority limits."""
    raw = await fleet.mcpd(target).request("exec.list")
    return JobList.model_validate(raw)
