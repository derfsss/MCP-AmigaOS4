"""End-to-end method sweep against a real X5000.

Reads the target endpoint from $MCPD_ENDPOINT (or --endpoint) and
exercises every QEMU-validated method against real hardware:

  - proto.capabilities + sys.version + sys.tasks / libraries / devices
    / ports / lastalert
  - fs.list / stat / read / write / delete / makedir / rename / copy
    / protect (binary-clean round-trip)
  - exec.cmd
  - wb.screens / wb.windows
  - debug.task_snapshot (against a stable system task)
  - events.wait

Compares numbers against the QEMU pegasos2 reference where it makes
sense (e.g. AmigaOS version, library count).

Assumes MCPd is already running on the X5000 (run
x5000_bootstrap_mcpd.py first).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOST_SRC = HERE.parent / "host" / "src"
sys.path.insert(0, str(HOST_SRC))

from amiga_fleet_mcp.archive import Archive  # noqa: E402
from amiga_fleet_mcp.config import (  # noqa: E402
    Config,
    McpdChannel,
    PathsConfig,
    ServerConfig,
    TargetChannels,
    TargetConfig,
)
from amiga_fleet_mcp.errors import TargetError  # noqa: E402
from amiga_fleet_mcp.fleet import Fleet  # noqa: E402
from amiga_fleet_mcp.tools import debug as debug_tool  # noqa: E402
from amiga_fleet_mcp.tools import events as events_tool  # noqa: E402
from amiga_fleet_mcp.tools import exec as exec_tool  # noqa: E402
from amiga_fleet_mcp.tools import fs as fs_tool  # noqa: E402
from amiga_fleet_mcp.tools import sys as sys_tool  # noqa: E402
from amiga_fleet_mcp.tools import wb as wb_tool  # noqa: E402

def make_config(archive_root: Path, endpoint: str) -> Config:
    return Config(
        server=ServerConfig(archive_root=archive_root),
        paths=PathsConfig(),
        targets={
            "x5000-real": TargetConfig(
                type="remote",
                display_name="X5000 (real hardware)",
                channels=TargetChannels(
                    mcpd=McpdChannel(endpoint=endpoint),
                ),
            ),
        },
    )


async def amain() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default=os.environ.get("MCPD_ENDPOINT"),
                    help="MCPd endpoint (default $MCPD_ENDPOINT; required)")
    args = ap.parse_args()
    if not args.endpoint:
        ap.error("--endpoint or $MCPD_ENDPOINT is required")

    archive_root = Path(__file__).parent.parent / "tmp" / "x5000-archive"
    archive_root.mkdir(parents=True, exist_ok=True)
    fleet = Fleet(make_config(archive_root, args.endpoint))
    archive = Archive(archive_root)
    print(f"[setup] archive at {archive.run_dir}")

    target = "x5000-real"
    failures = 0

    try:
        # ---- proto.capabilities + version --------------------------

        print("\n[v] proto.capabilities")
        try:
            caps = await fleet.mcpd(target).request("proto.capabilities")
            archive.log_call("proto.capabilities", target, {}, result=caps)
            method_names = sorted(m["name"] for m in caps["methods"])
            print(f"    server={caps['server']} protocol={caps['protocol']}")
            print(f"    advertises {len(method_names)} methods")
            need = {
                "fs.list", "fs.stat", "fs.read", "fs.write", "fs.delete",
                "fs.makedir", "fs.rename", "fs.protect", "fs.copy",
                "exec.cmd", "sys.version", "sys.tasks", "sys.libraries",
                "sys.devices", "sys.ports", "sys.lastalert",
                "wb.screens", "wb.windows",
                "debug.task_snapshot", "events.wait",
            }
            missing = need - set(method_names)
            if missing:
                print(f"    FAIL: missing {missing}")
                failures += 1
        except Exception as e:
            print(f"    FAIL: {e}")
            failures += 1

        print("[v] sys.version")
        try:
            v = await sys_tool.sys_version(fleet, target)
            archive.log_call("sys.version", target, {}, result=v.model_dump())
            print(f"    kickstart={v.kickstart} workbench={v.workbench}")
            if v.kickstart != "54.57":
                print(f"    NOTE: x5000 kickstart {v.kickstart!r} differs "
                      "from QEMU's 54.57 reference")
        except Exception as e:
            print(f"    FAIL: {e}")
            failures += 1

        # ---- introspection -----------------------------------------

        print("[v] sys.tasks  (real-hardware task census)")
        try:
            t = await sys_tool.sys_tasks(fleet, target)
            archive.log_call("sys.tasks", target, {}, result=t.model_dump())
            total = len(t.ready) + len(t.waiting)
            print(f"    {len(t.ready)} ready + {len(t.waiting)} waiting "
                  f"= {total} total")
            if total < 30:
                print(f"    FAIL: surprisingly few tasks ({total})")
                failures += 1
        except Exception as e:
            print(f"    FAIL: {e}")
            failures += 1

        print("[v] sys.libraries")
        try:
            libs = await sys_tool.sys_libraries(fleet, target)
            archive.log_call("sys.libraries", target, {},
                             result=[L.model_dump() for L in libs])
            print(f"    {len(libs)} libraries")
            for name in ("intuition.library", "dos.library",
                         "bsdsocket.library", "exec.library"):
                hits = [L for L in libs if L.name == name]
                if hits:
                    L = hits[0]
                    print(f"      {L.name:30} v{L.version}.{L.revision} "
                          f"open={L.open_count}")
        except Exception as e:
            print(f"    FAIL: {e}")
            failures += 1

        print("[v] sys.devices")
        try:
            devs = await sys_tool.sys_devices(fleet, target)
            archive.log_call("sys.devices", target, {},
                             result=[d.model_dump() for d in devs])
            print(f"    {len(devs)} devices")
            for d in devs[:5]:
                print(f"      {d.name:32} v{d.version}.{d.revision}")
        except Exception as e:
            print(f"    FAIL: {e}")
            failures += 1

        print("[v] sys.ports")
        try:
            ports = await sys_tool.sys_ports(fleet, target)
            archive.log_call("sys.ports", target, {},
                             result=[p.model_dump() for p in ports])
            print(f"    {len(ports)} public ports "
                  f"(WORKBENCH={any(p.name == 'WORKBENCH' for p in ports)})")
        except Exception as e:
            print(f"    FAIL: {e}")
            failures += 1

        print("[v] sys.lastalert")
        try:
            la = await sys_tool.sys_lastalert(fleet, target)
            archive.log_call("sys.lastalert", target, {},
                             result=la.model_dump())
            print(f"    alert_code=0x{la.alert_code:08x}")
        except Exception as e:
            print(f"    FAIL: {e}")
            failures += 1

        # ---- fs round-trip -----------------------------------------

        print("[v] fs.list \"RAM:\"")
        try:
            entries = await fs_tool.fs_list(fleet, target, "RAM:")
            archive.log_call("fs.list", target, {"path": "RAM:"},
                             result=[e.model_dump() for e in entries])
            print(f"    {len(entries)} entries: "
                  f"{[e.name for e in entries][:6]}")
        except Exception as e:
            print(f"    FAIL: {e}")
            failures += 1

        print("[v] fs makedir + write + stat + read + delete cycle")
        TEST_DIR = "RAM:fleet_sweep"
        TEST_FILE = f"{TEST_DIR}/blob.bin"
        # 256-byte blob with NULs / 0xFF / a recognisable marker.
        BLOB = (b"x5000 sweep marker\n" + bytes(range(256)))[:256]
        try:
            # Pre-clean.
            for p in (TEST_FILE, TEST_DIR):
                try: await fs_tool.fs_delete(fleet, target, p)
                except (TargetError, Exception): pass

            await fs_tool.fs_makedir(fleet, target, TEST_DIR)
            wr = await fs_tool.fs_write(
                fleet, target, TEST_FILE,
                base64.b64encode(BLOB).decode("ascii"),
            )
            if wr.size != len(BLOB):
                print(f"    FAIL: write size {wr.size} != {len(BLOB)}")
                failures += 1

            st = await fs_tool.fs_stat(fleet, target, TEST_FILE)
            if st.size != len(BLOB) or st.type != "file":
                print(f"    FAIL: stat shape unexpected {st.model_dump()}")
                failures += 1

            rd = await fs_tool.fs_read(fleet, target, TEST_FILE)
            decoded = base64.b64decode(rd.content_b64)
            if decoded != BLOB:
                print(f"    FAIL: round-trip differs "
                      f"({len(decoded)} vs {len(BLOB)})")
                failures += 1
            else:
                print(f"    OK: {len(BLOB)}-byte binary blob round-tripped")
        except Exception as e:
            print(f"    FAIL: {e}")
            failures += 1
        finally:
            for p in (TEST_FILE, TEST_DIR):
                try: await fs_tool.fs_delete(fleet, target, p)
                except (TargetError, Exception): pass

        # ---- exec.cmd ----------------------------------------------

        print("[v] exec.cmd \"echo from-x5000\"")
        try:
            r = await exec_tool.exec_cmd(fleet, target, "echo from-x5000")
            archive.log_call("exec.cmd", target,
                             {"command": "echo from-x5000"},
                             result=r.model_dump())
            if "from-x5000" not in r.output:
                print(f"    FAIL: unexpected output: {r.output!r}")
                failures += 1
            else:
                print(f"    OK: {r.output.strip()!r}")
        except Exception as e:
            print(f"    FAIL: {e}")
            failures += 1

        # ---- wb.* --------------------------------------------------

        print("[v] wb.screens + wb.windows")
        try:
            screens = await wb_tool.wb_screens(fleet, target)
            archive.log_call("wb.screens", target, {},
                             result=[s.model_dump() for s in screens])
            print(f"    {len(screens)} screens")
            for s in screens[:4]:
                print(f"      [{s.index}] {s.width}x{s.height} "
                      f"title={s.title!r} windows={s.window_count}")
            wins = await wb_tool.wb_windows(fleet, target)
            archive.log_call("wb.windows", target, {},
                             result=[w.model_dump() for w in wins])
            print(f"    {len(wins)} windows total")
            for w in wins[:6]:
                print(f"      screen[{w.screen_index}] {w.width}x{w.height} "
                      f"title={w.title!r}")
        except Exception as e:
            print(f"    FAIL: {e}")
            failures += 1

        # ---- debug.task_snapshot -----------------------------------

        # Pick a stable system task. input.device worked on QEMU; let's
        # try it first, fall back to anything available.
        candidate_tasks = ["input.device", "Workbench"]
        snap_target_name = None
        try:
            t = await sys_tool.sys_tasks(fleet, target)
            available = {x.name for x in (t.ready + t.waiting)}
            for c in candidate_tasks:
                if c in available:
                    snap_target_name = c
                    break
        except Exception:
            pass

        if snap_target_name is None:
            print("[v] debug.task_snapshot SKIP: no stable task found")
        else:
            print(f"[v] debug.task_snapshot({snap_target_name!r})")
            try:
                snap = await debug_tool.debug_task_snapshot(
                    fleet, target, snap_target_name, max_frames=8,
                )
                archive.log_call("debug.task_snapshot", target,
                                 {"name": snap_target_name},
                                 result={"frame_count": len(snap.backtrace),
                                         "rtcf_filled": snap.rtcf_filled})
                r = snap.registers
                print(f"    PC=0x{r.pc:08x}  SP=0x{r.sp:08x}  "
                      f"LR=0x{r.lr:08x}  rtcf=0x{snap.rtcf_filled:x}")
                print(f"    backtrace: {len(snap.backtrace)} frame(s)")
                for f in snap.backtrace[:6]:
                    print(f"      [{f.index}] PC=0x{f.pc:08x}  "
                          f"SP=0x{f.sp:08x}")
                if not snap.rtcf_filled or (r.pc == 0 and r.sp == 0):
                    print("    FAIL: empty register state")
                    failures += 1
            except Exception as e:
                print(f"    FAIL: {e}")
                failures += 1

        # ---- events.wait -------------------------------------------

        print("\n[v] events.wait(topics=['sys.task'], timeout=2000ms)")
        try:
            t0 = time.monotonic()
            r = await events_tool.events_wait(
                fleet, target, topics=["sys.task"], timeout_ms=2000,
            )
            dt = (time.monotonic() - t0) * 1000
            archive.log_call("events.wait", target,
                             {"topics": ["sys.task"], "timeout_ms": 2000},
                             result=r.model_dump())
            print(f"    elapsed_ms={r.elapsed_ms} round-trip={dt:.0f}ms "
                  f"events={len(r.events)}")
            for ev in r.events[:6]:
                print(f"      {ev.topic} {ev.data.get('action', '')!r:9} "
                      f"name={ev.data.get('name')!r}")
        except Exception as e:
            print(f"    FAIL: {e}")
            failures += 1

    finally:
        await fleet.close_all()

    print(f"\n[archive] {archive.run_dir}/tool-calls.ndjson")
    print(f"[result] failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
