"""MCP prompts: ready-made workflows a client can offer its user.

A prompt is a message template, not code. The client lists them (in
Claude Code they appear as `/mcp__amiga-fleet__<name>` commands) and,
when the user picks one, sends the filled-in text to the model, which
then drives the tools itself. Each prompt names the tools to call, in
order, and the AmigaOS traps worth knowing at each step, so the run
doesn't depend on the model rediscovering them.

Tool names here are the fine-grained ones (`sys_lastalert`, not the
`sys` dispatcher); test_prompts.py checks every one is registered.
"""

from __future__ import annotations

from collections.abc import Callable

from mcp.server.mcpserver import MCPServer


def _target_clause(target: str) -> str:
    if target.strip():
        return f"Use target `{target.strip()}` for every call."
    return ("No target was named: call `fleet_list_targets` first and, if "
            "there is more than one, ask the user which to use.")


def getting_started(target: str = "") -> str:
    return f"""\
Give me a short guided tour of my AmigaOS machine through the amiga-fleet
tools. Assume I am new to MCP and to these tools. {_target_clause(target)}

1. `fleet_target_status`: confirm the machine answers. If it doesn't,
   stop and explain what to check (is MCPd running on the Amiga, is the IP
   address in config.toml right; for a QEMU target, `qemu_start` it).
2. `sys_version` and `sys_hardware`: say which AmigaOS version and board
   this is.
3. `sys_volumes` and `sys_memory`: list the mounted volumes and free
   memory.
4. `sys_tasks`: name a few of the programs running.
5. `wb_screenshot`: show me the screen.

Then suggest three things I could ask you to do next, written as plain
requests (for example "copy this file to RAM: and run it"). Keep it
read-only: don't write files, run programs or reboot anything.
"""


def investigate_crash(target: str = "", what_happened: str = "") -> str:
    context = (f"What the user saw: {what_happened.strip()}"
               if what_happened.strip() else
               "The user gave no details; do a general investigation.")
    return f"""\
Investigate a crash or hang on an AmigaOS 4 machine. {context}
{_target_clause(target)} Collect the evidence first, then explain the
most likely cause and what to try next. Don't reboot or kill anything
without asking.

1. `sys_uptime`: is MCPd still answering? If this fails the daemon or
   the machine is gone; say so and stop. A single failed call is not
   proof: MCPd runs one worker per connection, so retry once.
2. `sys_lastalert`, then `sys_alert_decode` on its code. Note that
   payload[1] is a Task pointer, not the fault address.
3. `sys_debug_ring` (kernel debug output). The ring does not wrap: once
   full it accepts nothing, so before reading silence as "nothing
   happened", call it again a little later and see whether `raw_size`
   grew. If it didn't, the ring is full and a `serial_start` capture is
   the better source. On real hardware, look for XHCI, USB3
   or SBR lines first: a wedged USB3 card stalls the PCIe bus and looks
   like an application hang.
4. `events_wait` with `topics=["debug.exception"]` and a short timeout:
   the IDebug crash hook's report gives the faulting task, the PC and
   the DAR (the real fault address).
5. `sys_tasks` and `exec_list`: what is still running, and which tracked
   jobs did or didn't finish (their exit codes and IoErr).
6. `wb_screenshot` (or `qemu_screenshot` for a QEMU guest): is a Grim
   Reaper requester or an alert on screen? A Grim Reaper window holds
   the crashed program until someone clicks it.
7. If the target has a serial capture (`serial_status`), `serial_read`
   it and search for "Trap type", "Crashed process", "Temporary stack
   trace" and driver recovery lines. Serial output survives a frozen
   machine and a full debug ring. Keep a copy of anything that matters
   before anyone calls `serial_clear`.
8. If a fault address is known, `debug_symbol` turns it into a module,
   function and source line.

The first fault reported is not always the cause: a hang can be blamed
on the oldest piece of work still pending, and the case that failed may
only be the next victim of an earlier one. Say how sure you are.

Report: what crashed, the evidence for it, and the next step. If the
program is the user's own, suggest re-running it under SandboxVM
(`exec_cmd` with `sandbox=true`) so the next crash is contained and
reported.
"""


