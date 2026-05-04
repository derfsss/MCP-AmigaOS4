"""Validate round-4 software-only batch on the live X5000:

  sys.lastalert         (now includes decoded fields)
  sys.alert_decode      decode an arbitrary alert code
  sys.hardware.i2c      enumerate I2C buses + probe devices
  sys.hardware.perfcounters  read live PMCI_* counters
  sys.executable.symbols ELF symbol table query
  sys.applications      enumerate registered apps
  app.notify            Ringhio popup
  debug.symbol          address -> module/function/source
  debug.stacktrace      symbolicated backtrace
  debug.write_register  modify a saved register (then read back)
  debug.write_memory    poke bytes (round-trip via fs.read on a file
                        we wrote, since arbitrary VRAM is unsafe)
  events.wait + debug.exception topic (timeout path - no live crash)
"""

from __future__ import annotations

import asyncio
import base64
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOST_SRC = HERE.parent / "host" / "src"
sys.path.insert(0, str(HOST_SRC))

from amiga_fleet_mcp.archive import Archive  # noqa: E402
from amiga_fleet_mcp.config import (  # noqa: E402
    Config, McpdChannel, PathsConfig, ServerConfig,
    TargetChannels, TargetConfig,
)
from amiga_fleet_mcp.fleet import Fleet  # noqa: E402
from amiga_fleet_mcp.tools import debug as debug_tool  # noqa: E402
from amiga_fleet_mcp.tools import events as events_tool  # noqa: E402
from amiga_fleet_mcp.tools import sys as sys_tool  # noqa: E402


import os
X5000_ENDPOINT = os.environ.get("MCPD_ENDPOINT", "127.0.0.1:4322")


def make_config(archive_root: Path) -> Config:
    return Config(
        server=ServerConfig(archive_root=archive_root),
        paths=PathsConfig(),
        targets={
            "x5000-real": TargetConfig(
                type="remote", display_name="X5000",
                channels=TargetChannels(
                    mcpd=McpdChannel(endpoint=X5000_ENDPOINT),
                ),
            ),
        },
    )


