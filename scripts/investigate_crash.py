"""Investigate the most recent system fault on a target.

What MCP gives us for crash forensics:

  - sys.lastalert            most-recent alert code + 4-word payload,
                              with subsystem / general / specific
                              decomposition.
  - wb.screens / wb.windows  identify Grim Reaper / orphan screens.
  - sys.tasks                find a zombie task suspended pending
                              the user dismissing the GrimReaper.
  - debug.task_snapshot      register dump (PC, SP, LR, MSR, DAR,
                              DSISR, traptype, GPRs) + frame-chain
                              backtrace under Forbid.
  - debug.stacktrace         IDebug->StackTrace + ObtainDebugSymbol
                              for symbolicated frames.
  - debug.symbol             resolve any address to module / func /
                              source / line.
  - sys.executable.symbols   read the ELF symbol table from the
                              crashed binary on disk.
  - exec.cmd "DumpDebugBuffer"
                              kernel debug ring buffer; often
                              includes pre-fault diagnostic messages.
  - sys.libraries / devices  see what the program had opened at
                              the time of the fault.
  - sys.memory               quantify what the crash leaked.

Usage:
    python scripts/investigate_crash.py [--endpoint host:port]
                                        [--binary local-elf-path]
                                        [--task-name SUBSTR]

If --binary is given, it must be a local copy of the crashed
program's ELF; we'll redeploy it briefly to read its symbol table
and try to map the faulting PC to a function name.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import sys
from contextlib import suppress
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOST_SRC = HERE.parent / "host" / "src"
sys.path.insert(0, str(HOST_SRC))

from amiga_fleet_mcp.config import (  # noqa: E402
    Config, McpdChannel, PathsConfig, ServerConfig,
    TargetChannels, TargetConfig,
)
from amiga_fleet_mcp.fleet import Fleet  # noqa: E402
from amiga_fleet_mcp.tools import debug as debug_tool  # noqa: E402
from amiga_fleet_mcp.tools import exec as exec_tool  # noqa: E402
from amiga_fleet_mcp.tools import fs as fs_tool  # noqa: E402
from amiga_fleet_mcp.tools import sys as sys_tool  # noqa: E402
from amiga_fleet_mcp.tools import wb as wb_tool  # noqa: E402


def make_fleet(endpoint: str) -> Fleet:
    return Fleet(Config(
        server=ServerConfig(
            archive_root=HERE.parent / "tmp" / "investigate-archive",
        ),
        paths=PathsConfig(),
        targets={
            "target": TargetConfig(
                type="remote", display_name="target",
                channels=TargetChannels(
                    mcpd=McpdChannel(endpoint=endpoint),
                ),
            ),
        },
    ))


def banner(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))


# --------------------------------------------------------------------
# Investigation phases
# --------------------------------------------------------------------

async def phase_alert(t) -> dict:
    """Decode the most recent system alert."""
    banner("1. Most recent system alert")
    la = await t.request("sys.lastalert")
    code = la["alert_code"]
    decoded = la.get("decoded", {})
    print(f"  alert_code  = 0x{code:08x}")
    print(f"  dead_end    = {decoded.get('dead_end')}")
    print(f"  subsystem   = {decoded.get('subsystem')!r}")
    print(f"  general     = {decoded.get('general')!r}")
    print(f"  specific    = 0x{decoded.get('specific', 0):04x}")
    print(f"  name        = {decoded.get('name')!r}")
    print()
    print("  Raw payload (LastAlert[0..3]):")
    for i, v in enumerate(la.get("values", [])):
        annot = ""
        if i == 1 and (code & 0x80000000):
            # For 68k-style CPU alerts the payload[1] is typically
            # the faulting effective address.
            annot = "  <- likely faulting address"
        print(f"    [{i}] = 0x{v:08x}  ({v}){annot}")
    return la


async def phase_screens(fleet: Fleet) -> None:
    """Look for Grim Reaper or stranded screens."""
    banner("2. Public screens and active windows")
    screens = await wb_tool.wb_screens(fleet, "target")
    print(f"  {len(screens)} screen(s):")
    for s in screens:
        title = s.title or "<no title>"
        print(f"    {title!r}  {s.width}x{s.height}  windows={s.window_count}")
    fm = await wb_tool.wb_frontmost(fleet, "target")
    if fm.frontmost_screen:
        print(f"  frontmost: {fm.frontmost_screen.title!r}")
    if fm.active_window:
        w = fm.active_window
        print(f"  active window: {w.title!r}  "
              f"{w.width}x{w.height} @ ({w.left},{w.top})")


async def phase_tasks(fleet: Fleet, task_substr: str | None) -> str | None:
    """Find a possibly-suspended zombie task pending Reaper."""
    banner("3. Tasks of interest")
    tasks = await sys_tool.sys_tasks(fleet, "target")
    all_tasks = tasks.ready + tasks.waiting

    suspect = []
    for t in all_tasks:
        n = t.name.lower()
        if any(k in n for k in [
            "reaper", "doom", "dark", "midnight", "game", "sdl",
            "grim",
        ]):
            suspect.append(t)
        if task_substr and task_substr.lower() in n:
            suspect.append(t)
    # Dedup
    seen: set[str] = set()
    suspect = [t for t in suspect
               if not (t.name in seen or seen.add(t.name))]

    if not suspect:
        print("  (no candidate tasks found by heuristic)")
        return None

    print(f"  {len(suspect)} candidate task(s):")
    for t in suspect:
        print(f"    name={t.name!r}  type={t.type}  "
              f"pri={t.priority:>4}  state={t.state}")

    # Prefer the lowest-priority "victim" first - Reaper itself runs
    # at decent priority; the crashed program's task is often around
    # the default 0.
    for t in suspect:
        if "reaper" not in t.name.lower():
            print(f"\n  -> selecting {t.name!r} for register snapshot")
            return t.name
    print(f"\n  -> selecting {suspect[0].name!r} for register snapshot")
    return suspect[0].name


async def phase_snapshot(fleet: Fleet, task_name: str) -> dict:
    """Capture registers + frame chain for the suspended task."""
    banner(f"4. Register snapshot: {task_name!r}")
    try:
        snap = await debug_tool.debug_task_snapshot(
            fleet, "target", task_name, max_frames=24,
        )
    except Exception as e:
        print(f"  task_snapshot failed: {e}")
        return {}

    regs = snap.registers
    print(f"  state       = {snap.state}")
    print(f"  rtcf_filled = {snap.rtcf_filled}")
    print(f"  pc          = 0x{regs.pc:08x}")
    print(f"  sp          = 0x{regs.sp:08x}")
    print(f"  lr          = 0x{regs.lr:08x}")
    print(f"  msr         = 0x{regs.msr:08x}")
    print(f"  cr          = 0x{regs.cr:08x}")
    print(f"  ctr         = 0x{regs.ctr:08x}")
    print(f"  xer         = 0x{regs.xer:08x}")
    print(f"  traptype    = 0x{regs.traptype:08x}")
    print(f"  dar         = 0x{regs.dar:08x}  "
          f"<- data-access fault address")
    print(f"  dsisr       = 0x{regs.dsisr:08x}")
    print()
    print("  Frame chain:")
    for f in snap.backtrace[:12]:
        print(f"    [{f.index:>2}]  pc=0x{f.pc:08x}  "
              f"sp=0x{f.sp:08x}")
    return snap.model_dump()


async def phase_stacktrace(fleet: Fleet, task_name: str) -> None:
    """IDebug-symbolicated stacktrace."""
    banner(f"5. Symbolicated stacktrace: {task_name!r}")
    try:
        st = await debug_tool.debug_stacktrace(
            fleet, "target", task_name, max_frames=24,
        )
    except Exception as e:
        print(f"  stacktrace failed: {e}")
        return
    print(f"  rc={st.stacktrace_rc}  frames={st.frame_count}")
    for f in st.frames:
        label = f.function or f.module or "?"
        src = (f"  {f.source_file}:{f.source_line}"
               if f.source_file and f.source_line else "")
        print(f"    [{f.index:>2}]  0x{f.address:08x}  "
              f"({f.state_name})  {label}{src}")


async def phase_resolve(fleet: Fleet, addresses: list[int]) -> None:
    """Resolve a set of addresses through IDebug symbol lookup."""
    banner("6. IDebug address resolution")
    for addr in addresses:
        try:
            sym = await debug_tool.debug_symbol(fleet, "target", addr)
        except Exception as e:
            print(f"  0x{addr:08x}: {type(e).__name__}: {e}")
            continue
        if not sym.resolved:
            print(f"  0x{addr:08x}: <unresolved>")
            continue
        bits = []
        if sym.module:
            bits.append(f"module={sym.module!r}")
        if sym.function:
            bits.append(f"func={sym.function!r}")
        if sym.source_file and sym.source_line:
            bits.append(f"{sym.source_file}:{sym.source_line}")
        elif sym.source_file:
            bits.append(sym.source_file)
        if sym.offset:
            bits.append(f"+0x{sym.offset:x}")
        print(f"  0x{addr:08x}: " + ", ".join(bits))


async def phase_kernel_log(fleet: Fleet) -> None:
    """Capture the kernel debug ring buffer."""
    banner("7. Kernel debug buffer (DumpDebugBuffer)")
    try:
        r = await exec_tool.exec_cmd(
            fleet, "target", "C:DumpDebugBuffer", timeout_s=10.0,
        )
        if not r.output.strip():
            print("  (buffer empty - kernel.debug may be needed for "
                  "richer output)")
            return
        lines = r.output.strip().splitlines()
        print(f"  {len(lines)} lines captured. Tail (last 30):")
        for line in lines[-30:]:
            print(f"    | {line}")
    except Exception as e:
        print(f"  DumpDebugBuffer failed: {type(e).__name__}: {e}")


async def phase_libs(fleet: Fleet) -> None:
    """Sample the library / device list to see what was loaded at
    crash time. We can't query "what task X had open" directly, but
    seeing high-version libs (gl4es, opengl, MiniGL, AHI etc.) hints
    at what subsystem the fault may have come from."""
    banner("8. Notable libraries / devices currently open")
    libs = await sys_tool.sys_libraries(fleet, "target")
    devs = await sys_tool.sys_devices(fleet, "target")
    interesting_libs = [
        l for l in libs
        if any(k in l.name.lower() for k in [
            "minigl", "opengl", "gl4es", "ahi", "sdl", "amissl",
            "newlib", "amisslmaster", "freetype", "z.library",
            "tinygl",
        ])
    ]
    if interesting_libs:
        print("  Interesting libraries open:")
        for l in interesting_libs[:12]:
            print(f"    {l.name}  v{l.version}.{l.revision}  "
                  f"opens={l.open_count}")
    else:
        print("  (no obviously game-related libs in the open list)")
    print()
    print(f"  Total libraries open: {len(libs)}")
    print(f"  Total devices open:   {len(devs)}")


async def phase_binary_correlate(
    fleet: Fleet, binary: Path, faulting_addr: int,
) -> None:
    """Re-deploy the local binary briefly to read its symbol table
    and hint where the faulting PC lands. Crude: AmigaOS doesn't
    expose a per-task load base over MCP today, so we can only show
    candidate symbols whose value is "close to" the faulting address
    or which look likely (entry point, segments)."""
    banner("9. Symbol correlation against local ELF")
    if not binary.exists():
        print(f"  --binary {binary} not found; skipping")
        return
    REMOTE = f"RAM:{binary.name}.investigate"
    print(f"  redeploying {binary} -> {REMOTE} for symbol read...")
    try:
        blob = binary.read_bytes()
        await fs_tool.fs_write(
            fleet, "target", REMOTE,
            base64.b64encode(blob).decode(),
        )
    except Exception as e:
        print(f"  redeploy failed: {e}")
        return

    try:
        es = await sys_tool.sys_executable_symbols(
            fleet, "target", REMOTE, max=4096,
        )
        print(f"  sections={es.num_sections}  "
              f"symbols={es.symbol_count}  "
              f"truncated={es.truncated}")

        funcs = [s for s in es.symbols if s.type_name == "func"]
        funcs.sort(key=lambda s: s.value)
        print(f"  function symbols recovered: {len(funcs)}")

        # The faulting PC is likely inside a relocated segment. We
        # don't know the load base, but if the binary's text segment
        # starts near a low non-zero address (common for AOS4 ELFs),
        # the relative offset of the fault within the segment may
        # match a symbol delta.
        if funcs:
            print()
            print("  First 8 function symbols (smallest value):")
            for s in funcs[:8]:
                print(f"    0x{s.value:08x}  +{s.size:>6}  {s.name}")
            print()
            print("  Last 4 function symbols (largest value):")
            for s in funcs[-4:]:
                print(f"    0x{s.value:08x}  +{s.size:>6}  {s.name}")

            # The static-link case: ELF .text is usually 0x01000000 +
            # offset; a fault at 0x6dff17c0 with the binary loaded at
            # ~0x6d000000 is plausible. Try the "relative offset"
            # heuristic: assume base = (faulting_addr & 0xFF000000)
            # and see which function's value lands closest.
            base_guess = faulting_addr & 0xFF000000
            target = faulting_addr - base_guess
            below = [s for s in funcs if s.value <= target]
            if below:
                nearest = max(below, key=lambda s: s.value)
                print()
                print(f"  Heuristic: assuming load base "
                      f"0x{base_guess:08x},")
                print(f"  faulting PC offset = 0x{target:08x}")
                print(f"  -> closest preceding symbol: "
                      f"{nearest.name}")
                print(f"     at 0x{nearest.value:08x} "
                      f"+{target - nearest.value} bytes into it")
                print()
                print("  Caveat: this is a guess. AOS4's ELF loader "
                      "may relocate at any base.")
    finally:
        with suppress(Exception):
            await fs_tool.fs_delete(fleet, "target", REMOTE)


# --------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------

async def amain(args: argparse.Namespace) -> int:
    fleet = make_fleet(args.endpoint)
    try:
        t = fleet.mcpd("target")

        la = await phase_alert(t)
        await phase_screens(fleet)
        task_name = await phase_tasks(fleet, args.task_name)

        snap: dict = {}
        if task_name:
            snap = await phase_snapshot(fleet, task_name)
            await phase_stacktrace(fleet, task_name)

        # Addresses to resolve through IDebug symbol lookup.
        addrs: list[int] = []
        # Faulting address from the alert payload (typically [1]).
        if "values" in la and len(la["values"]) > 1:
            addrs.append(int(la["values"][1]) & 0xFFFFFFFF)
        # Saved registers from the task snapshot.
        if snap:
            regs = snap.get("registers", {})
            for key in ("pc", "lr", "ctr", "dar"):
                if regs.get(key):
                    addrs.append(int(regs[key]))
        # Dedup, drop zeros.
        seen: set[int] = set()
        addrs = [a for a in addrs
                 if a and not (a in seen or seen.add(a))]
        if addrs:
            await phase_resolve(fleet, addrs)

        await phase_kernel_log(fleet)
        await phase_libs(fleet)

        if args.binary:
            faulting = (
                int(la["values"][1]) & 0xFFFFFFFF
                if "values" in la and len(la["values"]) > 1 else 0
            )
            if faulting:
                await phase_binary_correlate(
                    fleet, Path(args.binary), faulting,
                )

    finally:
        await fleet.close_all()
    return 0


def main() -> int:
    import os
    p = argparse.ArgumentParser()
    p.add_argument("--endpoint", default=os.environ.get("MCPD_ENDPOINT"),
                   help="MCPd endpoint (default $MCPD_ENDPOINT; required)")
    p.add_argument(
        "--binary", default=None,
        help="local path to the crashed program's ELF, for symbol "
             "correlation",
    )
    p.add_argument(
        "--task-name", default=None,
        help="substring to search for in sys.tasks (in addition to "
             "the built-in 'doom/dark/sdl/reaper' heuristics)",
    )
    args = p.parse_args()
    if not args.endpoint:
        p.error("--endpoint or $MCPD_ENDPOINT is required")
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
