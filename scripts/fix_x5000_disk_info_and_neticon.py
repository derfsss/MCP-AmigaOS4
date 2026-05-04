"""Live patch on X5000 BootTest::
  - Copy <dest>:DEVS/NetInterfaces/P50X0_ETH1.info -> P50X0_ETH.info
    (so port 0 NetInterface shows up in Prefs/Network GUI like port 1).
  - Copy <ISO>:Installation-Files/Disk.info -> <dest>:Disk.info
    if the volume root doesn't already have an icon.

Configuration:
  --endpoint <host:port>   MCPd endpoint (default $MCPD_ENDPOINT;
                           required)
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import sys

HOST: str = ""
PORT: int = 4322


def call(method, params=None):
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


def stat(path):
    r = call("fs.stat", {"path": path})
    return "result" in r


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

    print("=== fixing X5000 BootTest: ===")

    # 1. NetInterface icon: clone P50X0_ETH1.info -> P50X0_ETH.info
    src = "BootTest:DEVS/NetInterfaces/P50X0_ETH1.info"
    dst = "BootTest:DEVS/NetInterfaces/P50X0_ETH.info"
    print(f"\n[1] {dst} from {src}")
    if stat(dst):
        print("    already exists, skipping")
    elif not stat(src):
        print("    source missing, skipping")
    else:
        r = call("fs.copy", {"src": src, "dst": dst})
        if "error" in r:
            print(f"    failed: {r['error']}")
        else:
            print("    copied OK")

    # 2. Disk.info on volume root. The ISO is unmounted by now, so
    #    we copy from the user's currently-mounted AmigaOS: install
    #    if it has Disk.info there. Otherwise skip with a hint.
    print(f"\n[2] BootTest:Disk.info")
    if stat("BootTest:Disk.info"):
        print("    already exists, skipping")
    else:
        # Try common source paths that might hold a disk icon.
        candidates = [
            "AmigaOS:Disk.info",
            "AmigaOS4.1:Disk.info",
            "DH0:Disk.info",
            "DH1:Disk.info",
            "DH2:Disk.info",
        ]
        copied = False
        for cand in candidates:
            if stat(cand):
                r = call("fs.copy",
                         {"src": cand, "dst": "BootTest:Disk.info"})
                if "result" in r:
                    print(f"    copied from {cand}")
                    copied = True
                    break
        if not copied:
            print("    no template found (would need ISO remounted to "
                  "copy from <ISO>:Installation-Files/Disk.info)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
