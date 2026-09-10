"""End-to-end validation of the `input.*` namespace against a live target.

`input.*` is the only tool surface in this project that *writes* to a
machine's UI, and its whole safety story is a gate that must be off
unless an operator deliberately turned it on. That story is only worth
anything if it is checked on a real AmigaOS, so this script drives a
target through the full cycle:

  gate closed by default -> enable via the sentinel -> restart ->
  gate open -> inject -> remove the sentinel -> restart -> gate closed

Sections
--------

  A. Gate closed by default      all 7 methods -> -32003, caps + ring
  B. Sentinel enable             written, ignored until a restart
  C. Read-only state             pointer / focus / geometry
  D. Pointer                     absolute move lands where asked
  E. Typing                      ASCII, then non-ASCII end-to-end
  F. Drag + scroll               a real window move
  G. Confirm guards              daemon-side, bypassing the wrapper
  H. Sentinel disable            gate closed again after a restart

Section E is the one that matters most for regressions. `input.type`
receives UTF-8 on the wire but both of the daemon's mapping paths are
one-byte ANSI, so the daemon decodes to codepoints first. The
discriminator is that "cafe"-with-an-acute is 4 characters and 5 UTF-8
bytes: `text_len == 4` and a 4-byte file mean the decode happened,
`5` means something walked the bytes.

The typed output is written to the guest's `SHARED:` volume (a 9p
share pointing at a host directory) so the bytes can be inspected on
the host rather than read back through the same daemon that typed
them. Pass `--shared-dir` to point at the host side of that share.
Without it -- real hardware has no such share -- the same files go to
`T:` and are read back with `fs.read`: less independent, but still
the Shell's own output rather than `input.type`'s self-report.

QEMU targets only for the restart cycles: the script kills and
relaunches the QEMU process, because neither QMP `system_reset` nor a
guest-side Reboot works reliably for AmigaOS 4 guests. On real
hardware use `--restart-mode cold`, which reboots via
`sys.cold_reboot` and falls back to an MCU power cycle when the board
doesn't come back; `--restart-mode manual` prompts instead.

Usage:
    python scripts/validate_input.py --target qemu-pegasos2-sm501 \\
        --shared-dir S:/temp
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _paths import repo_root

sys.path.insert(0, str(repo_root() / "host" / "src"))

from amiga_fleet_mcp.config import InputConfig, load_config
from amiga_fleet_mcp.errors import InvalidParams, NotCapable
from amiga_fleet_mcp.fleet import Fleet
from amiga_fleet_mcp.tools import exec as exec_tool
from amiga_fleet_mcp.tools import fs as fs_tool
from amiga_fleet_mcp.tools import input as input_tool
from amiga_fleet_mcp.tools import power as power_tool
from amiga_fleet_mcp.tools import qemu as qemu_tool
from amiga_fleet_mcp.tools import sys as sys_tool

SENTINEL = "SYS:System/MCPd/ENABLE-INPUT"
ACUTE = "caf\u00e9"          # 4 characters, 5 bytes of UTF-8
CJK = "\u65e5"               # U+65E5 -- above anything an Amiga keymap has

# QMP `quit` stops QEMU dead, and AmigaOS writes its filesystem cache
# back lazily. A file written seconds before the kill is simply not in
# the disk image afterwards -- and an `fs.upload` with verify=True does
# NOT catch it, because the read-back comes from the same dirty cache.
# 45 s of idle was measured as enough for a 300 KB write to land; both
# the sentinel writes and its deletion need it too.
WRITEBACK_SETTLE_S = 45.0

# This script prints non-ASCII on purpose -- that is the thing under
# test -- and a Windows console is cp1252.
sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail else ""), flush=True)
    return ok


def log(msg: str) -> None:
    print(f"\n=== {msg}", flush=True)


def port_open(endpoint: str, timeout: float = 1.0) -> bool:
    host, port = endpoint.split(":")
    try:
        with socket.create_connection((host, int(port)), timeout):
            return True
    except OSError:
        return False


class Runner:
    def __init__(self, fleet: Fleet, target: str, *,
                 shared_dir: Path | None, restart_mode: str) -> None:
        self.fleet = fleet
        self.target = target
        self.shared = shared_dir
        # "qemu"   kill + relaunch the QEMU process
        # "cold"   sys.cold_reboot, with an MCU power-cycle fallback
        # "manual" prompt the operator and wait
        self.restart_mode = restart_mode
        self.endpoint = fleet.target_config(target).channels.mcpd.endpoint

    # ---- plumbing --------------------------------------------------

    async def wait_up(self, timeout_s: float = 300.0) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if port_open(self.endpoint):
                # The listener answers before the rest of the machine
                # is ready; the settle is the documented gotcha.
                await asyncio.sleep(3.0)
                try:
                    await sys_tool.sys_version(self.fleet, self.target)
                    return True
                except Exception:
                    pass
            await asyncio.sleep(2.0)
        return False

    async def restart(self, why: str) -> bool:
        print(f"  idling {WRITEBACK_SETTLE_S:.0f}s for AmigaOS write-back",
              flush=True)
        await asyncio.sleep(WRITEBACK_SETTLE_S)
        log(f"restarting target ({why})")

        if self.restart_mode == "manual":
            print("  restart MCPd on the target now, then press Enter",
                  flush=True)
            await asyncio.to_thread(input)
        elif self.restart_mode == "cold":
            if not await self._cold_restart():
                return False
        else:
            try:
                await qemu_tool.qemu_stop(self.fleet, self.target)
            except Exception as e:
                print(f"  (stop: {e})", flush=True)
            await asyncio.sleep(5.0)
            await qemu_tool.qemu_start(self.fleet, self.target)

        ok = await self.wait_up()
        print(f"  target back: {ok}", flush=True)
        return ok

    async def _cold_restart(self) -> bool:
        """Reboot real hardware. ColdReboot doesn't always bring an
        X5000 back on the first try, so fall back to an MCU power
        cycle when the machine hasn't reappeared in time."""
        try:
            await sys_tool.sys_cold_reboot(
                self.fleet, self.target, confirm=True)
        except Exception as e:
            print(f"  (cold_reboot: {e})", flush=True)
        await asyncio.sleep(20.0)
        if await self.wait_up(timeout_s=240.0):
            return True
        print("  cold reboot didn't come back -- MCU power cycle",
              flush=True)
        try:
            await power_tool.power_off(self.fleet, self.target, confirm=True)
            await asyncio.sleep(15.0)
            await power_tool.power_on(self.fleet, self.target, confirm=True)
        except Exception as e:
            print(f"  (power cycle failed: {e})", flush=True)
            return False
        return True

    async def gate_open(self) -> bool:
        caps = await self.fleet.mcpd(self.target).request(
            "proto.capabilities", {})
        return bool(caps.get("input", {}).get("enabled", False))

    async def ring(self) -> str:
        r = await sys_tool.sys_debug_ring(
            self.fleet, self.target, max_lines=2000)
        return "\n".join(r.lines)

    async def check_ring(self, name: str, needle: str) -> None:
        """Assert a beacon is in the kernel debug ring -- unless this
        machine's ring never captured MCPd at all.

        AmigaOS's debug buffer does not wrap: once full it stops
        accepting entries. On a real X5000 with a chatty graphics
        driver (amdgpu-os4 logs ~80 KB during boot) the buffer is
        already exhausted before MCPd starts, so nothing the daemon
        emits can land there -- measured: 711 lines, zero growth over
        60 s with two daemons running. That is a property of the
        machine, not of the gate, so report it as SKIP rather than
        failing a check the daemon cannot influence. The authoritative
        gate reading is proto.capabilities, which is checked either
        way.
        """
        ring = await self.ring()
        if needle in ring:
            check(name, True)
        elif "[MCPd]" not in ring:
            print(f"  [SKIP] {name} -- this machine's debug buffer holds "
                  "no MCPd output at all (full before the daemon "
                  "started; AmigaOS's ring does not wrap)", flush=True)
        else:
            check(name, False, "MCPd is in the ring but this line isn't")

    async def sentinel_present(self) -> bool:
        try:
            await self.fleet.mcpd(self.target).request(
                "fs.stat", {"path": SENTINEL})
            return True
        except Exception:
            return False

    def typed_path(self, name: str) -> str:
        """Where the Shell should redirect its output to."""
        return f"SHARED:{name}" if self.shared is not None else f"T:{name}"

    async def clear_typed(self, name: str) -> None:
        if self.shared is not None:
            (self.shared / name).unlink(missing_ok=True)
            return
        try:
            await fs_tool.fs_delete(self.fleet, self.target, f"T:{name}")
        except Exception:
            pass

    async def read_typed(self, name: str) -> bytes:
        """Read back what the Shell actually received.

        Preferred route is the host side of the guest's 9p SHARED:
        volume, so the bytes are inspected outside the daemon that
        typed them. Real hardware has no such share, so fall back to
        fs.read of a T: file -- less independent, but still the
        Shell's own output rather than input.type's self-report.
        """
        if self.shared is not None:
            p = self.shared / name
            for _ in range(10):
                if p.exists():
                    try:
                        return p.read_bytes()
                    except OSError:
                        pass
                time.sleep(1.0)
            return b"<missing>"
        for _ in range(10):
            try:
                r = await fs_tool.fs_read(
                    self.fleet, self.target, f"T:{name}")
                return base64.b64decode(r.content_b64)
            except Exception:
                await asyncio.sleep(1.0)
        return b"<missing>"

    # ---- A: gate closed --------------------------------------------

    async def all_refused(self) -> tuple[int, list[str]]:
        f, t = self.fleet, self.target
        calls = [
            ("state", lambda: input_tool.input_state(f, t)),
            ("type", lambda: input_tool.input_type(
                f, t, text="x", confirm=True)),
            ("key", lambda: input_tool.input_key(
                f, t, keys=["esc"], confirm=True)),
            ("mouse_move", lambda: input_tool.input_mouse_move(
                f, t, dx=1, dy=0)),
            ("click", lambda: input_tool.input_click(f, t, confirm=True)),
            ("drag", lambda: input_tool.input_drag(
                f, t, from_x=10, from_y=10, to_x=20, to_y=20, confirm=True)),
            ("scroll", lambda: input_tool.input_scroll(f, t)),
        ]
        refused, leaked = 0, []
        for name, fn in calls:
            try:
                await fn()
                leaked.append(name)
            except Exception as e:
                if isinstance(e, NotCapable) or "32003" in str(e) \
                   or "disabled" in str(e).lower():
                    refused += 1
                else:
                    leaked.append(f"{name}({type(e).__name__}: {e})")
        return refused, leaked

    async def section_gate_closed(self) -> None:
        log("A. gate closed by default")
        check("proto.capabilities reports input.enabled false",
              await self.gate_open() is False)
        refused, leaked = await self.all_refused()
        check("all 7 methods refuse", refused == 7 and not leaked,
              f"refused={refused} leaked={leaked}")
        await self.check_ring("debug ring shows input_gate state=disabled",
                              "input_gate state=disabled")
        await self.check_ring("ready beacon carries input=off", "input=off")

    # ---- B: enable --------------------------------------------------

    async def section_enable(self) -> bool:
        log("B. enable via the sentinel file")
        await fs_tool.fs_write(
            self.fleet, self.target, SENTINEL,
            base64.b64encode(b"enabled by validate_input.py\n").decode())
        check("sentinel written", await self.sentinel_present())
        check("gate stays closed until a restart",
              await self.gate_open() is False)
        if not await self.restart("pick up the sentinel"):
            return check("target returns after enabling", False)
        check("proto.capabilities reports input.enabled true",
              await self.gate_open() is True)
        await self.check_ring("debug ring shows input_gate state=ENABLED",
                              "input_gate state=ENABLED")
        await self.check_ring("ready beacon carries input=on", "input=on")
        return True

    # ---- C-F: the actual injection ----------------------------------

    async def section_inject(self) -> None:
        f, t = self.fleet, self.target

        log("C. input.state")
        st = await input_tool.input_state(f, t)
        check("state returns screen geometry",
              bool(st.screen_width and st.screen_height),
              f"{st.screen_width}x{st.screen_height} "
              f"pointer=({st.pointer_x},{st.pointer_y})")

        log("D. input.mouse_move")
        mx = (st.screen_width or 640) // 2
        my = (st.screen_height or 480) // 2
        mv = await input_tool.input_mouse_move(f, t, x=mx, y=my)
        check("absolute move lands where asked",
              mv.x == mx and mv.y == my,
              f"asked ({mx},{my}) got ({mv.x},{mv.y})")

        await self._section_typing()

        log("F. input.drag + input.scroll")
        before = await input_tool.input_state(f, t)
        bx = before.active_window_left or 0
        by = before.active_window_top or 0
        d = await input_tool.input_drag(
            f, t, from_x=bx + 60, from_y=by + 5,
            to_x=bx + 120, to_y=by + 45, confirm=True)
        await asyncio.sleep(1.5)
        after = await input_tool.input_state(f, t)
        # Intuition clamps a window to the screen, so a full-width
        # window only moves vertically. Either axis moving is enough.
        check("drag moved the active window",
              (after.active_window_left, after.active_window_top)
              != (bx, by),
              f"({bx},{by}) -> ({after.active_window_left},"
              f"{after.active_window_top}) events={d.events} "
              f"truncated={d.truncated}")
        sc = await input_tool.input_scroll(f, t, clicks=3)
        check("scroll emits one event per click", sc.events == 3,
              f"events={sc.events}")

    async def _section_typing(self) -> None:
        f, t = self.fleet, self.target

        log("E. typing -- a Shell to receive the keystrokes")
        await exec_tool.exec_cmd(
            f, t, "Run >NIL: <NIL: NewShell", timeout_s=20.0)
        await asyncio.sleep(4.0)
        st = await input_tool.input_state(f, t)
        if not check("a Shell window is active",
                     "Shell" in str(st.active_window),
                     f"active_window={st.active_window!r}"):
            return

        await self.clear_typed("mcpd-typed.txt")
        r = await input_tool.input_type(
            f, t,
            text=f"Echo TYPED-OK >{self.typed_path('mcpd-typed.txt')}",
            confirm=True)
        await input_tool.input_key(f, t, keys=["return"], confirm=True)
        await asyncio.sleep(3.0)
        got = (await self.read_typed("mcpd-typed.txt")).strip()
        check("ASCII string reaches the Shell", got == b"TYPED-OK",
              f"file={got!r} keymap={r.keymap} unmapped={r.unmapped}")

        log("E. typing -- non-ASCII (UTF-8 decode)")
        r2 = await input_tool.input_type(f, t, text=ACUTE, confirm=True)
        check("text_len counts characters, not UTF-8 bytes",
              r2.text_len == 4,
              f"text_len={r2.text_len} (4 = decoded, 5 = a byte walk)")
        check("one accented character is at most one unmapped entry",
              len(r2.unmapped) <= 1,
              f"unmapped={r2.unmapped} chars_mapped={r2.chars_mapped} "
              f"keymap={r2.keymap}")
        check("no unmapped entry is a bare UTF-8 continuation byte",
              not any(u in ("U+00C3", "U+00A9", "0xC3", "0xA9")
                      for u in r2.unmapped),
              f"unmapped={r2.unmapped}")
        # That typed onto the command line without a Return; clear it
        # so the next command doesn't land behind it.
        await input_tool.input_key(f, t, keys=["ctrl", "x"], confirm=True)
        await asyncio.sleep(1.0)

        await self.clear_typed("mcpd-typed2.txt")
        await input_tool.input_type(
            f, t,
            text=f"Echo {ACUTE} >{self.typed_path('mcpd-typed2.txt')}",
            confirm=True)
        await input_tool.input_key(f, t, keys=["return"], confirm=True)
        await asyncio.sleep(3.0)
        raw = (await self.read_typed("mcpd-typed2.txt")).strip()
        check("the Shell received 4 characters, not 5", len(raw) == 4,
              f"got {len(raw)} bytes: {raw!r}")

        log("E. typing -- codepoint above U+00FF")
        try:
            await input_tool.input_type(f, t, text=CJK, confirm=True)
            check("host rejects a non-Latin-1 codepoint", False)
        except InvalidParams as e:
            check("host rejects a non-Latin-1 codepoint",
                  "U+65E5" in str(e), str(e)[:60])
        r3 = await f.mcpd(t).request(
            "input.type", {"text": CJK, "confirm": True})
        check("daemon reports it unmapped and types nothing",
              r3.get("unmapped") == ["U+65E5"]
              and r3.get("chars_mapped") == 0
              and r3.get("events") == 0, f"{r3}")

    # ---- G: guards ---------------------------------------------------

    async def section_guards(self) -> None:
        log("G. confirm guards (daemon-side, bypassing the wrapper)")
        t = self.fleet.mcpd(self.target)
        try:
            await t.request("input.type", {"text": "x"})
            check("daemon rejects input.type without confirm", False)
        except Exception as e:
            check("daemon rejects input.type without confirm",
                  "confirm" in str(e).lower(), str(e)[:80])
        try:
            await t.request("input.key", {
                "keys": ["ctrl", "lamiga", "ramiga"], "confirm": True})
            check("daemon rejects the reset chord without confirm_reset",
                  False, "ACCEPTED -- the machine may be rebooting")
        except Exception as e:
            check("daemon rejects the reset chord without confirm_reset",
                  "confirm_reset" in str(e), str(e)[:80])
        try:
            await input_tool.input_type(
                self.fleet, self.target, text="x")
            check("host rejects input.type without confirm", False)
        except InvalidParams as e:
            check("host rejects input.type without confirm", True,
                  str(e)[:50])

    # ---- H: disable ---------------------------------------------------

    async def section_disable(self) -> None:
        log("H. disable again")
        await fs_tool.fs_delete(self.fleet, self.target, SENTINEL)
        if not await self.restart("drop the sentinel"):
            check("target returns after disabling", False)
            return
        check("gate closed after removing the sentinel",
              await self.gate_open() is False)
        refused, leaked = await self.all_refused()
        check("all 7 methods refuse again", refused == 7 and not leaked,
              f"refused={refused} leaked={leaked}")


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Validate the input.* namespace end to end.")
    ap.add_argument("--target", required=True,
                    help="target name from the amiga-fleet-mcp config")
    ap.add_argument("--shared-dir",
                    help="host side of the guest's SHARED: volume; "
                         "without it the typed output goes to T: and is "
                         "read back with fs.read")
    ap.add_argument("--restart-mode", choices=("qemu", "cold", "manual"),
                    default="qemu",
                    help="how to restart the target between gate changes: "
                         "kill+relaunch QEMU (default), sys.cold_reboot "
                         "with an MCU power-cycle fallback (real hardware), "
                         "or prompt and wait")
    ap.add_argument("--keep-running", action="store_true",
                    help="leave the QEMU guest running at the end")
    args = ap.parse_args()

    cfg = load_config()
    if args.target not in cfg.targets:
        print(f"unknown target {args.target!r}; configured: "
              f"{sorted(cfg.targets)}")
        return 2
    # Open the host-side gate in memory only. The daemon gate is what
    # is under test, and writing this to the user's config file would
    # leave a live target configured for injection afterwards.
    cfg.targets[args.target].input = InputConfig(enabled=True)
    fleet = Fleet(cfg)

    shared = Path(args.shared_dir).resolve() if args.shared_dir else None
    if shared is not None and not shared.is_dir():
        print(f"--shared-dir {shared} is not a directory")
        return 2

    r = Runner(fleet, args.target, shared_dir=shared,
               restart_mode=args.restart_mode)

    if r.restart_mode == "qemu":
        # A QEMU left behind by an earlier run invalidates everything:
        # the second instance can't bind the same hostfwd port, so this
        # script would end up talking to the OLD guest, with whatever
        # gate state that one booted with.
        if port_open(r.endpoint):
            print("REFUSING: something already listens on "
                  f"{r.endpoint}. Kill the stale qemu-system-ppc first.")
            return 2
        log(f"starting {args.target}")
        await qemu_tool.qemu_start(fleet, args.target)
    if not await r.wait_up():
        check("target reachable", False)
        return 1
    check("target reachable", True)

    pv = await fleet.mcpd(args.target).request("proto.version", {})
    print(f"  {pv}", flush=True)

    # A previous run may have left the sentinel behind, which would
    # make section A meaningless.
    if await r.sentinel_present():
        log("sentinel left over from an earlier run -- removing")
        await fs_tool.fs_delete(fleet, args.target, SENTINEL)
        if not await r.restart("start from a closed gate"):
            return 1

    try:
        await r.section_gate_closed()
        if await r.section_enable():
            await r.section_inject()
            await r.section_guards()
            await r.section_disable()
    finally:
        if r.restart_mode == "qemu" and not args.keep_running:
            log("stopping guest")
            try:
                await qemu_tool.qemu_stop(fleet, args.target)
            except Exception as e:
                print(f"  (stop: {e})", flush=True)

    failed = [n for n, ok, _ in results if not ok]
    print("\n" + "=" * 62, flush=True)
    print(f"{len(results) - len(failed)}/{len(results)} checks passed",
          flush=True)
    for n in failed:
        print(f"  FAILED: {n}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
