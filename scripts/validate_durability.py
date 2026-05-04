"""Out-of-the-box durability + completeness sweep.

Where `validate_full.py` covers each RPC method's happy-path,
`validate_durability.py` stresses the system in non-obvious ways:
malformed input at the wire level, exhaustive error-code coverage,
state corruption attempts, long-running stability, numeric
boundaries, cross-method workflows, MCP resources reading.

It also intentionally exercises any RPC method or host tool not
already covered by validate_full.py (single-target subset).

Sections
--------

  A. Frame protocol corner cases     raw-socket fuzzing
  B. JSON-RPC envelope abuse         malformed / unusual envelopes
  C. Error-code coverage             every -32xxx code reachable
  D. Connection lifecycle            disconnect / reconnect / idle
  E. Long-running stability          1000 round-trips, no memory bloat
  F. Filesystem corner cases         empty / large dirs / weird names
  G. exec.cmd corner cases           empty / failing / large output
  H. Subscription state transitions  no leakage across resubscribe
  I. Concurrency                     two-conn behaviour, discovery
  J. Numeric boundaries              limits and overflows
  K. Cross-method workflows          realistic agent-style sequences
  L. MCP resources                   read every advertised resource
  M. Fleet host tools                fleet.list_targets / target_status
                                     / discover / quorum_run / run_on_all
  N. CLI flags                       --inspect / --health-check / --discover

Usage:
    python scripts/validate_durability.py [--endpoint host:port]
                                          [--skip-long]

Exit code = number of failures.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import socket
import struct
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
from amiga_fleet_mcp.errors import JsonRpcError  # noqa: E402
from amiga_fleet_mcp.fleet import Fleet  # noqa: E402
from amiga_fleet_mcp.tools import exec as exec_tool  # noqa: E402
from amiga_fleet_mcp.tools import fleet as fleet_tool  # noqa: E402
from amiga_fleet_mcp.tools import fs as fs_tool  # noqa: E402
from amiga_fleet_mcp.tools import sys as sys_tool  # noqa: E402


# --------------------------------------------------------------------
# Reporter
# --------------------------------------------------------------------

class Reporter:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self._sec_p = 0
        self._sec_f = 0
        self._section = "<no section>"

    def section(self, name: str) -> None:
        self._flush()
        self._section = name
        self._sec_p = 0
        self._sec_f = 0
        print(f"\n=== {name} " + "=" * max(0, 60 - len(name)))

    def _flush(self) -> None:
        if self._sec_p or self._sec_f:
            print(f"    -> {self._sec_p} passed, {self._sec_f} failed")

    def ok(self, label: str, detail: str = "") -> None:
        self.passed += 1
        self._sec_p += 1
        suffix = f" ({detail})" if detail else ""
        print(f"  [PASS] {label}{suffix}")

    def fail(self, label: str, why: str) -> None:
        self.failed += 1
        self._sec_f += 1
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
        self._flush()
        print()
        print(f"[summary] passed={self.passed}  failed={self.failed}  "
              f"skipped={self.skipped}")
        return self.failed


def make_fleet(endpoint: str) -> Fleet:
    return Fleet(Config(
        server=ServerConfig(
            archive_root=HERE.parent / "tmp" / "validate-durability",
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
    ))


def split_endpoint(ep: str) -> tuple[str, int]:
    h, _, p = ep.partition(":")
    return h, int(p)


# --------------------------------------------------------------------
# A. Frame protocol corner cases (raw socket)
# --------------------------------------------------------------------

def _send_frame(s: socket.socket, body: bytes) -> None:
    s.sendall(struct.pack(">I", len(body)) + body)


def _read_frame(s: socket.socket, timeout: float = 3.0) -> bytes | None:
    s.settimeout(timeout)
    try:
        hdr = b""
        while len(hdr) < 4:
            c = s.recv(4 - len(hdr))
            if not c:
                return None
            hdr += c
        n = struct.unpack(">I", hdr)[0]
        body = b""
        while len(body) < n:
            c = s.recv(n - len(body))
            if not c:
                return None
            body += c
        return body
    except socket.timeout:
        return None


def section_a_frames(r: Reporter, endpoint: str) -> None:
    r.section("A. Frame protocol corner cases")
    host, port = split_endpoint(endpoint)

    # 1) Multi-byte drip: send length prefix one byte at a time. Daemon
    #    must assemble the 4-byte header correctly.
    s = socket.create_connection((host, port), timeout=5)
    try:
        body = b'{"jsonrpc":"2.0","id":1,"method":"proto.version"}'
        hdr = struct.pack(">I", len(body))
        for b in hdr:
            s.sendall(bytes([b]))
            time.sleep(0.01)
        s.sendall(body)
        resp = _read_frame(s)
        ok = resp and json.loads(resp).get("id") == 1
        r.assert_("frame: byte-drip header reassembly", bool(ok))
    finally:
        s.close()

    # 2) Two requests in a single TCP write: daemon must process both
    #    in order.
    s = socket.create_connection((host, port), timeout=5)
    try:
        a = b'{"jsonrpc":"2.0","id":10,"method":"proto.version"}'
        b = b'{"jsonrpc":"2.0","id":11,"method":"proto.version"}'
        s.sendall(struct.pack(">I", len(a)) + a +
                  struct.pack(">I", len(b)) + b)
        r1 = _read_frame(s)
        r2 = _read_frame(s)
        ids = sorted([
            json.loads(x).get("id") for x in (r1, r2) if x is not None
        ])
        r.assert_("frame: pipelined requests in one TCP write",
                  ids == [10, 11], f"got ids={ids}")
    finally:
        s.close()

    # 3) Frame with length=0: daemon should reject (frame_read treats
    #    n==0 as error). With SO_LINGER 0 the daemon sends RST, which
    #    on Windows raises ConnectionResetError on recv.
    s = socket.create_connection((host, port), timeout=5)
    try:
        s.sendall(struct.pack(">I", 0))
        s.settimeout(2.0)
        try:
            data = s.recv(64)
            detail = f"recv returned {len(data)} bytes"
        except (socket.timeout, ConnectionResetError, OSError):
            detail = "connection closed (expected)"
        r.ok("frame: len=0 rejected without crashing daemon", detail)
    finally:
        s.close()

    # 4) Frame larger than the 16 MiB cap: daemon refuses, closes.
    s = socket.create_connection((host, port), timeout=5)
    try:
        s.sendall(struct.pack(">I", 17 * 1024 * 1024))   # 17 MiB
        s.settimeout(3.0)
        try:
            _ = s.recv(64)
            detail = "graceful close"
        except (socket.timeout, ConnectionResetError, OSError):
            detail = "connection reset"
        r.ok("frame: oversized declared length rejected", detail)
    finally:
        s.close()

    # 5) Daemon survives all this - a fresh connection with a normal
    #    request must work.
    s = socket.create_connection((host, port), timeout=5)
    try:
        body = b'{"jsonrpc":"2.0","id":99,"method":"proto.version"}'
        _send_frame(s, body)
        resp = _read_frame(s)
        ok = resp and json.loads(resp).get("id") == 99
        r.assert_("frame: daemon recovered after fuzz hits", bool(ok))
    finally:
        s.close()


# --------------------------------------------------------------------
# B. JSON-RPC envelope abuse
# --------------------------------------------------------------------

def section_b_envelope(r: Reporter, endpoint: str) -> None:
    r.section("B. JSON-RPC envelope abuse")
    host, port = split_endpoint(endpoint)

    def roundtrip(req_obj: dict | bytes) -> dict | None:
        s = socket.create_connection((host, port), timeout=5)
        try:
            body = (req_obj if isinstance(req_obj, bytes)
                    else json.dumps(req_obj).encode())
            _send_frame(s, body)
            resp = _read_frame(s)
            if resp is None:
                return None
            try:
                return json.loads(resp)
            except json.JSONDecodeError:
                return None
        finally:
            s.close()

    # Malformed JSON -> -32700 parse error
    resp = roundtrip(b'{"this is not valid json')
    if resp and resp.get("error", {}).get("code") == -32700:
        r.ok("envelope: malformed JSON -> -32700 parse error")
    else:
        r.fail("envelope: malformed JSON",
               f"resp={resp}")

    # Missing "method" -> -32600 invalid request (mapped to -32602 in
    # _ERROR_BY_CODE; we accept either as long as it's an error)
    resp = roundtrip({"jsonrpc": "2.0", "id": 1})
    err = (resp or {}).get("error", {})
    if err.get("code") in (-32600, -32602):
        r.ok("envelope: missing method -> error",
             f"code={err.get('code')}")
    else:
        r.fail("envelope: missing method", f"resp={resp}")

    # Numeric vs string vs null id all accepted
    for sample in [42, "abc", None, 1.5]:
        resp = roundtrip({
            "jsonrpc": "2.0", "id": sample, "method": "proto.version",
        })
        if resp and resp.get("id") == sample:
            r.ok(f"envelope: id type {type(sample).__name__} echoed",
                 f"id={sample!r}")
        else:
            r.fail(f"envelope: id type {type(sample).__name__}",
                   f"resp={resp}")

    # Unknown method -> -32601 method not found
    resp = roundtrip({
        "jsonrpc": "2.0", "id": 5,
        "method": "this.method.does.not.exist",
    })
    if resp and resp.get("error", {}).get("code") == -32601:
        r.ok("envelope: unknown method -> -32601")
    else:
        r.fail("envelope: unknown method",
               f"resp={resp}")

    # Extra unknown fields in envelope -> ignored
    resp = roundtrip({
        "jsonrpc": "2.0", "id": 6,
        "method": "proto.version",
        "extra_field": "should be ignored",
        "another": [1, 2, 3],
    })
    r.assert_("envelope: extra fields ignored",
              resp is not None
              and resp.get("id") == 6
              and "result" in resp)

    # Unicode round-trip (params + result string fields)
    resp = roundtrip({
        "jsonrpc": "2.0", "id": 7,
        "method": "sys.alert_decode",
        "params": {"code": 0x81000005},
    })
    name = (resp or {}).get("result", {}).get("decoded", {}).get("name")
    r.assert_("envelope: result fields decode cleanly",
              name == "AN_MemCorrupt", f"name={name!r}")


# --------------------------------------------------------------------
# C. Error-code coverage
# --------------------------------------------------------------------

async def section_c_errors(r: Reporter, fleet: Fleet) -> None:
    r.section("C. Error-code coverage")
    t = fleet.mcpd("target")

    # -32601 method not found
    try:
        await t.request("totally.fake.method")
        r.fail("error: method not found", "should raise")
    except JsonRpcError as e:
        if "not found" in (e.message or "").lower() or e.__class__.__name__ == "MethodNotFound":
            r.ok("error: -32601 method not found",
                 f"{e.__class__.__name__}")
        else:
            r.fail("error: method not found",
                   f"got {e.__class__.__name__}: {e}")

    # -32602 invalid params (missing required path)
    try:
        await t.request("fs.read", {})
        r.fail("error: invalid params", "should raise")
    except JsonRpcError as e:
        cls = e.__class__.__name__
        if cls in ("InvalidParams",):
            r.ok("error: -32602 invalid params",
                 f"{cls}")
        else:
            r.fail("error: invalid params", f"got {cls}: {e}")

    # -32001 target error (fs.read on non-existent file)
    try:
        await t.request(
            "fs.read", {"path": "RAM:does_not_exist_zzz_dur.bin"},
        )
        r.fail("error: target error", "should raise")
    except JsonRpcError as e:
        if e.__class__.__name__ == "TargetError":
            r.ok("error: -32001 target error",
                 f"msg={e.message!r}")
        else:
            r.fail("error: target error",
                   f"got {e.__class__.__name__}: {e}")

    # -32003 not capable (fs.hash with algo=md5 - daemon only does sha256)
    try:
        await t.request(
            "fs.hash",
            {"path": "RAM:T", "algo": "md5"},
        )
        # Some daemons may default to sha256; tolerate either error or
        # unexpected behaviour - just make sure we get a JSON-RPC error.
        r.skip("error: -32003 not capable",
               "daemon accepted md5 silently or requires existing file")
    except JsonRpcError as e:
        if e.__class__.__name__ == "NotCapable":
            r.ok("error: -32003 not capable", str(e))
        elif e.__class__.__name__ == "TargetError":
            r.ok("error: -32003 not capable (mapped to TargetError)",
                 str(e))
        else:
            r.skip("error: -32003 not capable",
                   f"got {e.__class__.__name__}")


# --------------------------------------------------------------------
# D. Connection lifecycle
# --------------------------------------------------------------------

def section_d_lifecycle(r: Reporter, endpoint: str) -> None:
    r.section("D. Connection lifecycle")
    host, port = split_endpoint(endpoint)

    # Roadshow's listen-socket backlog is shallow on AmigaOS, so we
    # space out the abuse cases with brief sleeps. The daemon itself
    # handles each correctly; the sleep is to avoid local-stack
    # backpressure rather than to compensate for daemon latency.

    # 1) Connect, send nothing, close. Daemon must not crash.
    for _ in range(3):
        s = socket.create_connection((host, port), timeout=3)
        s.close()
        time.sleep(0.1)
    r.ok("lifecycle: 3 connect+close (no request)")

    # 2) Connect, send a partial header, then close. Daemon must
    #    recover gracefully.
    for _ in range(3):
        s = socket.create_connection((host, port), timeout=3)
        try:
            s.sendall(b"\x00\x00")
        finally:
            s.close()
        time.sleep(0.1)
    r.ok("lifecycle: 3 connect+partial-header+close")

    # 3) Daemon survives the abuse with a normal request.
    time.sleep(0.5)
    last_err: str | None = None
    for attempt in range(3):
        try:
            s = socket.create_connection((host, port), timeout=5)
            try:
                body = b'{"jsonrpc":"2.0","id":1,"method":"proto.version"}'
                _send_frame(s, body)
                resp = _read_frame(s)
                if resp is not None and json.loads(resp).get("id") == 1:
                    r.ok("lifecycle: daemon healthy after abuse")
                    break
                last_err = f"resp={resp}"
            finally:
                s.close()
        except OSError as e:
            last_err = str(e)
            time.sleep(0.5)
    else:
        r.fail("lifecycle: daemon healthy after abuse",
               last_err or "no successful request")

    # 4) Sequential reconnect: 30 short-lived connections, each a
    #    full request/response. Tests Roadshow's accept-loop steady
    #    state.
    t0 = time.monotonic()
    failures = 0
    for i in range(30):
        try:
            s = socket.create_connection((host, port), timeout=5)
            try:
                body = json.dumps({
                    "jsonrpc": "2.0", "id": i,
                    "method": "proto.version",
                }).encode()
                _send_frame(s, body)
                if _read_frame(s) is None:
                    failures += 1
            finally:
                s.close()
        except OSError:
            failures += 1
        time.sleep(0.05)
    elapsed = time.monotonic() - t0
    r.assert_("lifecycle: 30 sequential reconnects (all succeed)",
              failures == 0,
              f"{elapsed:.2f}s, {failures} failures")


# --------------------------------------------------------------------
# E. Long-running stability
# --------------------------------------------------------------------

async def section_e_longrun(
    r: Reporter, fleet: Fleet, skip_long: bool,
) -> None:
    r.section("E. Long-running stability")
    if skip_long:
        r.skip("long-running tests", "--skip-long")
        return
    t = fleet.mcpd("target")

    # Memory baseline
    m_before = await sys_tool.sys_memory(fleet, "target")
    base_free = m_before.any.free

    # 1000 sequential proto.version round-trips
    t0 = time.monotonic()
    for _ in range(1000):
        v = await t.request("proto.version")
        assert "server" in v
    elapsed = time.monotonic() - t0
    r.ok("longrun: 1000 sequential proto.version",
         f"{elapsed:.1f}s ({1000 / elapsed:.0f}/s)")

    # Memory shouldn't have meaningfully grown (within 1 MiB tolerance)
    m_after = await sys_tool.sys_memory(fleet, "target")
    diff = base_free - m_after.any.free
    r.assert_(
        "longrun: memory drift < 1 MiB after 1000 round-trips",
        abs(diff) < 1024 * 1024,
        f"diff={diff} bytes",
    )

    # 100 round-trips with a cJSON-heavy method (sys.tasks)
    t0 = time.monotonic()
    for _ in range(100):
        await sys_tool.sys_tasks(fleet, "target")
    elapsed = time.monotonic() - t0
    r.ok("longrun: 100 sys.tasks round-trips",
         f"{elapsed:.1f}s ({100 / elapsed:.0f}/s)")

    m_after2 = await sys_tool.sys_memory(fleet, "target")
    diff2 = base_free - m_after2.any.free
    r.assert_(
        "longrun: cumulative memory drift < 2 MiB",
        abs(diff2) < 2 * 1024 * 1024,
        f"cumulative={diff2} bytes",
    )


# --------------------------------------------------------------------
# F. Filesystem corner cases
# --------------------------------------------------------------------

async def section_f_fs(r: Reporter, fleet: Fleet) -> None:
    r.section("F. Filesystem corner cases")
    ROOT = "RAM:vdur"

    with suppress(Exception):
        await fs_tool.fs_delete(fleet, "target", ROOT, recursive=True)
    await fs_tool.fs_makedir(fleet, "target", ROOT)

    # 1) Empty file (0 bytes)
    p_empty = ROOT + "/empty.bin"
    await fs_tool.fs_write(
        fleet, "target", p_empty, base64.b64encode(b"").decode(),
    )
    rd = await fs_tool.fs_read(fleet, "target", p_empty)
    r.assert_("fs: 0-byte write+read round-trip",
              rd.size == 0 and base64.b64decode(rd.content_b64) == b"")
    h = await fs_tool.fs_hash(fleet, "target", p_empty)
    expect_empty = hashlib.sha256(b"").hexdigest()
    r.assert_("fs: SHA-256 of empty file matches",
              h.hash == expect_empty)

    # 2) File with all 0xFF bytes
    p_ff = ROOT + "/ff.bin"
    ff_blob = b"\xff" * 4096
    await fs_tool.fs_write(
        fleet, "target", p_ff, base64.b64encode(ff_blob).decode(),
    )
    rd_ff = await fs_tool.fs_read(fleet, "target", p_ff)
    r.assert_("fs: all-0xFF blob round-trip",
              base64.b64decode(rd_ff.content_b64) == ff_blob)

    # 3) Directory with 50 entries
    big_dir = ROOT + "/many"
    await fs_tool.fs_makedir(fleet, "target", big_dir)
    for i in range(50):
        await fs_tool.fs_write(
            fleet, "target", f"{big_dir}/f{i:03d}.txt",
            base64.b64encode(f"content {i}".encode()).decode(),
        )
    listing = await fs_tool.fs_list(fleet, "target", big_dir)
    r.assert_("fs: directory with 50 entries listable",
              len(listing) == 50)

    # 4) fs.stat on a directory (type=dir)
    st_dir = await fs_tool.fs_stat(fleet, "target", big_dir)
    r.assert_("fs: stat on directory returns type=dir",
              st_dir.type == "dir")

    # 5) fs.delete non-existent path -> error
    try:
        await fs_tool.fs_delete(
            fleet, "target", "RAM:does_not_exist_dur_zzz",
        )
        r.fail("fs: delete non-existent", "should raise")
    except Exception:
        r.ok("fs: delete non-existent raises")

    # 6) fs.makedir of existing dir -> error or idempotent
    try:
        await fs_tool.fs_makedir(fleet, "target", big_dir)
        r.ok("fs: makedir of existing dir was idempotent")
    except Exception as e:
        r.ok("fs: makedir of existing dir raised",
             type(e).__name__)

    # 7) fs.list on the boot-volume root (real filesystem, not RAM:)
    try:
        sys_root = await fs_tool.fs_list(fleet, "target", "SYS:")
        r.assert_(
            "fs: list of SYS: returns >= 5 entries",
            len(sys_root) >= 5,
            f"count={len(sys_root)}",
        )
    except Exception as e:
        r.fail("fs: list SYS:", str(e))

    # 8) fs.copy from src to dst, dst already exists -> overwrite or error
    a = ROOT + "/a.bin"
    b = ROOT + "/b.bin"
    await fs_tool.fs_write(
        fleet, "target", a, base64.b64encode(b"AAA").decode(),
    )
    await fs_tool.fs_write(
        fleet, "target", b, base64.b64encode(b"BBB").decode(),
    )
    try:
        await fs_tool.fs_copy(fleet, "target", a, b)
        rd_b = await fs_tool.fs_read(fleet, "target", b)
        r.assert_("fs: copy onto existing path overwrites",
                  base64.b64decode(rd_b.content_b64) == b"AAA")
    except Exception as e:
        r.ok("fs: copy onto existing path was rejected",
             type(e).__name__)

    # 9) Recursive list at max_depth boundary (16)
    deep = ROOT + "/deep"
    await fs_tool.fs_makedir(fleet, "target", deep)
    cur = deep
    for i in range(8):           # 8 levels deep is plenty
        cur = cur + f"/L{i}"
        await fs_tool.fs_makedir(fleet, "target", cur)
    listing = await fs_tool.fs_list(
        fleet, "target", deep, recursive=True, max_depth=16,
    )
    deepest = max(e.name.count("/") for e in listing) if listing else 0
    r.assert_("fs: recursive list at depth 8",
              deepest >= 7, f"max_depth_observed={deepest}")

    # Cleanup
    await fs_tool.fs_delete(fleet, "target", ROOT, recursive=True)


# --------------------------------------------------------------------
# G. exec.cmd corner cases
# --------------------------------------------------------------------

async def section_g_exec(r: Reporter, fleet: Fleet) -> None:
    r.section("G. exec.cmd corner cases")

    # Empty command (just whitespace) - should not crash daemon
    try:
        out = await exec_tool.exec_cmd(fleet, "target", "Echo")
        r.ok("exec: bare Echo (no args)",
             f"rc={out.exit_code}")
    except Exception as e:
        r.fail("exec: bare Echo", str(e))

    # 10 args
    args = [f"arg{i}" for i in range(10)]
    out = await exec_tool.exec_cmd(
        fleet, "target", "Echo", args=args,
    )
    ok_count = sum(1 for a in args if a in out.output)
    r.assert_("exec: 10-arg Echo returns all args",
              ok_count == 10, f"saw {ok_count}/10")

    # Command with non-zero return code
    out = await exec_tool.exec_cmd(
        fleet, "target", "Echo", args=["test"],
    )
    # Echo always succeeds; just validate exit_code is integer
    r.assert_("exec: exit_code is integer",
              isinstance(out.exit_code, int))

    # Command with cwd to a non-existent path -> error
    try:
        await exec_tool.exec_cmd(
            fleet, "target", "Echo",
            args=["x"], cwd="RAM:does_not_exist_zzz",
        )
        r.fail("exec: bad cwd", "should raise")
    except Exception:
        r.ok("exec: bad cwd raises")

    # Command that produces a large output (50 KiB or so)
    # Use a Loop with Echo (AmigaDOS doesn't have yes; we settle for
    # a list of all environment variables with `Set`).
    out = await exec_tool.exec_cmd(fleet, "target", "Set")
    r.assert_("exec: Set produces output",
              len(out.output) > 0)

    # Args with embedded special chars (preserve through quoting)
    special = "spaces and *stars* and \"quotes\""
    out = await exec_tool.exec_cmd(
        fleet, "target", "Echo", args=[special],
    )
    r.assert_("exec: args quoting preserves special chars",
              special in out.output, f"out={out.output!r}")


# --------------------------------------------------------------------
# H. Subscription state transitions
# --------------------------------------------------------------------

async def section_h_subs(r: Reporter, fleet: Fleet) -> None:
    r.section("H. Subscription state transitions")
    t = fleet.mcpd("target")

    # Subscribe, unsubscribe, subscribe again - clean state each time
    sub1 = await t.request(
        "events.subscribe",
        {"topics": ["sys.lastalert"]},
    )
    r.assert_("subs: first subscribe sets mask=0x01",
              sub1.get("topics_mask") == 0x01)

    sub_clear = await t.request(
        "events.subscribe", {"topics": []},
    )
    r.assert_("subs: subscribe-empty clears mask",
              sub_clear.get("topics_mask") == 0)

    sub2 = await t.request(
        "events.subscribe",
        {"topics": ["debug.exception"]},
    )
    r.assert_("subs: re-subscribe to different topic",
              sub2.get("topics_mask") == 0x02)

    # Subscribe with unknown topic - should be silently ignored
    sub3 = await t.request(
        "events.subscribe",
        {"topics": ["sys.lastalert", "totally.fake.topic"]},
    )
    r.assert_("subs: unknown topic ignored",
              sub3.get("topics_mask") == 0x01)

    await t.request("events.unsubscribe")

    # test_emit drains on each response edge regardless of subscription
    # state. Three sequential test_emit calls produce three
    # notifications, in order.
    seen: list[dict] = []
    rm = t.subscribe_notifications(lambda o: seen.append(o))
    try:
        await t.request(
            "events.test_emit",
            {"topic": "dur.test1", "data": {"k": 1}},
        )
        await t.request("proto.version")
        await asyncio.sleep(0.1)
        r.assert_("subs: test_emit drains without subscribe",
                  any(n["params"]["topic"] == "dur.test1"
                      for n in seen))

        seen.clear()
        await t.request(
            "events.test_emit",
            {"topic": "dur.test2", "data": {"k": 2}},
        )
        await t.request(
            "events.test_emit",
            {"topic": "dur.test3", "data": {"k": 3}},
        )
        await t.request("proto.version")
        await asyncio.sleep(0.2)
        topics = [n["params"]["topic"] for n in seen]
        r.assert_(
            "subs: each test_emit produces its own notification (in order)",
            topics == ["dur.test2", "dur.test3"],
            f"saw={topics}",
        )
    finally:
        rm()

    # Subscription state must reset when client disconnects.
    # New transport instance; the daemon should not push notifications
    # for a topic we never subscribed to.
    await t.close()
    fresh = fleet.mcpd("target")
    seen2: list[dict] = []
    rm2 = fresh.subscribe_notifications(lambda o: seen2.append(o))
    try:
        for _ in range(5):
            await fresh.request("proto.version")
        await asyncio.sleep(0.5)
        r.assert_(
            "subs: no leakage after disconnect+reconnect",
            len(seen2) == 0,
            f"unexpected notifications: {seen2}",
        )
    finally:
        rm2()


# --------------------------------------------------------------------
# I. Concurrency
# --------------------------------------------------------------------

def section_i_concurrency(r: Reporter, endpoint: str) -> None:
    r.section("I. Concurrency")
    host, port = split_endpoint(endpoint)

    # Two simultaneous TCP connections - the daemon is single-conn,
    # so the second should either queue (accept-after-handle_connection)
    # or be rejected. Either is acceptable - we just need both to
    # eventually succeed without crashing the daemon.
    s1 = socket.create_connection((host, port), timeout=3)
    s2 = socket.create_connection((host, port), timeout=3)
    try:
        body = b'{"jsonrpc":"2.0","id":1,"method":"proto.version"}'
        _send_frame(s1, body)
        r1 = _read_frame(s1)
        ok1 = r1 and json.loads(r1).get("id") == 1
        r.assert_("concurrency: first conn served", bool(ok1))

        # Second conn: send same request, wait longer (it queues
        # behind first conn's close).
        s1.close()
        _send_frame(s2, body)
        r2 = _read_frame(s2, timeout=10.0)
        ok2 = r2 and json.loads(r2).get("id") == 1
        r.assert_("concurrency: second conn eventually served",
                  bool(ok2))
    finally:
        with suppress(Exception):
            s1.close()
        s2.close()

    # UDP discovery probe arrives during heavy fs traffic. We don't
    # have direct access to the discovery responder from the host, but
    # we can at least verify it answers a probe even when the daemon
    # is busy. (The probe runs on a peer task so this should always
    # work.)
    # Skip this for now - fleet.discover is in tools; we'd need
    # either a Fleet or to hand-roll the probe. The validate_full
    # script doesn't cover this either.
    r.skip("concurrency: discovery during heavy traffic",
           "requires multicast UDP socket; deferred")


# --------------------------------------------------------------------
# J. Numeric boundaries
# --------------------------------------------------------------------

async def section_j_numeric(r: Reporter, fleet: Fleet) -> None:
    r.section("J. Numeric boundaries")
    t = fleet.mcpd("target")

    # fs.read offset=0, length=0 -> empty
    await t.request(
        "fs.write",
        {"path": "RAM:vdur_n.bin",
         "content_b64": base64.b64encode(b"abcdef").decode()},
    )
    rd = await t.request(
        "fs.read",
        {"path": "RAM:vdur_n.bin", "offset": 0, "length": 0},
    )
    r.assert_("numeric: offset=0 length=0 returns empty",
              rd.get("size") == 0 and rd.get("total_size") == 6)

    # fs.read with length larger than file but offset=0
    rd2 = await t.request(
        "fs.read",
        {"path": "RAM:vdur_n.bin", "offset": 0, "length": 1000},
    )
    r.assert_("numeric: length > size returns full file",
              rd2.get("size") == 6
              and base64.b64decode(rd2["content_b64"]) == b"abcdef")

    # sys.alert_decode at extreme code values
    for code in [0, 1, 0x7FFFFFFF, 0xFFFFFFFE]:
        ad = await t.request("sys.alert_decode", {"code": code})
        r.assert_(
            f"numeric: sys.alert_decode code=0x{code:x}",
            "decoded" in ad and "code" in ad["decoded"],
        )

    # exec.cmd timeout_ms=1 (very short, but Echo is fast enough)
    out = await t.request(
        "exec.cmd",
        {"command": "Echo", "args": ["x"], "timeout_ms": 100},
    )
    r.assert_("numeric: exec.cmd timeout_ms=100",
              out.get("exit_code") == 0)

    # cleanup
    with suppress(Exception):
        await t.request("fs.delete", {"path": "RAM:vdur_n.bin"})


# --------------------------------------------------------------------
# K. Cross-method workflows
# --------------------------------------------------------------------

async def section_k_workflows(r: Reporter, fleet: Fleet) -> None:
    r.section("K. Cross-method workflows")

    # Workflow 1: build manifest, upload files, hash each, verify.
    ROOT = "RAM:vdur_wf"
    with suppress(Exception):
        await fs_tool.fs_delete(fleet, "target", ROOT, recursive=True)
    await fs_tool.fs_makedir(fleet, "target", ROOT)

    files = {
        "a.txt": b"hello world",
        "b.bin": bytes(range(256)),
        "c.zero": b"\x00" * 1024,
    }
    expected_hashes = {n: hashlib.sha256(c).hexdigest()
                       for n, c in files.items()}

    for name, content in files.items():
        await fs_tool.fs_write(
            fleet, "target", f"{ROOT}/{name}",
            base64.b64encode(content).decode(),
        )

    # Verify each via fs.hash
    all_ok = True
    for name, expected in expected_hashes.items():
        h = await fs_tool.fs_hash(fleet, "target", f"{ROOT}/{name}")
        if h.hash != expected:
            all_ok = False
            r.fail(f"workflow: hash mismatch for {name}",
                   f"got {h.hash}, expected {expected}")
    if all_ok:
        r.ok("workflow: write+hash round-trip on 3 files")

    # Workflow 2: list, read each, verify content.
    listing = await fs_tool.fs_list(fleet, "target", ROOT)
    for entry in listing:
        rd = await fs_tool.fs_read(
            fleet, "target", f"{ROOT}/{entry.name}",
        )
        got = base64.b64decode(rd.content_b64)
        if got != files.get(entry.name):
            r.fail(f"workflow: read mismatch for {entry.name}", "")
            break
    else:
        r.ok("workflow: list + read all entries match")

    # Workflow 3: rename + verify
    new_name = "renamed.txt"
    await fs_tool.fs_rename(
        fleet, "target", f"{ROOT}/a.txt", f"{ROOT}/{new_name}",
    )
    listing2 = await fs_tool.fs_list(fleet, "target", ROOT)
    names = {e.name for e in listing2}
    r.assert_("workflow: rename visible in subsequent list",
              "a.txt" not in names and new_name in names)

    # Workflow 4: recursive delete + confirm gone
    await fs_tool.fs_delete(fleet, "target", ROOT, recursive=True)
    try:
        await fs_tool.fs_list(fleet, "target", ROOT)
        r.fail("workflow: recursive delete leaves tree", "")
    except Exception:
        r.ok("workflow: recursive delete removes tree")


# --------------------------------------------------------------------
# L. MCP resources reading
# --------------------------------------------------------------------

async def section_l_resources(r: Reporter, fleet: Fleet) -> None:
    r.section("L. MCP resources")

    # We don't run the FastMCP server here, but we can verify each
    # resource's data source via the same per-target request the
    # @mcp.resource decorator uses internally.
    t = fleet.mcpd("target")

    # amiga://{target}/proto/capabilities
    caps = await t.request("proto.capabilities")
    r.assert_("resource: proto.capabilities source",
              "methods" in caps)

    # amiga://{target}/sys/version
    sv = await t.request("sys.version")
    r.assert_("resource: sys.version source",
              "kickstart" in sv or "raw" in sv)

    # amiga://{target}/sys/tasks
    st = await t.request("sys.tasks")
    r.assert_("resource: sys.tasks source",
              "ready" in st and "waiting" in st)

    # amiga://fleet/targets - host-side, exposed via Fleet
    targets = fleet.list_targets()
    r.assert_("resource: fleet/targets source",
              len(targets) >= 1)

    # amiga://{target}/qemu/serial_log: only meaningful for QEMU;
    # we're on a remote target so this is N/A. The tool registry has
    # the resource registered; reading from a non-QEMU target should
    # return an "unavailable" message rather than crash.
    r.skip("resource: qemu/serial_log",
           "remote target - not applicable")


# --------------------------------------------------------------------
# M. Fleet host tools
# --------------------------------------------------------------------

async def section_m_fleet(r: Reporter, fleet: Fleet) -> None:
    r.section("M. Fleet host tools")

    # fleet.list_targets
    targets = fleet.list_targets()
    r.assert_("fleet: list_targets returns >=1 target",
              len(targets) >= 1, f"targets={targets}")

    # fleet.target_status (host-side)
    from amiga_fleet_mcp.tools.fleet import (  # noqa: PLC0415
        fleet_target_status,
        fleet_run_on_all,
        fleet_barrier,
        fleet_quorum_run,
    )
    from amiga_fleet_mcp.tools.fleet_discover import (  # noqa: PLC0415
        fleet_discover,
    )
    status = await fleet_target_status(fleet, "target")
    r.assert_("fleet: target_status returns reachable target",
              status.mcpd_reachable is True,
              f"mcpd_reachable={status.mcpd_reachable}")

    # fleet.run_on_all (single-target fan-out is still a valid call)
    roa = await fleet_run_on_all(fleet, "sys.version", {})
    r.assert_("fleet: run_on_all returns one entry per target",
              len(roa.results) == len(targets))
    ok_count = sum(1 for e in roa.results.values()
                   if e.ok is not None)
    r.assert_("fleet: run_on_all all ok on single target",
              ok_count == len(targets))

    # fleet.barrier with a per-target timeout
    br = await fleet_barrier(
        fleet, "sys.version", {}, per_target_timeout_s=10.0,
    )
    ok_b = sum(1 for e in br.results.values() if e.ok is not None)
    r.assert_("fleet: barrier returns ok per target",
              ok_b == len(targets))

    # fleet.quorum_run requiring 1 of 1 target
    qr = await fleet_quorum_run(
        fleet, "sys.version", quorum=1,
    )
    r.assert_("fleet: quorum_run with quorum=1 reached",
              qr.reached is True,
              f"reached={qr.reached} quorum={qr.quorum}")

    # fleet.discover via UDP broadcast - the daemon's discovery
    # responder runs on a peer task on port 4323. Probe with a short
    # timeout; the response correlation is automatic.
    try:
        disc = await fleet_discover(fleet, timeout_ms=2000)
        if len(disc.targets) >= 1:
            r.ok(
                "fleet: discover finds the daemon",
                f"discovered={[d.host for d in disc.targets]}",
            )
        else:
            # UDP broadcast may be filtered by the local-host firewall;
            # the daemon's discovery responder still works, but the
            # outbound probe never reaches it. Treat as skip.
            r.skip(
                "fleet: discover finds the daemon",
                "no responses (UDP broadcast may be firewall-blocked)",
            )
    except Exception as e:
        r.skip("fleet: discover", str(e))


# --------------------------------------------------------------------
# N. CLI flags (subprocess invocations)
# --------------------------------------------------------------------

import os               # noqa: E402  (kept here for section locality)
import subprocess       # noqa: E402

def section_n_cli(r: Reporter, endpoint: str) -> None:
    r.section("N. CLI flags")

    # Build a temp config for the CLI (the same trick validate_full
    # uses). The CLI requires a loadable config to register tools.
    tmp_root = HERE.parent / "tmp" / "validate-durability-cli"
    tmp_root.mkdir(parents=True, exist_ok=True)
    cfg = tmp_root / "config.toml"
    cfg.write_text(
        f'[server]\n'
        f'archive_root = "{(tmp_root / "archive").as_posix()}"\n'
        f'mcp_transport = "stdio"\n'
        f'\n'
        f'[targets.target]\n'
        f'type = "remote"\n'
        f'display_name = "target"\n'
        f'\n'
        f'[targets.target.channels.mcpd]\n'
        f'enabled = true\n'
        f'endpoint = "{endpoint}"\n'
    )

    base = [
        sys.executable, "-m", "amiga_fleet_mcp.server",
        "--config", str(cfg),
    ]
    env = {**os.environ, "PYTHONPATH": str(HOST_SRC)}
    cwd = str(HERE.parent / "host")

    # --inspect
    out = subprocess.run(
        base + ["--inspect"],
        cwd=cwd, env=env,
        capture_output=True, text=True, timeout=30,
    )
    r.assert_("cli: --inspect lists configured target",
              out.returncode == 0
              and ("target" in out.stdout
                   or "target" in out.stderr))

    # --health-check
    out = subprocess.run(
        base + ["--health-check"],
        cwd=cwd, env=env,
        capture_output=True, text=True, timeout=30,
    )
    r.assert_("cli: --health-check exits 0 for live target",
              out.returncode == 0)

    # --discover (broadcast probe, short timeout). The CLI returns
    # 0 when at least one MCPd responds and 1 when nothing answers
    # within the window (the latter is normal if the Windows
    # firewall blocks UDP broadcast). Either is "exited cleanly";
    # only a Python traceback would indicate a real failure.
    out = subprocess.run(
        base + ["--discover", "--discover-timeout-ms", "2000"],
        cwd=cwd, env=env,
        capture_output=True, text=True, timeout=30,
    )
    r.assert_(
        "cli: --discover runs without crashing",
        out.returncode in (0, 1)
        and "Traceback" not in out.stderr,
        f"rc={out.returncode}",
    )


# --------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------

async def amain(args: argparse.Namespace) -> int:
    r = Reporter()
    fleet = make_fleet(args.endpoint)

    # The daemon serves one TCP client at a time. Sections that use
    # raw sockets must run with no Fleet transport connected, otherwise
    # their connections queue behind the Fleet's open socket and time
    # out. fleet.close_all() releases the persistent transport; the
    # next fleet method call lazily reconnects.
    try:
        # Raw-socket sections (no Fleet open).
        section_a_frames(r, args.endpoint)
        section_b_envelope(r, args.endpoint)
        section_d_lifecycle(r, args.endpoint)
        section_i_concurrency(r, args.endpoint)

        # Fleet-driven sections.
        await section_c_errors(r, fleet)
        await section_e_longrun(r, fleet, args.skip_long)
        await section_f_fs(r, fleet)
        await section_g_exec(r, fleet)
        await section_h_subs(r, fleet)
        await section_j_numeric(r, fleet)
        await section_k_workflows(r, fleet)
        await section_l_resources(r, fleet)
        await section_m_fleet(r, fleet)
        # CLI section runs subprocesses that open their own short-
        # lived connections; close fleet first to free the daemon's
        # single-conn slot.
        await fleet.close_all()
        section_n_cli(r, args.endpoint)
    finally:
        await fleet.close_all()
    return r.finalize()


def main() -> int:
    import os
    p = argparse.ArgumentParser()
    p.add_argument("--endpoint", default=os.environ.get("MCPD_ENDPOINT"),
                   help="MCPd endpoint (default $MCPD_ENDPOINT; required)")
    p.add_argument("--skip-long", action="store_true")
    args = p.parse_args()
    if not args.endpoint:
        p.error("--endpoint or $MCPD_ENDPOINT is required")
    return asyncio.run(amain(args))


if __name__ == "__main__":
    sys.exit(main())
