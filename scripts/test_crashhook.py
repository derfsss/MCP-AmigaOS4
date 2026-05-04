"""End-to-end verify the MCPd IDebug crash hook fires.

Deploys a tiny binary that dereferences NULL, runs it, and watches
for a `debug.exception` JSON-RPC notification with captured register
state. The notification rides the events.subscribe server-push
plumbing - so we open a connection, subscribe, fire the binary, and
read frames until we see the notification or hit the timeout.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOCAL_BIN = HERE / "crashtest"
REMOTE_BIN = "RAM:crashtest"


def send(s, msg):
    body = json.dumps(msg).encode()
    s.sendall(struct.pack(">I", len(body)) + body)


def recv_frame(s, timeout=10.0):
    s.settimeout(timeout)
    hdr = b""
    while len(hdr) < 4:
        chunk = s.recv(4 - len(hdr))
        if not chunk:
            return None
        hdr += chunk
    n = struct.unpack(">I", hdr)[0]
    buf = b""
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return json.loads(buf)


CAPTURED_NOTIFY = []


def call(s, method, params=None, request_id=1):
    msg = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params:
        msg["params"] = params
    send(s, msg)
    while True:
        f = recv_frame(s, timeout=20.0)
        if f is None:
            raise RuntimeError("connection dropped waiting for response")
        if f.get("id") == request_id:
            return f
        # Notification - print it, capture, then keep reading for the response.
        nm = f.get("method")
        ps = f.get("params", {})
        print(f"  notify (during call): method={nm} params={ps}")
        if nm == "events.notify":
            CAPTURED_NOTIFY.append(ps)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default=os.environ.get("MCPD_ENDPOINT"),
                    help="MCPd endpoint host:port (default $MCPD_ENDPOINT)")
    args = ap.parse_args()
    if not args.endpoint:
        ap.error("--endpoint or $MCPD_ENDPOINT is required")
    host, _, port_s = args.endpoint.partition(":")
    port = int(port_s) if port_s else 4322

    bin_bytes = LOCAL_BIN.read_bytes()
    print(f"[1] connect {host}:{port}")
    s = socket.create_connection((host, port), timeout=10.0)

    print("[2] proto.capabilities")
    r = call(s, "proto.capabilities")
    methods = r.get("result", {}).get("methods", [])
    names = [m.get("name") for m in methods] if isinstance(methods, list) else []
    print(f"    {len(names)} methods, has sys.modules={'sys.modules' in names}")

    print("[2.5] sys.crashhook_status (pre-fire)")
    r = call(s, "sys.crashhook_status")
    print(f"    {r.get('result') or r.get('error')}")

    print("[3] subscribe debug.exception")
    r = call(s, "events.subscribe", {"topics": ["debug.exception"]})
    print(f"    {r.get('result')}")

    print(f"[3.5] clear protect bits + delete")
    r = call(s, "fs.protect", {"path": REMOTE_BIN, "bits": 0})
    print(f"    protect: {r.get('result') or r.get('error')}")
    r = call(s, "fs.delete", {"path": REMOTE_BIN})
    print(f"    delete: {r.get('result') or r.get('error')}")

    print(f"[4] fs.write -> {REMOTE_BIN} ({len(bin_bytes)} bytes)")
    r = call(s, "fs.write", {
        "path": REMOTE_BIN,
        "content_b64": base64.b64encode(bin_bytes).decode(),
    })
    print(f"    {r.get('result') or r.get('error')}")

    print("[5] fs.protect bits=0 (all access)")
    r = call(s, "fs.protect", {"path": REMOTE_BIN, "bits": 0})
    print(f"    {r.get('result') or r.get('error')}")

    # Capture lastalert before
    r = call(s, "sys.lastalert")
    before = r.get("result", {}).get("alert_code")
    print(f"[6] alert before: 0x{before:08x}" if isinstance(before, int)
          else f"[6] alert before: {before}")

    # Use `Run` so the child detaches: SystemTags returns immediately,
    # the binary crashes asynchronously, and MCPd's WaitSelect loop
    # drains the hook on the next 200ms tick. Without this the daemon
    # blocks inside SystemTags until GrimReaper's modal is dismissed.
    print("[7] exec.cmd Run RAM:crashtest")
    r = call(s, "exec.cmd", {"command": "Run >NIL: RAM:crashtest",
                             "timeout_s": 5})
    print(f"    response: {json.dumps(r)[:200]}")

    # Read frames for up to 30s to catch the debug.exception notification.
    print("[8] watching for debug.exception notifications (30s)...")
    deadline = time.time() + 30.0
    found = []
    s.settimeout(1.0)
    while time.time() < deadline:
        try:
            f = recv_frame(s, timeout=max(0.1, deadline - time.time()))
        except (socket.timeout, TimeoutError):
            continue
        except Exception as e:
            print(f"    recv error: {e}")
            break
        if f is None:
            break
        if f.get("method") == "events.notify":
            params = f.get("params", {})
            if params.get("topic") == "debug.exception":
                found.append(params.get("data", {}))
                d = params.get("data", {})
                print(f"    GOT debug.exception: task={d.get('task')!r} "
                      f"pc=0x{d.get('pc',0):08x} dar=0x{d.get('dar',0):08x} "
                      f"traptype={d.get('traptype')}")

    r = call(s, "sys.lastalert")
    after = r.get("result", {}).get("alert_code")
    print(f"[9] alert after: 0x{after:08x}" if isinstance(after, int)
          else f"[9] alert after: {after}")

    print("[10] sys.crashhook_status (post-fire)")
    r = call(s, "sys.crashhook_status")
    print(f"    {r.get('result') or r.get('error')}")

    s.close()

    # Combine notifications captured mid-call and during the post-call
    # watch window. The hook usually drains during exec.cmd's return
    # path so the in-call collector is what fires.
    all_notify = [n.get("data", {}) for n in CAPTURED_NOTIFY
                  if n.get("topic") == "debug.exception"] + found
    # Hook payloads have the rich keys (task, pc, dar). LastAlert-poll
    # payloads have only alert_code/decoded/payload. Flag the rich ones.
    hook_hits = [d for d in all_notify if "task" in d and "dar" in d]

    print()
    print(f"=== RESULT: {len(all_notify)} debug.exception total, "
          f"{len(hook_hits)} from IDebug hook ===")
    if hook_hits:
        print(json.dumps(hook_hits[0], indent=2))
        return 0
    if all_notify:
        print("PARTIAL: LastAlert-poll fired but IDebug hook did not")
        print(json.dumps(all_notify[0], indent=2))
        return 1
    print("FAIL: no notifications received")
    return 1


if __name__ == "__main__":
    sys.exit(main())
