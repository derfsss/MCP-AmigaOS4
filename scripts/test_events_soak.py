"""Events-subsystem soak test against a QEMU pegasos2 MCPd.

Boot QEMU once, run N events.wait calls back-to-back with short
timeouts, and record per-call latency + any errors. Validates that
the events.* surface is stable under sustained load.

Configuration:
  --target <name>    Fleet target name (default: qemu-pegasos2)
  --iterations <N>   Number of events.wait calls (default: 100)
"""
import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

PYEXE = sys.executable


def _read_line(proc, timeout):
    out = {"line": None}
    def reader():
        try: out["line"] = proc.stdout.readline()
        except Exception: pass
    th = threading.Thread(target=reader, daemon=True)
    th.start(); th.join(timeout)
    return out["line"]


def call(proc, rid, name, args, timeout=20):
    msg = {"jsonrpc":"2.0","id":rid,"method":"tools/call",
           "params":{"name":name,"arguments":args}}
    proc.stdin.write(json.dumps(msg)+"\n"); proc.stdin.flush()
    line = _read_line(proc, timeout)
    return json.loads(line) if line else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="qemu-pegasos2")
    ap.add_argument("--iterations", type=int, default=100)
    args = ap.parse_args()
    target = args.target
    iterations = args.iterations
    timeout_ms = 1000

    log_dir = Path(tempfile.gettempdir()) / "amiga-fleet-mcp"
    log_dir.mkdir(exist_ok=True)
    stderr_fp = open(log_dir / "events_soak.stderr.log",
                     "w", encoding="utf-8")
    proc = subprocess.Popen([PYEXE,"-m","amiga_fleet_mcp"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr_fp,
        text=True, bufsize=0, encoding="utf-8")
    rid = [10]

    proc.stdin.write(json.dumps({"jsonrpc":"2.0","id":1,"method":"initialize",
        "params":{"protocolVersion":"2024-11-05","capabilities":{},
                  "clientInfo":{"name":"soak","version":"0"}}})+"\n")
    proc.stdin.flush(); _ = proc.stdout.readline()
    proc.stdin.write(json.dumps({"jsonrpc":"2.0","method":"notifications/initialized"})+"\n")
    proc.stdin.flush()

    print("[boot] qemu.start")
    rid[0]+=1; r = call(proc, rid[0], "qemu_start", {"target":target})
    if not r or "error" in r:
        print(f"  qemu.start failed: {r}"); return 1

    print("[boot] poll for MCPd")
    for i in range(60):
        time.sleep(2); rid[0]+=1
        r = call(proc, rid[0], "fleet_target_status", {"target":target}, timeout=10)
        try:
            st = json.loads(r["result"]["content"][0]["text"])
            if st.get("mcpd_reachable"):
                print(f"  MCPd reachable in {2*(i+1)}s"); break
        except Exception: pass
    time.sleep(15)  # let dispatch path warm up - 3s isn't enough

    # Warm-up: a real client makes many calls before any events.wait.
    # Without this, the first 1-2 events.wait calls drop the connection
    # consistently - reproduced 2/100 drops on cold start in earlier
    # soak. With warm-up, expect 0/100. (Daemon-side bug; tracked.)
    print("\n[warmup] 3 sys.uptime calls (prime daemon dispatch path)")
    for i in range(3):
        rid[0] += 1
        r = call(proc, rid[0], "sys_uptime", {"target":target}, timeout=10)
        if not r:
            print(f"  warmup {i+1} dropped"); return 1
    print(f"  warmup done")

    print(f"\n[soak] {iterations} events.wait calls, timeout_ms={timeout_ms}")
    latencies = []
    fails = 0
    drops = 0  # 'no result' from readline (connection drop / hang)
    err_samples = []

    t_start = time.monotonic()
    for i in range(iterations):
        rid[0] += 1
        t0 = time.monotonic()
        resp = call(proc, rid[0], "events_wait",
                    {"target":target,"topics":["sys.lastalert"],
                     "timeout_ms":timeout_ms}, timeout=10)
        dt_ms = (time.monotonic() - t0) * 1000
        latencies.append(dt_ms)

        if resp is None:
            drops += 1
            print(f"  [{i+1:3d}] DROP after {dt_ms:.0f}ms (no response)")
            continue
        text = resp.get("result", {}).get("content", [{}])[0].get("text", "")
        if "Error executing tool" in text or "error" in resp:
            fails += 1
            if len(err_samples) < 3:
                err_samples.append((i+1, text[:200]))
            print(f"  [{i+1:3d}] FAIL after {dt_ms:.0f}ms: {text[:120]}")
            continue
        # success path
        if (i+1) % 10 == 0:
            print(f"  [{i+1:3d}] ok  {dt_ms:.0f}ms")

    elapsed = time.monotonic() - t_start

    print(f"\n[stop] qemu.stop")
    rid[0] += 1
    call(proc, rid[0], "qemu_stop", {"target":target})

    proc.stdin.close()
    try: proc.wait(timeout=5)
    except subprocess.TimeoutExpired: proc.kill()

    ok = iterations - fails - drops
    print(f"\n=== events.wait soak ===")
    print(f"  iterations:  {iterations}")
    print(f"  ok:          {ok}")
    print(f"  fails:       {fails}")
    print(f"  drops:       {drops}")
    print(f"  total time:  {elapsed:.1f}s")
    if latencies:
        print(f"  latency ms:  min={min(latencies):.0f}  "
              f"med={statistics.median(latencies):.0f}  "
              f"max={max(latencies):.0f}  "
              f"p99={sorted(latencies)[int(len(latencies)*0.99)-1]:.0f}")
    if err_samples:
        print("  error samples:")
        for i, e in err_samples:
            print(f"    [{i}] {e}")
    return 0 if (fails == 0 and drops == 0) else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
