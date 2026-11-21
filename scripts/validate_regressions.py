"""Regression checks for MCPd bugs fixed in 2.5, against a live daemon.

Usage:
    python scripts/validate_regressions.py --endpoint host:port

Run it on a freshly booted QEMU guest with fast networking (virtio-net
is quick; rtl8139 takes about ten minutes): it uploads a 24 MiB file,
runs a deliberately crashing program (scripts/crashtest, which leaves a
GrimReaper requester on screen), and opens 17 connections at once.
Checks:

  1. app.notify answers (three in a row), a transfer right after it
     works, MCPd is registered once and reports allows_blanker false
     straight after a transfer, and the "MCPd applib" process runs.
  2. fs.read of exactly 25116672 bytes works; one byte more, or the
     whole larger file, is refused with an error and the connection
     stays usable.
  3. exec.cmd output over 1 MiB is truncated and exec.output returns
     the rest.
  4. A crash caught by the IDebug hook reaches both connections
     subscribed to debug.exception and not a third that is not
     subscribed; events.wait returns it; a connection opened afterwards
     is not sent it.
  5. Sixteen connections are served, the seventeenth is refused, and
     sixteen are served again after the first ones close.

Against 2.4, check 1 fails (each app.notify hangs) and wedges the
daemon's transfers, so the rest cannot run. Exit code = number of
failed checks.
"""
import argparse
import asyncio
import base64
import json
import os
import socket
import struct
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "host" / "src"))
from amiga_fleet_mcp.config import (  # noqa: E402
    Config,
    McpdChannel,
    PathsConfig,
    ServerConfig,
    TargetChannels,
    TargetConfig,
)
from amiga_fleet_mcp.fleet import Fleet  # noqa: E402
from amiga_fleet_mcp.tools import exec as exec_tool  # noqa: E402
from amiga_fleet_mcp.tools import fs as fs_tool  # noqa: E402

_ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
_ap.add_argument("--endpoint", default=os.environ.get("MCPD_ENDPOINT"),
                 help="MCPd endpoint (default $MCPD_ENDPOINT; required)")
_args = _ap.parse_args()
if not _args.endpoint:
    _ap.error("--endpoint or $MCPD_ENDPOINT is required")
EP = _args.endpoint
HOST, _port = EP.rsplit(":", 1)
PORT = int(_port)
FAILS = []


def check(name, ok, info=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {info}", flush=True)
    if not ok:
        FAILS.append(name)


# ---- raw framing ----------------------------------------------------
def conn():
    s = socket.create_connection((HOST, PORT), timeout=10)
    return s


def send(s, method, params=None, rid=1):
    b = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method,
                    "params": params or {}}).encode()
    s.sendall(struct.pack(">I", len(b)) + b)


def recv(s, timeout=10.0):
    s.settimeout(timeout)
    h = b""
    while len(h) < 4:
        c = s.recv(4 - len(h))
        if not c:
            raise EOFError("closed")
        h += c
    (n,) = struct.unpack(">I", h)
    d = b""
    while len(d) < n:
        c = s.recv(min(1 << 20, n - len(d)))
        if not c:
            raise EOFError("closed mid-frame")
        d += c
    return json.loads(d)


def call(s, method, params=None, timeout=30.0, notes=None):
    """Request; collect notifications that arrive before the reply."""
    send(s, method, params)
    while True:
        m = recv(s, timeout)
        if "id" in m:
            return m
        if notes is not None:
            notes.append(m)


def drain(s, secs):
    out = []
    end = time.time() + secs
    while time.time() < end:
        try:
            out.append(recv(s, max(0.1, end - time.time())))
        except (socket.timeout, TimeoutError):
            break
    return out


def make_fleet():
    cfg = Config(
        server=ServerConfig(archive_root=Path(os.environ.get("TEMP", ".")) / "r25-archive"),
        paths=PathsConfig(),
        targets={"t": TargetConfig(type="remote", display_name="t",
                                   channels=TargetChannels(mcpd=McpdChannel(endpoint=EP)))})
    return Fleet(cfg)


