"""Recover A1222 S:Network-Startup after the Echo>>SerialShell collision.

The earlier install script used `Echo >>T:foo "..."` to append lines.
SerialShell's internal output capture (`>T:serialshell_out_...`)
collided with the Echo's quoted string, so the appended lines ended
up containing the SerialShell capture-redirect path:

  Run >NIL: <NIL: SYS:System/MCPd/MCPd >T:serialshell_out_0x...

This script:
  1. Downloads S:Network-Startup.before-mcpd (the clean backup taken
     before the first edit) to the host.
  2. Appends a clean MCPd launch line on the host (no SerialShell
     redirection involved).
  3. Uploads the result back as S:Network-Startup via SERIALSHELL_UPLOAD
     (binary-safe — no Echo collision possible).
"""

from __future__ import annotations

import argparse
import sys

sys.path.insert(0, "scripts")

from install_mcpd_via_serialshell import SerialShell, run


MCPD_BLOCK = (
    "\n"
    "; MCPd - Model Context Protocol daemon\n"
    "Run >NIL: <NIL: SYS:System/MCPd/MCPd\n"
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=4321)
    args = ap.parse_args()

    print(f"connecting to SerialShell @ {args.host}:{args.port}...")
    c = SerialShell(args.host, args.port)
    try:
        # 1. Download the clean backup
        print("\n[1] downloading S:Network-Startup.before-mcpd")
        try:
            backup_text = c.download_text("S:Network-Startup.before-mcpd")
        except Exception as e:
            print(f"    backup download failed: {e}")
            print("    falling back to current S:Network-Startup")
            backup_text = c.download_text("S:Network-Startup")
        print(f"    {len(backup_text)}B")

        # 2. Sanity-check: no leaked redirects + no duplicate MCPd lines
        if "serialshell_out_" in backup_text:
            print("    WARNING: backup ALREADY has leaked redirects -- "
                  "you may need a manual recovery")
        if backup_text.count("SYS:System/MCPd/MCPd") > 0:
            print("    backup already contains an MCPd launch line "
                  "(stripping all of them and re-adding cleanly)")
            # Strip any prior MCPd block (lines mentioning the binary
            # path or the comment header) so we can append exactly one
            # clean copy.
            lines = backup_text.splitlines(keepends=True)
            kept = []
            for ln in lines:
                stripped = ln.strip()
                if "SYS:System/MCPd/MCPd" in ln:
                    continue
                if "MCPd - Model Context Protocol" in ln:
                    continue
                # Leaked SerialShell capture-redirect line (orphan from
                # the earlier Echo collision)
                if "serialshell_out_" in ln:
                    continue
                if stripped.startswith(">T:"):
                    continue
                kept.append(ln)
            backup_text = "".join(kept)

        # 3. Append clean MCPd block
        if not backup_text.endswith("\n"):
            backup_text += "\n"
        new_text = backup_text + MCPD_BLOCK

        # 4. Upload as new S:Network-Startup
        print(f"\n[2] uploading {len(new_text)}B as S:Network-Startup")
        # Write to a temp first, then rename -- so a partial upload
        # doesn't trash the live file.
        c.upload_bytes(new_text.encode("latin-1"), "T:netstart.new")

        # 5. Atomic-ish swap
        run(c, "Copy T:netstart.new S:Network-Startup CLONE")
        run(c, "Delete T:netstart.new QUIET")
    finally:
        c.close()

    # Verify on a fresh connection
    print("\n=== verify ===")
    c = SerialShell(args.host, args.port)
    try:
        body = c.download_text("S:Network-Startup")
    finally:
        c.close()

    leaks = body.count("serialshell_out_")
    runs = body.count("SYS:System/MCPd/MCPd")
    print(f"    bytes:                       {len(body)}")
    print(f"    leaked serialshell_out lines: {leaks}")
    print(f"    MCPd launch line count:      {runs}")
    print()
    print("--- last 12 lines ---")
    for line in body.splitlines()[-12:]:
        print(f"    {line}")

    ok = leaks == 0 and runs == 1
    print(f"\n*** verdict: {'PASS' if ok else 'FAIL'} ***")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