def deploy_and_run(local_path: str, target: str = "",
                   remote_dir: str = "RAM:", args: str = "") -> str:
    return f"""\
Copy a program from this computer to an AmigaOS machine and run it.
{_target_clause(target)}

- Local file: `{local_path}`
- Destination drawer on the Amiga: `{remote_dir}`
- Arguments: `{args or "(none)"}`

1. `fs_upload` the file into the destination drawer, keeping its file
   name.
2. `fs_stat` it, and if the `e` (execute) protection bit is not set,
   `fs_protect` it to `rwed`. A file copied from another computer often
   arrives without it, and AmigaOS then refuses to run it.
3. `sandbox_probe`: if SandboxVM is available, run the program with
   `exec_cmd` and `sandbox=true`, so a crash is contained and reported
   instead of taking the machine down. Otherwise run it with `exec_cmd`
   as normal.
4. If `exec_cmd` returns `state: "running"`, the program outlived the
   timeout and is still going. Follow it with `exec_status` and
   `exec_output`; never re-run it to "retry".
5. Report the exit code, IoErr (`result2`) and output. If it crashed,
   continue as for a crash investigation: `sys_lastalert`,
   `sys_debug_ring`, and the sandbox trap report if it ran sandboxed.
"""


def check_fleet(tags: str = "") -> str:
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    scope = (f"only the targets tagged {', '.join(tag_list)} "
             f"(pass `tags={tag_list!r}`)" if tag_list else
             "every configured target")
    return f"""\
Check the health of {scope}.

1. `fleet_snapshot`: one probe of every target in parallel.
2. For each target that is down, say what to check: is the machine
   powered and booted, is MCPd running, is the address in config.toml
   right. For a QEMU target, it may simply not be started (`qemu_start`).
3. For each target that is up, `sys_memory` and `sys_lastalert`: flag
   low free memory and any recent alert.

Finish with one table: target, up or down, AmigaOS version, free memory,
and anything that needs attention. Change nothing.
"""


def _cold_boot_steps(n: int) -> str:
    """The cold-boot procedure, numbered from `n`. Shared by every
    prompt that has to load new code: a cold boot is the only way to be
    sure the old copy is gone."""
    return f"""\
{n}. `exec_list`: no job may still be running. Writing to the boot drive
   or powering off under a running job can corrupt it; wait for it or
   ask the user.
{n + 1}. `fs_sync` every volume written to (for example `SYS:`, `LIBS:`).
   Writes can still be only in RAM, and a power cut loses them.
{n + 2}. If the target has a serial channel (`serial_status`): the new boot
   should start a fresh log. `serial_clear` wipes the old one, so save
   anything from it you still need first; `serial_start` if no capture
   is running.
{n + 3}. Restart it the safe way for its kind:
   - Real hardware with a power channel: `power_off` with
     `confirm=true`, wait about 5 seconds, then `power_on` with
     `confirm=true`. Don't use `sys_cold_reboot` on an X5000; warm
     reboots aren't reliable there. While the board is off, call no
     other power tool.
   - QEMU guest: `qemu_stop` (it flushes the guest first), then
     `qemu_start`. Never `qemu_reset`.
   - Anything else: ask the user to power-cycle it.
{n + 4}. Wait for the daemon. With a serial capture, `serial_tail` until a
   `[MCPd] ready` line appears (it carries `version=`); otherwise poll
   `sys_uptime` until it answers. Give it about 15 more seconds to
   settle.
{n + 5}. `fleet_target_status`. The first call after a boot can still fail
   with a dropped connection. If an error reports `may_have_run`, check
   `exec_list` before retrying anything that changes state."""


def safe_reboot(target: str = "") -> str:
    return f"""\
Restart an AmigaOS machine without losing data or wedging it.
{_target_clause(target)} Ask the user before you begin, saying how this
target will be restarted.

{_cold_boot_steps(1)}

Report how long the restart took and the MCPd version it came back with.
"""


def deploy_library(local_path: str, remote_path: str,
                   target: str = "") -> str:
    return f"""\
Replace a library, device or driver on an AmigaOS machine and load the
new copy. {_target_clause(target)}

- New file on this computer: `{local_path}`
- Where it goes on the Amiga: `{remote_path}`

Loading new code takes a cold boot. Don't try to swap it in place with
`Avail FLUSH`: any program that still has the old copy open (a helper
process, a leftover thread) keeps it in memory, and every test after
that silently runs the old code.

1. `fs_stat` the destination. If a file is there, `fs_rename` it to a
   dated backup next to it (`<name>.bak-<YYYYMMDD>`) instead of
   overwriting it, so it can be put back.
2. `fs_upload` the new file with `verify=true`. For a file over 24 MB,
   pass a smaller `chunk_size`, such as 8388608.
{_cold_boot_steps(3)}
9. Confirm the new copy is the one in memory, not just the one on disk:
   `sys_libraries` (or `sys_devices`) should show the new version, or
   the boot log its banner. A file on disk proves nothing until it has
   been loaded.

Environment variables a library reads when it opens take effect only
from the next cold boot, and stay in effect until the one after they
are removed.

Report the old and new versions, and where the backup is.
"""


