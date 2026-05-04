"""Direct JSON-RPC probe of a target's BootTest: NetInterfaces.

Configuration:
  --endpoint <host:port>   MCPd endpoint (default $MCPD_ENDPOINT;
                           required)
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import sys

HOST: str = ""
PORT: int = 4322


def call(method: str, params: dict | None = None) -> dict:
    s = socket.create_connection((HOST, PORT), timeout=15)
    msg = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params:
        msg["params"] = params
    body = json.dumps(msg, separators=(",", ":")).encode()
    s.sendall(struct.pack(">I", len(body)) + body)
    hdr = b""
    while len(hdr) < 4:
        c = s.recv(4 - len(hdr))
        if not c: break
        hdr += c
    n = struct.unpack(">I", hdr)[0]
    buf = b""
    while len(buf) < n:
        c = s.recv(n - len(buf))
        if not c: break
        buf += c
    s.close()
    return json.loads(buf.decode())


def main() -> int:
    global HOST, PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default=os.environ.get("MCPD_ENDPOINT"),
                    help="MCPd endpoint host:port (default "
                         "$MCPD_ENDPOINT; required)")
    args = ap.parse_args()
    if not args.endpoint:
        ap.error("--endpoint or $MCPD_ENDPOINT is required")
    HOST, _, port_s = args.endpoint.partition(":")
    PORT = int(port_s) if port_s else 4322

    # 1. List every dir that might hold network config
    for path in ("DEVS:NetInterfaces", "DEVS:NetInterfaces/Plugins",
                 "DEVS:Internet", "ENV:Sys/Net"):
        try:
            r = call("fs.list", {"path": path})
            res = r.get("result", [])
            # fs.list returns a list directly OR a dict with 'entries'.
            entries = res if isinstance(res, list) else res.get("entries", [])
            print(f"\n=== {path} ===")
            for e in entries:
                t = e.get("type", "?")
                size = e.get("size", "")
                print(f"  [{t}] {e.get('name')} ({size}B)")
        except Exception as ex:
            print(f"\n=== {path} === skip: {ex}")

    # 2. Read each NetInterface file
    print("\n\n--- NetInterface file contents ---")
    try:
        r = call("fs.list", {"path": "DEVS:NetInterfaces"})
        raw = r.get("result", [])
        entries = raw if isinstance(raw, list) else raw.get("entries", [])
        for e in entries:
            if e.get("type") != "file":
                continue
            name = e.get("name", "")
            if name.endswith(".info"):
                continue
            full = "DEVS:NetInterfaces/" + name
            rd = call("fs.read", {"path": full})
            res = rd.get("result", {})
            if "content_b64" in res:
                txt = base64.b64decode(res["content_b64"]).decode(
                    "ascii", errors="replace")
                print(f"\n>>> {full} ({res.get('size', '?')}B):")
                for line in txt.splitlines():
                    print(f"    {line}")
    except Exception as ex:
        print(f"could not enumerate NetInterfaces: {ex}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
