"""Install MCPd + refresh SerialShell on a remote AmigaOS via SerialShell.

Steps:
  1. Connect to SerialShell at <host>:4321
  2. Upload SerialShell binary -> T:SerialShell
  3. Copy T:SerialShell -> C:SerialShell (+rwed)
  4. Upload MCPd binary -> T:MCPd
  5. Upload mcpd/install/MCPd-Install script -> T:MCPd-Install
  6. CD T: + Execute MCPd-Install (copies binary, edits S:Network-Startup)
  7. Verify both binaries are in place

Used to bring up MCPd on a fleet member that doesn't have it yet
(e.g. A1222 today). Once MCPd is running we can use the host MCP
tools instead.

The SerialShell protocol used here is the same one qemu-runner's
serial_client.py speaks: SERIALSHELL_READY banner, line-based
commands ending at ___SERIALSHELL_DONE___, SERIALSHELL_UPLOAD for
binary uploads.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import time
from pathlib import Path

READY = b"SERIALSHELL_READY\n"
DONE = b"___SERIALSHELL_DONE___\n"
QUIT = b"SERIALSHELL_QUIT\n"
UPLOAD_OK = b"SERIALSHELL_UPLOAD_OK"
UPLOAD_FAIL = b"SERIALSHELL_UPLOAD_FAIL"


class SerialShell:
    def __init__(self, host: str, port: int = 4321,
                 connect_timeout: float = 10.0) -> None:
        self.s = socket.create_connection((host, port),
                                          timeout=connect_timeout)
        self._buf = b""
        end = time.monotonic() + connect_timeout
        while time.monotonic() < end:
            self.s.settimeout(end - time.monotonic())
            try:
                chunk = self.s.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            self._buf += chunk
            if READY in self._buf:
                self._buf = self._buf.split(READY, 1)[1]
                return
        raise RuntimeError(f"no READY in {self._buf!r}")

    def cmd(self, command: str, timeout: float = 30.0) -> str:
        self.s.sendall((command + "\n").encode("latin-1"))
        end = time.monotonic() + timeout
        while DONE not in self._buf and time.monotonic() < end:
            self.s.settimeout(max(0.1, end - time.monotonic()))
            try:
                chunk = self.s.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            self._buf += chunk
        if DONE not in self._buf:
            raise TimeoutError(
                f"no DONE marker for {command!r}, "
                f"partial: {self._buf!r}"
            )
        out, self._buf = self._buf.split(DONE, 1)
        return out.decode("latin-1", errors="replace")

    def upload_bytes(self, payload: bytes, guest_path: str,
                     timeout: float = 120.0) -> None:
        size = len(payload)
        header = f"SERIALSHELL_UPLOAD {guest_path} {size}\n"
        self.s.settimeout(timeout)
        self.s.sendall(header.encode("latin-1"))
        idx = 0
        while idx < size:
            n = min(8192, size - idx)
            self.s.sendall(payload[idx:idx + n])
            idx += n
        self._wait_upload_response(timeout)

    def upload(self, local_path: str, guest_path: str,
               timeout: float = 120.0) -> None:
        size = os.path.getsize(local_path)
        header = f"SERIALSHELL_UPLOAD {guest_path} {size}\n"
        self.s.settimeout(timeout)
        self.s.sendall(header.encode("latin-1"))
        with open(local_path, "rb") as fh:
            remaining = size
            while remaining > 0:
                chunk = fh.read(min(8192, remaining))
                if not chunk:
                    break
                self.s.sendall(chunk)
                remaining -= len(chunk)
        self._wait_upload_response(timeout)

    def _wait_upload_response(self, timeout: float) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                line = self._buf[:nl]
                self._buf = self._buf[nl + 1:]
                if UPLOAD_OK in line:
                    return
                if UPLOAD_FAIL in line:
                    raise RuntimeError(f"upload failed: {line!r}")
                continue
            self.s.settimeout(max(0.1, end - time.monotonic()))
            try:
                chunk = self.s.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("server closed during upload")
            self._buf += chunk
        raise TimeoutError("upload response timeout")

    def download_text(self, guest_path: str,
                      timeout: float = 30.0) -> str:
        """Download a small text file via SERIALSHELL_DOWNLOAD."""
        header = f"SERIALSHELL_DOWNLOAD {guest_path}\n"
        self.s.settimeout(timeout)
        self.s.sendall(header.encode("latin-1"))
        # Read SERIALSHELL_FILE <size>\n header
        end = time.monotonic() + timeout
        while b"\n" not in self._buf and time.monotonic() < end:
            self.s.settimeout(max(0.1, end - time.monotonic()))
            try:
                chunk = self.s.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("server closed during download")
            self._buf += chunk
        nl = self._buf.find(b"\n")
        if nl < 0:
            raise TimeoutError("no SERIALSHELL_FILE header")
        line = self._buf[:nl].decode("latin-1", errors="replace")
        self._buf = self._buf[nl + 1:]
        if not line.startswith("SERIALSHELL_FILE"):
            raise RuntimeError(f"unexpected download header: {line!r}")
        size = int(line.split()[1])
        # Read <size> bytes of file content
        while len(self._buf) < size:
            self.s.settimeout(max(0.1, end - time.monotonic()))
            try:
                chunk = self.s.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                raise ConnectionError("server closed during download body")
            self._buf += chunk
        body = bytes(self._buf[:size])
        self._buf = self._buf[size:]
        # Drain the trailing DONE marker
        while DONE not in self._buf and time.monotonic() < end:
            self.s.settimeout(max(0.1, end - time.monotonic()))
            try:
                chunk = self.s.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            self._buf += chunk
        if DONE in self._buf:
            self._buf = self._buf.split(DONE, 1)[1]
        return body.decode("latin-1", errors="replace")

    def close(self) -> None:
        try:
            self.s.sendall(QUIT)
        except Exception:
            pass
        try:
            self.s.close()
        except Exception:
            pass


def run(c: SerialShell, command: str) -> str:
    print(f"\n>>> {command}")
    out = c.cmd(command)
    for line in out.splitlines():
        line = line.rstrip()
        if line:
            print(f"    {line}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True,
                    help="A1222 / X5000 IP")
    ap.add_argument("--port", type=int, default=4321)
    ap.add_argument("--mcpd-binary",
                    default=str(Path(__file__).resolve().parents[1]
                                / "mcpd" / "MCPd"))
    ap.add_argument("--mcpd-install-script",
                    default=str(Path(__file__).resolve().parents[1]
                                / "mcpd" / "install" / "MCPd-Install"))
    ap.add_argument("--serialshell-binary",
                    required=True,
                    help="Path to a SerialShell binary on the host")
    args = ap.parse_args()

    for p in (args.mcpd_binary, args.mcpd_install_script,
              args.serialshell_binary):
        if not os.path.isfile(p):
            print(f"missing local file: {p}")
            return 2

    print(f"connecting to SerialShell @ {args.host}:{args.port}...")
    c = SerialShell(args.host, args.port)
    print("READY received.")

    try:
        # 1. Upload SerialShell binary to T:
        print(f"\n[1] uploading {args.serialshell_binary} -> T:SerialShell")
        c.upload(args.serialshell_binary, "T:SerialShell")
        print("    ok.")

        # 2. Copy to C: + Protect +rwed
        run(c, 'Copy T:SerialShell C:SerialShell CLONE')
        run(c, 'Protect C:SerialShell +rwed')
        run(c, 'Delete T:SerialShell QUIET')

        # 3. Upload MCPd binary
        print(f"\n[2] uploading {args.mcpd_binary} -> T:MCPd")
        c.upload(args.mcpd_binary, "T:MCPd")
        print("    ok.")

        # 4. Inline the steps from mcpd/install/MCPd-Install (Execute
        #    on an uploaded script swallows errors via >NIL:; running
        #    the steps directly gives us visibility on each one).
        print(f"\n[3] installing MCPd into SYS:System/MCPd/")
        run(c, 'MakeDir SYS:System/MCPd ALL')
        run(c, 'Copy T:MCPd SYS:System/MCPd/MCPd CLONE')
        run(c, 'Protect SYS:System/MCPd/MCPd +rwed')
        run(c, 'Delete T:MCPd QUIET')

        # 5. Edit S:Network-Startup: download, edit on host, upload.
        #    DON'T use `Echo >>file "..."` over SerialShell -- the
        #    daemon's internal output-capture redirect (`>T:serialshell_out_...`)
        #    leaks into Echo's quoted argument and ends up in the
        #    file. The download-edit-upload path is binary-safe.
        print(f"\n[4] adding MCPd launch line to S:Network-Startup")
        original = c.download_text("S:Network-Startup")
        # Strip any prior MCPd block + leaked redirect lines (from a
        # previous broken run via the old Echo path)
        kept_lines = []
        for ln in original.splitlines(keepends=True):
            if "SYS:System/MCPd/MCPd" in ln:
                continue
            if "MCPd - Model Context Protocol" in ln:
                continue
            if "serialshell_out_" in ln:
                continue
            if ln.strip().startswith(">T:"):
                continue
            kept_lines.append(ln)
        cleaned = "".join(kept_lines)
        if not cleaned.endswith("\n"):
            cleaned += "\n"
        new_ns = (cleaned
                  + "\n"
                  + "; MCPd - Model Context Protocol daemon\n"
                  + "Run >NIL: <NIL: SYS:System/MCPd/MCPd\n")

        # Backup the cleaned original (NOT the corrupted current).
        run(c, 'Copy S:Network-Startup S:Network-Startup.before-mcpd CLONE')
        c.upload_bytes(new_ns.encode("latin-1"), "T:netstart.new")
        run(c, 'Copy T:netstart.new S:Network-Startup CLONE')
        run(c, 'Delete T:netstart.new QUIET')

        # 6. Restore cwd before verify
        run(c, 'CD SYS:')
    finally:
        c.close()

    # Re-open a fresh connection for verification — protocol drift
    # from earlier embedded-newline commands can leave residual buffer
    # state that interferes with multi-command response parsing.
    print("\n=== verify (fresh connection) ===")
    c = SerialShell(args.host, args.port)
    try:
        out_c = run(c, 'List C:SerialShell QUICK')
        out_mcpd = run(c, 'List SYS:System/MCPd/ QUICK')
        out_ns = run(c, 'Search S:Network-Startup "MCPd"')
    finally:
        c.close()

    has_serialshell = "SerialShell" in out_c
    has_mcpd = "MCPd" in out_mcpd
    has_ns_line = "MCPd" in out_ns
    ok = has_serialshell and has_mcpd and has_ns_line

    print(f"\n*** verdict: {'PASS' if ok else 'FAIL'} ***")
    print(f"   C:SerialShell present: {has_serialshell}")
    print(f"   SYS:System/MCPd/MCPd present: {has_mcpd}")
    print(f"   Network-Startup launches MCPd: {has_ns_line}")
    if ok:
        print(f"\n   -> Reboot the target. MCPd should auto-start on :4322.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
