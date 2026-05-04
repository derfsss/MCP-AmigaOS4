"""Comprehensive capability sweep against a single MCPd target.

Goes wider than the per-round scripts: every advertised method is
exercised at least once, edge cases are probed, the demuxer is stress-
tested, and the host MCP protocol layer is sanity-checked.

Sections:

  1. Connectivity              proto.version + proto.capabilities envelope
  2. Filesystem                full fs.* round-trip incl. binary integrity
  3. Execution                 exec.cmd basic / args / cwd
  4. System introspection      every sys.* method
  5. Workbench                 every wb.* method
  6. Debug                     IDebug-driven helpers
  7. Events                    long-poll + server-push demux
  8. Application library       sys.applications + app.notify + self-registration
  9. Edge cases                error paths, boundary inputs
 10. Stress                    rapid round-trips, large payload, concurrent subs
 11. Host MCP layer            --list-tools, --list-resources

Usage:
    python scripts/validate_full.py [--endpoint host:port]
                                    [--skip-stress] [--skip-mcp]

Exit code = number of failed assertions (0 on full pass).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import os
import socket
import struct
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOST_SRC = HERE.parent / "host" / "src"
sys.path.insert(0, str(HOST_SRC))

from amiga_fleet_mcp.config import (  # noqa: E402
    Config,
    McpdChannel,
    PathsConfig,
    ServerConfig,
    TargetChannels,
    TargetConfig,
)
from amiga_fleet_mcp.fleet import Fleet  # noqa: E402
from amiga_fleet_mcp.tools import debug as debug_tool  # noqa: E402
from amiga_fleet_mcp.tools import events as events_tool  # noqa: E402
from amiga_fleet_mcp.tools import exec as exec_tool  # noqa: E402
from amiga_fleet_mcp.tools import fs as fs_tool  # noqa: E402
from amiga_fleet_mcp.tools import sys as sys_tool  # noqa: E402
from amiga_fleet_mcp.tools import wb as wb_tool  # noqa: E402


# --------------------------------------------------------------------
# Test infrastructure
# --------------------------------------------------------------------

class Reporter:
    """Tiny test reporter; section-aware, counts fails per section."""

    def __init__(self) -> None:
        self.section_name = "<no section>"
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self._section_passed = 0
        self._section_failed = 0

    def section(self, name: str) -> None:
        self._flush_section()
        self.section_name = name
        self._section_passed = 0
        self._section_failed = 0
        print(f"\n=== {name} " + "=" * max(0, 60 - len(name)))

    def _flush_section(self) -> None:
        if self._section_passed or self._section_failed:
            print(f"    -> {self._section_passed} passed, "
                  f"{self._section_failed} failed")

    def ok(self, label: str, detail: str = "") -> None:
        self.passed += 1
        self._section_passed += 1
        suffix = f" ({detail})" if detail else ""
        print(f"  [PASS] {label}{suffix}")

    def fail(self, label: str, why: str) -> None:
        self.failed += 1
        self._section_failed += 1
        print(f"  [FAIL] {label}: {why}")

    def skip(self, label: str, why: str = "") -> None:
        self.skipped += 1
        suffix = f" ({why})" if why else ""
        print(f"  [SKIP] {label}{suffix}")

    def assert_(self, label: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.ok(label, detail)
        else:
            self.fail(label, detail or "assertion failed")

    def finalize(self) -> int:
        self._flush_section()
        print()
        print(f"[summary] passed={self.passed}  failed={self.failed}  "
              f"skipped={self.skipped}")
        return self.failed


def make_fleet(endpoint: str) -> Fleet:
    cfg = Config(
        server=ServerConfig(
            archive_root=HERE.parent / "tmp" / "validate-full-archive",
        ),
        paths=PathsConfig(),
        targets={
            "target": TargetConfig(
                type="remote",
                display_name="target",
                channels=TargetChannels(
                    mcpd=McpdChannel(endpoint=endpoint),
                ),
            ),
        },
    )
    return Fleet(cfg)


# --------------------------------------------------------------------
# Section runners
# --------------------------------------------------------------------

async def section_connectivity(r: Reporter, fleet: Fleet) -> None:
    r.section("1. Connectivity")
    t = fleet.mcpd("target")

    # Direct TCP probe (sanity)
    host, port = t._channel.host, t._channel.port  # noqa: SLF001
    try:
        with socket.create_connection((host, port), timeout=5):
            pass
        r.ok("TCP connect", f"{host}:{port}")
    except OSError as e:
        r.fail("TCP connect", str(e))
        return

    # proto.version
    pv = await t.request("proto.version")
    r.assert_("proto.version returns server", "server" in pv,
              f"server={pv.get('server')!r}")
    r.assert_("proto.version returns protocol", "protocol" in pv,
              f"protocol={pv.get('protocol')!r}")

    # proto.capabilities full envelope
    caps = await t.request("proto.capabilities")
    for key in ("server", "protocol", "build", "methods",
                "method_names", "features", "limits", "board"):
        r.assert_(f"proto.capabilities.{key}", key in caps)
    r.assert_("methods is non-empty list",
              isinstance(caps.get("methods"), list)
              and len(caps["methods"]) > 0,
              f"count={len(caps.get('methods', []))}")
    r.assert_("method_names contains proto.capabilities",
              "proto.capabilities" in caps.get("method_names", []))
    r.assert_("limits.max_payload_bytes is positive",
              caps.get("limits", {}).get("max_payload_bytes", 0) > 0)
    r.assert_("board.detected is set",
              bool(caps.get("board", {}).get("detected")),
              f"board={caps.get('board', {}).get('detected')!r}")


async def section_fs(r: Reporter, fleet: Fleet) -> None:
    r.section("2. Filesystem")
    ROOT = "RAM:vfull_test"

    # Clean any leftover from a previous aborted run
    with suppress(Exception):
        await fs_tool.fs_delete(fleet, "target", ROOT, recursive=True)

    # makedir + list
    await fs_tool.fs_makedir(fleet, "target", ROOT)
    entries = await fs_tool.fs_list(fleet, "target", ROOT)
    r.assert_("fs.makedir + fs.list (empty)", len(entries) == 0)

    # write + read round-trip with NULs and 0xFF bytes
    blob = bytes(range(256)) * 32        # 8 KiB, all byte values
    p1 = ROOT + "/blob.bin"
    await fs_tool.fs_write(
        fleet, "target", p1,
        base64.b64encode(blob).decode(),
    )
    rd = await fs_tool.fs_read(fleet, "target", p1)
    got = base64.b64decode(rd.content_b64)
    r.assert_("fs.write + fs.read round-trip (binary safe)",
              got == blob,
              f"size={len(got)} expect={len(blob)}")

    # stat
    st = await fs_tool.fs_stat(fleet, "target", p1)
    r.assert_("fs.stat type=file", st.type == "file")
    r.assert_("fs.stat size matches",
              st.size == len(blob),
              f"got={st.size} expect={len(blob)}")

    # read offset/length
    rd2 = await fs_tool.fs_read(
        fleet, "target", p1, offset=512, length=128,
    )
    expected = blob[512:512 + 128]
    got2 = base64.b64decode(rd2.content_b64)
    r.assert_("fs.read offset+length",
              got2 == expected and rd2.size == 128 and
              rd2.total_size == len(blob))

    # hash
    h = await fs_tool.fs_hash(fleet, "target", p1)
    expected_hash = hashlib.sha256(blob).hexdigest()
    r.assert_("fs.hash sha256",
              h.hash == expected_hash and h.size == len(blob))

    # copy
    p_copy = ROOT + "/blob_copy.bin"
    await fs_tool.fs_copy(fleet, "target", p1, p_copy)
    h_copy = await fs_tool.fs_hash(fleet, "target", p_copy)
    r.assert_("fs.copy preserves contents", h_copy.hash == expected_hash)

    # rename
    p_renamed = ROOT + "/blob_renamed.bin"
    await fs_tool.fs_rename(fleet, "target", p_copy, p_renamed)
    st_r = await fs_tool.fs_stat(fleet, "target", p_renamed)
    r.assert_("fs.rename moves the file",
              st_r.size == len(blob))

    # protect (clear all protection bits = bits=0 = full access)
    await fs_tool.fs_protect(fleet, "target", p_renamed, bits=0)
    r.ok("fs.protect (bits=0)")

    # recursive list
    sub = ROOT + "/sub"
    await fs_tool.fs_makedir(fleet, "target", sub)
    await fs_tool.fs_makedir(fleet, "target", sub + "/inner")
    leaf = sub + "/inner/leaf.txt"
    await fs_tool.fs_write(
        fleet, "target", leaf, base64.b64encode(b"leaf!").decode(),
    )
    deep = await fs_tool.fs_list(
        fleet, "target", ROOT, recursive=True, max_depth=8,
    )
    names = sorted(e.name for e in deep)
    r.assert_("fs.list recursive finds nested file",
              "sub/inner/leaf.txt" in names,
              f"entries={len(names)}")

    # recursive delete cleans everything
    await fs_tool.fs_delete(fleet, "target", ROOT, recursive=True)
    try:
        await fs_tool.fs_list(fleet, "target", ROOT)
        r.fail("fs.delete recursive", "tree still listable after delete")
    except Exception:
        r.ok("fs.delete recursive")


async def section_exec(r: Reporter, fleet: Fleet) -> None:
    r.section("3. Execution")

    # Basic: Echo prints its argument
    res = await exec_tool.exec_cmd(fleet, "target", "Echo hello")
    r.assert_("exec.cmd basic Echo",
              "hello" in res.output and res.exit_code == 0)

    # args[]: tricky string with spaces / quotes / asterisks
    tricky = 'one two * "three" end'
    res2 = await exec_tool.exec_cmd(
        fleet, "target", "Echo", args=[tricky],
    )
    r.assert_("exec.cmd args[] (quoting round-trip)",
              tricky in res2.output and res2.exit_code == 0)

    # cwd: Cd with no args prints current; with cwd=RAM: it should mention RAM
    res3 = await exec_tool.exec_cmd(
        fleet, "target", "Cd", cwd="RAM:",
    )
    r.assert_("exec.cmd cwd swap",
              ("RAM" in res3.output or "Ram" in res3.output)
              and res3.exit_code == 0,
              f"out={res3.output.strip()[:60]!r}")


async def section_sys(r: Reporter, fleet: Fleet) -> None:
    r.section("4. System introspection")

    v = await sys_tool.sys_version(fleet, "target")
    r.assert_("sys.version returns kickstart",
              v.kickstart is not None and v.kickstart != "")

    u = await sys_tool.sys_uptime(fleet, "target")
    r.assert_("sys.uptime > 0", u.seconds > 0,
              f"{u.seconds:.0f}s")

    m = await sys_tool.sys_memory(fleet, "target")
    r.assert_("sys.memory.any.free > 0", m.any.free > 0,
              f"any.free={m.any.free}")

    vols = await sys_tool.sys_volumes(fleet, "target")
    r.assert_("sys.volumes returns >= 1 mounted volume", len(vols) >= 1)

    assigns = await sys_tool.sys_assigns(fleet, "target")
    r.assert_("sys.assigns returns >= 5 assigns", len(assigns) >= 5,
              f"count={len(assigns)}")

    tasks = await sys_tool.sys_tasks(fleet, "target")
    r.assert_("sys.tasks returns ready+waiting",
              (len(tasks.ready) + len(tasks.waiting)) > 0,
              f"ready={len(tasks.ready)} wait={len(tasks.waiting)}")

    libs = await sys_tool.sys_libraries(fleet, "target")
    r.assert_("sys.libraries returns >= 5 libs", len(libs) >= 5)

    devs = await sys_tool.sys_devices(fleet, "target")
    r.assert_("sys.devices returns >= 1 device", len(devs) >= 1)

    ports = await sys_tool.sys_ports(fleet, "target")
    r.assert_("sys.ports returns >= 1 port", len(ports) >= 1)

    al = await sys_tool.sys_lastalert(fleet, "target")
    r.assert_("sys.lastalert returns alert_code",
              al.alert_code is not None)

    # alert_decode on a known code
    ad = await sys_tool.sys_alert_decode(fleet, "target", 0x81000005)
    r.assert_("sys.alert_decode AN_MemCorrupt",
              ad.decoded.name == "AN_MemCorrupt"
              and ad.decoded.dead_end is True,
              f"name={ad.decoded.name!r}")

    hw = await sys_tool.sys_hardware(fleet, "target")
    r.assert_("sys.hardware returns CPU family",
              hw.cpu.family != "" and hw.cpu.family != "unknown",
              f"family={hw.cpu.family} model={hw.cpu.model}")
    r.assert_("sys.hardware speed > 0", hw.cpu.speed_hz > 0,
              f"{hw.cpu.speed_hz} Hz")
    r.assert_("sys.hardware cache line > 0", hw.cpu.cache_line > 0)

    i2c = await sys_tool.sys_hardware_i2c(fleet, "target")
    r.ok("sys.hardware.i2c reachable",
         f"available={i2c.available} buses={len(i2c.buses)}")

    pm = await sys_tool.sys_hardware_perfcounters(fleet, "target")
    r.ok("sys.hardware.perfcounters reachable",
         f"available={pm.available} counters={pm.counter_count}")

    # ELF symbol table on the running daemon
    es = await sys_tool.sys_executable_symbols(
        fleet, "target", "SYS:System/MCPd/MCPd",
        type="func", max=64,
    )
    r.assert_("sys.executable.symbols extracts function symbols",
              es.symbol_count > 0,
              f"count={es.symbol_count} sections={es.num_sections}")

    apps = await sys_tool.sys_applications(fleet, "target")
    r.assert_("sys.applications reachable",
              apps.available is True,
              f"count={len(apps.applications)}")
    mcpd_in_apps = any(
        a.name and "MCP" in a.name for a in apps.applications
    )
    if apps.available:
        r.assert_("sys.applications lists MCPd self-registration",
                  mcpd_in_apps)


async def section_wb(r: Reporter, fleet: Fleet) -> None:
    r.section("5. Workbench")

    s = await wb_tool.wb_screens(fleet, "target")
    r.assert_("wb.screens returns >= 1 screen", len(s) >= 1)

    w = await wb_tool.wb_windows(fleet, "target")
    r.ok("wb.windows reachable", f"count={len(w)}")

    ps = await wb_tool.wb_publicscreens(fleet, "target")
    r.assert_("wb.publicscreens returns >= 1 (Workbench)",
              len(ps) >= 1)

    fm = await wb_tool.wb_frontmost(fleet, "target")
    r.assert_("wb.frontmost returns frontmost_screen",
              fm.frontmost_screen is not None)


async def section_debug(r: Reporter, fleet: Fleet) -> None:
    r.section("6. Debug")

    # task_snapshot on a stable system task
    snap = await debug_tool.debug_task_snapshot(
        fleet, "target", "input.device", max_frames=4,
    )
    r.assert_("debug.task_snapshot input.device returns regs",
              isinstance(snap.registers.pc, int))

    # symbol resolution (may not resolve in shipped binaries; just check call)
    sym = await debug_tool.debug_symbol(
        fleet, "target", snap.registers.pc,
    )
    r.ok("debug.symbol(pc) returned",
         f"resolved={sym.resolved}")

    # stacktrace on input.device
    st = await debug_tool.debug_stacktrace(
        fleet, "target", "input.device", max_frames=8,
    )
    r.ok("debug.stacktrace input.device",
         f"frames={st.frame_count} rc={st.stacktrace_rc}")

    # write_register: read xer, write the same value back, confirm wtcf
    snap2 = await debug_tool.debug_task_snapshot(
        fleet, "target", "input.device", max_frames=1,
    )
    wr = await debug_tool.debug_write_register(
        fleet, "target", "input.device", "xer", snap2.registers.xer,
    )
    r.assert_("debug.write_register no-op write",
              wr.register_name == "xer"
              and wr.value == snap2.registers.xer)

    # write_memory boundary checks
    wm0 = await debug_tool.debug_write_memory(
        fleet, "target", 0,
        base64.b64encode(b"").decode(),
    )
    r.assert_("debug.write_memory n=0 short-circuits",
              wm0.bytes_written == 0)

    try:
        await debug_tool.debug_write_memory(
            fleet, "target", 0,
            base64.b64encode(b"\xde\xad").decode(),
        )
        r.fail("debug.write_memory NULL+data",
               "should reject")
    except Exception:
        r.ok("debug.write_memory NULL+data correctly rejected")


async def section_events(r: Reporter, fleet: Fleet) -> None:
    r.section("7. Events")
    t = fleet.mcpd("target")

    # Long-poll timeout path
    ew = await events_tool.events_wait(
        fleet, "target", topics=["sys.task"], timeout_ms=1000,
    )
    r.assert_("events.wait timeout returns []",
              ew.elapsed_ms >= 900,
              f"elapsed={ew.elapsed_ms} events={len(ew.events)}")

    # Subscribe / unsubscribe protocol
    sub = await t.request(
        "events.subscribe",
        {"topics": ["sys.lastalert", "debug.exception"]},
    )
    r.assert_("events.subscribe sets mask",
              sub.get("topics_mask") == 0x03)

    unsub = await t.request("events.unsubscribe")
    r.assert_("events.unsubscribe ok", unsub.get("ok") is True)

    # Server-push end-to-end via test_emit
    seen: list[dict] = []
    rm = t.subscribe_notifications(lambda o: seen.append(o))
    try:
        await t.request(
            "events.test_emit",
            {"topic": "validate.full",
             "data": {"hello": "world", "n": 99}},
        )
        # The notification fires before the next response. Force a
        # round-trip to drive the read.
        await t.request("proto.version")
        # Brief poll to let the handler land (synchronous, but be
        # forgiving on slow targets).
        for _ in range(20):
            if seen:
                break
            await asyncio.sleep(0.05)
        if not seen:
            r.fail("events.test_emit + notification dispatch",
                   "no notification observed")
        else:
            ev = seen[-1]
            ok = (
                ev.get("method") == "events.notify"
                and ev["params"]["topic"] == "validate.full"
                and ev["params"]["data"]["n"] == 99
            )
            r.assert_("events.test_emit + notification dispatch", ok)
    finally:
        rm()


async def section_applib(r: Reporter, fleet: Fleet) -> None:
    r.section("8. Application library")

    nf = await sys_tool.app_notify(
        fleet, "target",
        title="MCP-AmigaOS4 sweep",
        text="If this Ringhio popup is visible, app.notify works.",
        priority=0,
    )
    if nf.queued:
        r.ok("app.notify queued", f"rc={nf.result_code}")
    elif nf.result_code in (50, 120):
        r.skip("app.notify",
               f"Ringhio not running (rc={nf.result_code})")
    else:
        r.fail("app.notify", f"rc={nf.result_code}")

    apps = await sys_tool.sys_applications(fleet, "target")
    if not apps.available:
        r.skip("MCPd self-registration", "application.library missing")
    else:
        urls = [a.url_identifier for a in apps.applications
                if a.url_identifier]
        r.assert_("MCPd registered with com.derfsss.mcpd URL identifier",
                  "com.derfsss.mcpd" in urls,
                  f"urls={urls}")


async def section_edge(r: Reporter, fleet: Fleet) -> None:
    r.section("9. Edge cases")

    # fs.read offset > file size returns empty content + total_size
    p = "RAM:vfull_oversize.bin"
    body = b"hello world"
    await fs_tool.fs_write(
        fleet, "target", p, base64.b64encode(body).decode(),
    )
    rd = await fs_tool.fs_read(
        fleet, "target", p, offset=1000, length=10,
    )
    r.assert_("fs.read offset > size returns empty",
              rd.size == 0 and rd.total_size == len(body))
    await fs_tool.fs_delete(fleet, "target", p)

    # fs.list on non-existent path raises
    try:
        await fs_tool.fs_list(fleet, "target", "RAM:does_not_exist_zzz")
        r.fail("fs.list non-existent path", "should raise")
    except Exception:
        r.ok("fs.list non-existent path raises")

    # sys.alert_decode on 0xFFFFFFFF (no-alert sentinel)
    ad = await sys_tool.sys_alert_decode(fleet, "target", 0xFFFFFFFF)
    r.ok("sys.alert_decode 0xFFFFFFFF",
         f"name={ad.decoded.name!r} sub={ad.decoded.subsystem!r}")

    # Large fs.write/read (256 KiB)
    big = bytes((i * 7) & 0xFF for i in range(256 * 1024))
    pbig = "RAM:vfull_big.bin"
    await fs_tool.fs_write(
        fleet, "target", pbig, base64.b64encode(big).decode(),
    )
    rd_big = await fs_tool.fs_read(fleet, "target", pbig)
    r.assert_("fs.write/read 256 KiB round-trip",
              base64.b64decode(rd_big.content_b64) == big)
    h_big = await fs_tool.fs_hash(fleet, "target", pbig)
    r.assert_("fs.hash 256 KiB matches",
              h_big.hash == hashlib.sha256(big).hexdigest())
    await fs_tool.fs_delete(fleet, "target", pbig)


async def section_stress(r: Reporter, fleet: Fleet) -> None:
    r.section("10. Stress")
    t = fleet.mcpd("target")

    # 100 rapid round-trips
    t0 = time.monotonic()
    for _ in range(100):
        v = await t.request("proto.version")
        assert "server" in v
    elapsed = time.monotonic() - t0
    r.ok("100 rapid proto.version round-trips",
         f"{elapsed:.2f}s ({100 / elapsed:.0f} req/s)")

    # 50 round-trips while subscribed (frame-demuxer torture test)
    await t.request(
        "events.subscribe",
        {"topics": ["sys.lastalert", "debug.exception"]},
    )
    try:
        for _ in range(50):
            v = await t.request("proto.version")
            assert "server" in v
        r.ok("50 round-trips while subscribed (no demux corruption)")
    finally:
        await t.request("events.unsubscribe")


async def section_mcp_protocol(r: Reporter) -> None:
    r.section("11. Host MCP protocol layer")

    # The CLI flags --list-tools / --list-resources need a loadable
    # config so the server can register its tool surface. Build a
    # minimal temp config with no targets - the MCP introspection
    # path doesn't require any to exist.
    tmp_root = HERE.parent / "tmp" / "validate-full-mcpcli"
    tmp_root.mkdir(parents=True, exist_ok=True)
    tmp_cfg = tmp_root / "config.toml"
    tmp_cfg.write_text(
        f'[server]\n'
        f'archive_root = "{(tmp_root / "archive").as_posix()}"\n'
        f'mcp_transport = "stdio"\n'
    )

    base_cmd = [
        sys.executable, "-m", "amiga_fleet_mcp.server",
        "--config", str(tmp_cfg),
    ]
    env = {**os.environ, "PYTHONPATH": str(HOST_SRC)}

    out = subprocess.run(
        base_cmd + ["--list-tools"],
        cwd=str(HERE.parent / "host"),
        env=env,
        capture_output=True, text=True, timeout=30,
    )
    if out.returncode != 0:
        r.fail("--list-tools", f"rc={out.returncode}\n{out.stderr[-500:]}")
    else:
        # Tool descriptors are indented; banner and log lines are not.
        lines = [
            ln for ln in out.stdout.splitlines()
            if ln.startswith("  ") and "[" in ln
        ]
        r.assert_("--list-tools prints >= 40 tools",
                  len(lines) >= 40, f"lines={len(lines)}")

    out2 = subprocess.run(
        base_cmd + ["--list-resources"],
        cwd=str(HERE.parent / "host"),
        env=env,
        capture_output=True, text=True, timeout=30,
    )
    if out2.returncode != 0:
        r.fail("--list-resources",
               f"rc={out2.returncode}\n{out2.stderr[-500:]}")
    else:
        r.assert_("--list-resources mentions amiga://",
                  "amiga://" in out2.stdout)


# --------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------

async def amain(args: argparse.Namespace) -> int:
    r = Reporter()
    fleet = make_fleet(args.endpoint)
    try:
        await section_connectivity(r, fleet)
        await section_fs(r, fleet)
        await section_exec(r, fleet)
        await section_sys(r, fleet)
        await section_wb(r, fleet)
        await section_debug(r, fleet)
        await section_events(r, fleet)
        await section_applib(r, fleet)
        await section_edge(r, fleet)
        if not args.skip_stress:
            await section_stress(r, fleet)
        else:
            r.section("10. Stress (skipped)")
            r.skip("stress section", "--skip-stress")
        if not args.skip_mcp:
            await section_mcp_protocol(r)
        else:
            r.section("11. Host MCP protocol layer (skipped)")
            r.skip("mcp section", "--skip-mcp")
    finally:
        await fleet.close_all()
    return r.finalize()


def main() -> int:
    import os
    p = argparse.ArgumentParser()
    p.add_argument("--endpoint", default=os.environ.get("MCPD_ENDPOINT"),
                   help="MCPd endpoint (default $MCPD_ENDPOINT; required)")
    p.add_argument("--skip-stress", action="store_true")
    p.add_argument("--skip-mcp", action="store_true")
    args = p.parse_args()
    if not args.endpoint:
        p.error("--endpoint or $MCPD_ENDPOINT is required")
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