def run_test_batch(local_script: str, target: str = "",
                   remote_dir: str = "RAM:", results: str = "") -> str:
    fetch = (f"`fs_download` the results (`{results}`) to this computer."
             if results.strip() else
             "`fs_download` any result files the script wrote, if the "
             "user wants them.")
    return f"""\
Run an AmigaDOS test script on an AmigaOS machine as a tracked job and
collect the results. {_target_clause(target)}

- Script on this computer: `{local_script}`
- Drawer on the Amiga to run it from: `{remote_dir}`

1. `exec_list`: start nothing while another long job is still running
   unless the user says so.
2. `fs_upload` the script into the drawer. Scripts that run for long
   are steadier with `FailAt` set and a large `Stack` near the top.
3. `exec_cmd` with `command` set to `Execute <script>` and a short
   `timeout_s`, such as 10. A long batch outlives it and comes back with
   `state: "running"` and a `job_id`. That is expected: never start it a
   second time.
4. Follow it with `exec_status` until it finishes, reading progress
   with `exec_output`. Output a script redirects to a file is buffered,
   so its size from `fs_stat` lags behind. If the target has a serial
   capture, `serial_tail` shows a crash ("Trap type", a Grim Reaper
   report) as soon as it happens.
5. When it ends, report the exit code and `result2`, and pull out the
   pass and fail lines (`exec_output`, or `exec_cmd` with `Search` on
   an output file).
6. {fetch}

Stop a stuck job with `exec_kill` only if the user agrees. A break
signal ends the process but not necessarily threads it started, and
those can crash the machine later.
"""


def ab_test_fix(repro: str, local_path: str, remote_path: str,
                target: str = "") -> str:
    return f"""\
Prove a fix with a before-and-after run on an AmigaOS machine.
{_target_clause(target)}

- Reproduction: `{repro}`
- Fixed file on this computer: `{local_path}`
- Where it goes on the Amiga: `{remote_path}`

1. Before: run the reproduction on the code now installed (`exec_cmd`;
   follow a long run with `exec_status` and `exec_output`). It should
   fail. If it passes, stop and say so: the test doesn't show the bug.
   Note the evidence: exit code, output, alerts, serial lines.
2. Deploy the fix: dated backup of the old file with `fs_rename`,
   `fs_upload` with `verify=true`, then a cold boot:
{_cold_boot_steps(3)}
9. Check the fixed version is loaded (`sys_libraries`, `sys_devices`,
   or its banner in the boot log), not just present on disk.
10. After: run the same reproduction the same way. It should pass.
11. Check nothing new went wrong: `sys_lastalert`, `sys_debug_ring`,
    and the serial log if there is one.

Report before against after in one table. If after still fails, say
which evidence changed, if any.
"""


PROMPTS: dict[str, tuple[Callable[..., str], str, str]] = {
    "getting_started": (getting_started, "Getting started",
                        "A guided, read-only tour of one Amiga: version, "
                        "volumes, memory, tasks and a screenshot."),
    "investigate_crash": (investigate_crash, "Investigate a crash",
                          "Collect alert, debug-ring, crash-hook and "
                          "screen evidence after a crash or hang, then "
                          "explain the likely cause."),
    "deploy_and_run": (deploy_and_run, "Deploy and run a program",
                       "Upload a local program, fix its protection bits "
                       "and run it, sandboxed when SandboxVM is there."),
    "check_fleet": (check_fleet, "Check the fleet",
                    "Probe every target and summarise which are up, "
                    "their memory, and any recent alerts."),
    "safe_reboot": (safe_reboot, "Restart safely",
                    "Cold-boot a target without losing writes: check "
                    "jobs, flush, fresh serial log, power-cycle or QEMU "
                    "restart, wait for MCPd."),
    "deploy_library": (deploy_library, "Deploy a library or driver",
                       "Back up and replace a library, device or driver, "
                       "cold-boot, and confirm the new version is the one "
                       "loaded."),
    "run_test_batch": (run_test_batch, "Run a test batch",
                       "Upload an AmigaDOS script and run it as a tracked "
                       "job, follow it without re-running it, and collect "
                       "the results."),
    "ab_test_fix": (ab_test_fix, "Before-and-after test of a fix",
                    "Run a reproduction, deploy the fix with a cold "
                    "boot, run it again, and compare the two."),
}


def register_prompts(mcp: MCPServer) -> None:
    for name, (fn, title, description) in PROMPTS.items():
        mcp.prompt(name=name, title=title, description=description)(fn)