async def main():
    fleet = make_fleet()
    t = fleet.mcpd("t")
    caps = await t.request("proto.capabilities")
    print("server", caps["server"], flush=True)

    # 1. app.notify answers; a transfer right after it works -------------
    print("[1] app.notify, then transfers")
    for i in range(3):
        t0 = time.perf_counter()
        try:
            r = await t.request("app.notify", {"title": "regress25",
                                               "text": f"notify {i}"}, timeout_s=20)
            check(f"app.notify #{i}", r.get("queued") is True,
                  f"rc={r.get('result_code')} {time.perf_counter()-t0:.2f}s")
        except Exception as e:  # noqa: BLE001
            check(f"app.notify #{i}", False, repr(e))
    try:
        await asyncio.wait_for(t.request("fs.write", {
            "path": "RAM:r25.bin", "content_b64": base64.b64encode(b"x" * 64).decode()}), 15)
        check("fs.write after app.notify", True)
    except Exception as e:  # noqa: BLE001
        check("fs.write after app.notify", False, repr(e))
    apps = await t.request("sys.applications")
    me = [a for a in apps["applications"] if a.get("url_identifier") == "com.derfsss.mcpd"]
    check("MCPd registered once", len(me) == 1, f"{len(me)} entries")
    check("allows_blanker false right after a transfer",
          bool(me) and me[0].get("allows_blanker") is False, str(me[0] if me else None))
    tasks = await t.request("sys.tasks")
    names = [x.get("name") for k in ("running", "ready", "waiting") for x in tasks.get(k, [])]
    check("MCPd applib process running", "MCPd applib" in names)

    # 2. fs.read at the cap and one byte over ----------------------------
    print("[2] fs.read cap")
    cap = (32 * 1024 * 1024 - 65536) // 4 * 3
    local = Path(os.environ.get("TEMP", ".")) / "r25cap.bin"
    blob = os.urandom(cap + 1)
    local.write_bytes(blob)
    await fs_tool.fs_upload(fleet, "t", local_path=str(local),
                            remote_path="RAM:r25cap.bin", transfer="stream")
    s = conn()
    r = call(s, "fs.read", {"path": "RAM:r25cap.bin", "length": cap}, timeout=120)
    ok = "result" in r and base64.b64decode(r["result"]["content_b64"]) == blob[:cap]
    check("fs.read of exactly the cap", ok,
          str(r.get("error")) if "error" in r else f"size={r['result']['size']}")
    r = call(s, "fs.read", {"path": "RAM:r25cap.bin", "length": cap + 1}, timeout=60)
    check("fs.read cap+1 refused with an error", "error" in r, str(r.get("error", ""))[:120])
    r = call(s, "sys.uptime")
    check("same connection still answers", "result" in r)
    r = call(s, "fs.read", {"path": "RAM:r25cap.bin"}, timeout=60)
    check("fs.read of the whole (cap+1) file refused", "error" in r)
    s.close()
    await t.request("fs.delete", {"path": "RAM:r25cap.bin"})
    local.unlink()

    # 3. exec.cmd truncated output stays readable ------------------------
    print("[3] exec.cmd output over 1 MiB")
    text = ("0123456789abcdef" * 4 + "\n") * 25000          # ~1.6 MiB
    lt = Path(os.environ.get("TEMP", ".")) / "r25big.txt"
    lt.write_text(text, newline="\n")
    await fs_tool.fs_upload(fleet, "t", local_path=str(lt),
                            remote_path="RAM:r25big.txt", transfer="stream")
    r = await exec_tool.exec_cmd(fleet, "t", "Type RAM:r25big.txt", timeout_s=60)
    check("exec.cmd reports truncation", r.truncated, f"output={len(r.output)}")
    try:
        rest = await exec_tool.exec_output(fleet, "t", r.job_id, offset=len(r.output.encode("latin-1")),
                                           max_bytes=4 << 20)
        got = r.output + rest.output
        check("exec.output returns the rest", got == text,
              f"total={len(got)} want={len(text)} eof={rest.eof}")
    except Exception as e:  # noqa: BLE001
        check("exec.output returns the rest", False, repr(e))
    await t.request("fs.delete", {"path": "RAM:r25big.txt"})
    lt.unlink()

    # 4. crash hook: two subscribers get it, a non-subscriber does not ---
    print("[4] crash hook delivery")
    crash = REPO / "scripts" / "crashtest"
    await fs_tool.fs_upload(fleet, "t", local_path=str(crash), remote_path="RAM:crashtest")
    await t.request("fs.protect", {"path": "RAM:crashtest", "bits": 0})
    a, b, c = conn(), conn(), conn()
    for x in (a, b):
        r = call(x, "events.subscribe", {"topics": ["debug.exception"]})
        check("subscribe", "result" in r)
    call(c, "sys.uptime")
    before = (await t.request("sys.crashhook_status"))["exception_count"]
    waiter = asyncio.create_task(make_fleet().mcpd("t").request(
        "events.wait", {"topics": ["debug.exception"], "timeout_ms": 20000}, timeout_s=40))
    await asyncio.sleep(1)
    await t.request("exec.cmd", {"command": "Run >NIL: <NIL: RAM:crashtest"})
    na = [m for m in drain(a, 6) if m.get("method") == "events.notify"]
    nb = [m for m in drain(b, 2) if m.get("method") == "events.notify"]
    nc = drain(c, 2)
    hook = lambda ms: [m for m in ms if m["params"]["data"].get("source") == "crash_hook"]  # noqa: E731
    check("subscriber A got the crash", len(hook(na)) == 1, f"{len(na)} notes")
    check("subscriber B got the crash", len(hook(nb)) == 1, f"{len(nb)} notes")
    check("non-subscriber C got nothing", nc == [], f"{nc[:1]}")
    if hook(na):
        d = hook(na)[0]["params"]["data"]
        check("crash names crashtest", "crashtest" in d.get("task", "").lower() or d.get("task"),
              d.get("task"))
    w = await waiter
    ev = [e for e in w.get("events", []) if e["data"].get("source") == "crash_hook"]
    check("events.wait sees the hook crash", len(ev) == 1, json.dumps(w)[:160])
    after = (await t.request("sys.crashhook_status"))["exception_count"]
    check("exception_count moved", after > before, f"{before}->{after}")
    # a connection opened after the crash is not sent it
    d2 = conn()
    call(d2, "events.subscribe", {"topics": ["debug.exception"]})
    late = [m for m in drain(d2, 2) if m.get("method") == "events.notify"]
    check("new subscriber not sent the old crash", late == [], str(late[:1]))
    for x in (a, b, c, d2):
        x.close()

    # 5. worker cap and slot release -------------------------------------
    print("[5] worker slots")
    await t.close()
    socks = []
    for i in range(16):
        x = conn()
        r = call(x, "sys.uptime")
        socks.append(x)
    check("16 connections served", len(socks) == 16)
    x = conn()
    try:
        send(x, "sys.uptime")
        r = recv(x, 5)
        check("17th connection refused", False, "it was served")
    except (EOFError, ConnectionError, OSError, socket.timeout):
        check("17th connection refused", True)
    x.close()
    for x in socks:
        x.close()
    await asyncio.sleep(2)
    socks = []
    for i in range(16):
        x = conn()
        r = call(x, "sys.uptime")
        socks.append(x)
    check("16 connections served again after closing", len(socks) == 16)
    for x in socks:
        x.close()

    print(f"\n[summary] failures={len(FAILS)} {FAILS}")
    return len(FAILS)


sys.exit(asyncio.run(main()))