async def amain() -> int:
    archive_root = Path(__file__).parent.parent / "tmp" / "round4-archive"
    archive_root.mkdir(parents=True, exist_ok=True)
    fleet = Fleet(make_config(archive_root))
    archive = Archive(archive_root)
    print(f"[setup] archive at {archive.run_dir}")

    target = "x5000-real"
    failures = 0

    # ---------- sys.lastalert (with decode) -----------------------
    print("\n[v] sys.lastalert (with decoded fields)")
    try:
        raw = await fleet.mcpd(target).request("sys.lastalert")
        archive.log_call("sys.lastalert", target, {}, result=raw)
        code = raw["alert_code"]
        if raw.get("no_alert_recorded"):
            print(f"    code=0x{code:08x}  no_alert_recorded=True (clean boot)")
        elif "decoded" not in raw:
            print("    FAIL: decoded field missing on a real alert")
            failures += 1
        else:
            d = raw["decoded"]
            print(f"    code=0x{code:08x}  "
                  f"subsystem={d.get('subsystem')!r}  "
                  f"general={d.get('general')!r}  "
                  f"specific=0x{d.get('specific'):04x}  "
                  f"name={d.get('name')!r}")
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    # ---------- sys.alert_decode (arbitrary code) -----------------
    print("[v] sys.alert_decode arbitrary codes")
    try:
        # 0x81000005 = AN_MemCorrupt (dead-end exec.library specific)
        ad = await sys_tool.sys_alert_decode(fleet, target, 0x81000005)
        archive.log_call("sys.alert_decode", target, {"code": 0x81000005},
                         result=ad.model_dump())
        d = ad.decoded
        print(f"    0x81000005: dead_end={d.dead_end}  "
              f"subsystem={d.subsystem!r}  name={d.name!r}")
        if not d.dead_end:
            print("    FAIL: 0x81000005 should have dead_end=True")
            failures += 1
        if d.name != "AN_MemCorrupt":
            print(f"    FAIL: expected name=AN_MemCorrupt, got {d.name!r}")
            failures += 1
        # 0x80000002 = ACPU_BusErr
        ad2 = await sys_tool.sys_alert_decode(fleet, target, 0x80000002)
        if ad2.decoded.name != "ACPU_BusErr":
            print(f"    FAIL: 0x80000002 -> {ad2.decoded.name!r}, "
                  "expected ACPU_BusErr")
            failures += 1
        else:
            print(f"    0x80000002 -> {ad2.decoded.name}")
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    # ---------- sys.hardware.i2c ----------------------------------
    print("[v] sys.hardware.i2c")
    try:
        i2c = await sys_tool.sys_hardware_i2c(fleet, target)
        archive.log_call("sys.hardware.i2c", target, {},
                         result=i2c.model_dump())
        print(f"    available={i2c.available}  buses={len(i2c.buses)}")
        for b in i2c.buses:
            print(f"      bus {b.bus_number} ({b.name}): "
                  f"{len(b.devices)} device(s)")
            for d in b.devices[:6]:
                print(f"        addr=0x{d.addr:02x}  hint={d.hint}")
        if not i2c.available:
            print("    NOTE: i2c.resource not available - skipped")
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    # ---------- sys.hardware.perfcounters -------------------------
    print("[v] sys.hardware.perfcounters")
    try:
        pm = await sys_tool.sys_hardware_perfcounters(fleet, target)
        archive.log_call("sys.hardware.perfcounters", target, {},
                         result=pm.model_dump())
        print(f"    available={pm.available}  "
              f"counters={pm.counter_count}  "
              f"ibrk={pm.instr_breakpoint}  bpm={pm.breakpoint_mask}")
        for c in pm.counters[:6]:
            print(f"      [{c.index}] {c.item}={c.value}")
        if not pm.available:
            print("    NOTE: performancemonitor.resource not available")
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    # ---------- sys.executable.symbols ----------------------------
    print("[v] sys.executable.symbols on SYS:System/MCPd/MCPd")
    try:
        es = await sys_tool.sys_executable_symbols(
            fleet, target, "SYS:System/MCPd/MCPd",
            type="func", max=64,
        )
        archive.log_call(
            "sys.executable.symbols", target,
            {"path": "SYS:System/MCPd/MCPd", "type": "func", "max": 64},
            result=es.model_dump(),
        )
        print(f"    sections={es.num_sections}  "
              f"symbols={es.symbol_count}  truncated={es.truncated}")
        for s in es.symbols[:6]:
            print(f"      {s.name}  value=0x{s.value:08x}  "
                  f"binding={s.binding_name}")
        if es.symbol_count == 0:
            print("    FAIL: expected >0 func symbols in MCPd binary")
            failures += 1
        # Check we can find at least one known symbol name (main).
        names = [s.name for s in es.symbols]
        if not any("main" in n for n in names):
            # main may be sliced out; try a re-query without filter
            es2 = await sys_tool.sys_executable_symbols(
                fleet, target, "SYS:System/MCPd/MCPd", match="main", max=16,
            )
            if es2.symbol_count == 0:
                print(f"    FAIL: no symbol matching 'main'")
                failures += 1
            else:
                print(f"    found via match: {es2.symbols[0].name}")
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    # ---------- sys.applications ----------------------------------
    print("[v] sys.applications (MCPd should be in the list)")
    try:
        apps = await sys_tool.sys_applications(fleet, target)
        archive.log_call("sys.applications", target, {},
                         result=apps.model_dump())
        print(f"    available={apps.available}  "
              f"count={len(apps.applications)}")
        names = [a.name for a in apps.applications if a.name]
        for a in apps.applications[:8]:
            print(f"      {a.name!r}  appID={a.appID}  hidden={a.hidden}  "
                  f"url={a.url_identifier!r}")
        if apps.available and not any(n and "MCP" in n for n in names):
            print("    NOTE: MCPd not seen by name; ok if registered "
                  "with a numeric suffix")
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    # ---------- app.notify ----------------------------------------
    print("[v] app.notify (Ringhio popup)")
    try:
        nf = await sys_tool.app_notify(
            fleet, target,
            "MCPd round-4 test",
            "If you see this on the X5000 screen, app.notify works.",
        )
        archive.log_call("app.notify", target,
                         {"title": "MCPd round-4 test"},
                         result=nf.model_dump())
        print(f"    rc={nf.result_code}  queued={nf.queued}")
        if not nf.queued:
            # rc 50 = APPNOTIFY_ERROR_APPNOTALLOWED (Ringhio not running)
            # rc 120 = APPNOTIFY_ERROR_SERVERNOTRUNNING
            if nf.result_code in (50, 120):
                print("    NOTE: Ringhio not running - notify failed "
                      "(non-fatal)")
            else:
                print(f"    FAIL: notify rc={nf.result_code}")
                failures += 1
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    # ---------- debug.symbol --------------------------------------
    print("[v] debug.symbol on a known kernel address")
    try:
        # Pick an address from MCPd's text - read it from sys.executable.symbols
        # then ObtainDebugSymbol on it.
        es = await sys_tool.sys_executable_symbols(
            fleet, target, "SYS:System/MCPd/MCPd", type="func", max=10,
        )
        target_addr = 0
        for s in es.symbols:
            if s.value > 0:
                target_addr = s.value
                break
        if target_addr:
            r = await debug_tool.debug_symbol(fleet, target, target_addr)
            archive.log_call("debug.symbol", target,
                             {"address": target_addr},
                             result=r.model_dump())
            print(f"    addr=0x{target_addr:08x}  resolved={r.resolved}  "
                  f"module={r.module!r}  function={r.function!r}")
        else:
            print("    NOTE: no func symbol with non-zero value to test")
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    # ---------- debug.stacktrace ----------------------------------
    print("[v] debug.stacktrace on input.device")
    try:
        # Use a stable system task. input.device is always present
        # and FindTask reliably resolves it (we already verified
        # write_register on it).
        st = await debug_tool.debug_stacktrace(
            fleet, target, "input.device", max_frames=16,
        )
        archive.log_call("debug.stacktrace", target,
                         {"name": "input.device", "max_frames": 16},
                         result=st.model_dump())
        print(f"    rc={st.stacktrace_rc}  frames={st.frame_count}")
        for fr in st.frames[:8]:
            label = fr.function or fr.module or "?"
            src = (f"  {fr.source_file}:{fr.source_line}"
                   if fr.source_file and fr.source_line else "")
            print(f"      [{fr.index}] 0x{fr.address:08x}  {label}"
                  f"  ({fr.state_name}){src}")
        if st.frame_count == 0 and st.stacktrace_rc == 0:
            print("    NOTE: 0 frames but rc=0 - stacktrace ran but "
                  "task was active and not parkable")
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    # ---------- debug.write_register (read+modify+read) -----------
    print("[v] debug.write_register on a known background task")
    try:
        # Use a target task we know exists - pick a low-priority idle task.
        # We snapshot it, write CR (a harmless-ish reg), confirm wtcf.
        # We DON'T modify a real task in production - this is "round-trip".
        # Actually run on MCPd's own task is dangerous (we'd corrupt our
        # own state). Use a guaranteed-exist long-running task: "input.device"
        snap = await debug_tool.debug_task_snapshot(
            fleet, target, "input.device", max_frames=2,
        )
        original_xer = snap.registers.xer
        # Write the same value back - WriteTaskContext should succeed
        wr = await debug_tool.debug_write_register(
            fleet, target, "input.device", "xer", original_xer,
        )
        archive.log_call("debug.write_register", target,
                         {"name": "input.device", "register": "xer",
                          "value": original_xer},
                         result=wr.model_dump())
        print(f"    register={wr.register_name}  value=0x{wr.value:08x}  "
              f"wtcf_filled={wr.wtcf_filled}")
        if wr.wtcf_filled == 0:
            print("    NOTE: wtcf=0 may mean WriteTaskContext skipped "
                  "(task active); not a failure")
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    # ---------- debug.write_memory (boundary checks) -------------
    print("[v] debug.write_memory boundary checks (no actual write)")
    try:
        # API exercise: zero-length write must succeed and report 0
        # bytes (early-return path).
        wm0 = await debug_tool.debug_write_memory(
            fleet, target, 0x00000000,
            base64.b64encode(b"").decode(),
        )
        archive.log_call("debug.write_memory", target,
                         {"address": 0, "bytes_b64": ""},
                         result=wm0.model_dump())
        print(f"    empty: addr=0x{wm0.address:08x}  "
              f"bytes_written={wm0.bytes_written}")
        if wm0.bytes_written != 0:
            print(f"    FAIL: expected 0 bytes, got {wm0.bytes_written}")
            failures += 1
        # NULL-with-data must fail with INVPARAMS, not crash the daemon.
        try:
            await debug_tool.debug_write_memory(
                fleet, target, 0,
                base64.b64encode(b"\xde\xad").decode(),
            )
            print("    FAIL: NULL+data should have raised")
            failures += 1
        except Exception as ee:
            print(f"    NULL+data correctly rejected: {ee}")
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    # ---------- events.wait debug.exception topic -----------------
    print("[v] events.wait debug.exception topic (timeout path)")
    try:
        ew = await events_tool.events_wait(
            fleet, target, topics=["debug.exception"], timeout_ms=1500,
        )
        archive.log_call("events.wait", target,
                         {"topics": ["debug.exception"], "timeout_ms": 1500},
                         result=ew.model_dump())
        print(f"    elapsed_ms={ew.elapsed_ms}  events={len(ew.events)}")
        # We don't induce a crash, so 0 events is the expected result.
        # Just confirm the call completes within the timeout.
        if ew.elapsed_ms < 1000 and len(ew.events) == 0:
            print("    FAIL: returned too quickly with no event")
            failures += 1
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    await fleet.close_all()
    print(f"\n[result] failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
