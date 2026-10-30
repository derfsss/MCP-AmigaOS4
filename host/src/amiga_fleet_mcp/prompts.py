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
7. If a fault address is known, `debug_symbol` turns it into a module,
   function and source line.

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
}


def register_prompts(mcp: MCPServer) -> None:
    for name, (fn, title, description) in PROMPTS.items():
        mcp.prompt(name=name, title=title, description=description)(fn)
