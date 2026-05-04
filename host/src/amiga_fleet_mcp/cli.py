"""amiga-fleet-mcp CLI.

Consolidates the ad-hoc probe / deploy / benchmark scripts that were
scattered across ``C:\\tmp\\*.py`` into one entry point.
Run ``python -m amiga_fleet_mcp.cli --help`` to list subcommands.

Most subcommands accept ``--target NAME``; if omitted, falls back to
``server.default_target`` from config.toml.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import sys
import time
from pathlib import Path

from .config import Config, load_config
from .fleet import Fleet

# ---------- shared bootstrap --------------------------------------


def _make_fleet(args: argparse.Namespace) -> tuple[Fleet, Config]:
    cfg = load_config(Path(args.config) if args.config else None)
    fleet = Fleet(cfg)
    return fleet, cfg


def _resolve(fleet: Fleet, name: str | None) -> str:
    return fleet.resolve_target(name)


# ---------- subcommand: health -------------------------------------


async def cmd_health(args: argparse.Namespace) -> int:
    fleet, _cfg = _make_fleet(args)
    target = _resolve(fleet, args.target)
    print(f"[{time.strftime('%H:%M:%S')}] {target} probe ...")

    from .tools import fleet as ftool
    from .tools import sys as stool

    st = await ftool.fleet_target_status(fleet, target)
    print(f"  reachable: mcpd={st.mcpd_reachable}  "
          f"qmp={st.qmp_reachable}  qemu={st.qemu_running}")
    if not st.mcpd_reachable:
        print("  *** MCPd unreachable - aborting deeper probes ***")
        await fleet.close_all()
        return 2

    try:
        up = await stool.sys_uptime(fleet, target)
        print(f"  uptime: {up.seconds:.1f}s")
        la = await stool.sys_lastalert(fleet, target)
        print(f"  last_alert: 0x{la.alert_code:x}")
        mem = await stool.sys_memory(fleet, target)
        free_mb = mem.any.free / 1024 / 1024
        total_mb = mem.any.total / 1024 / 1024
        print(f"  free RAM: {free_mb:.0f} MiB / {total_mb:.0f} MiB total")
    except Exception as e:
        print(f"  probe failed: {e}")
        await fleet.close_all()
        return 1

    await fleet.close_all()
    return 0


# ---------- subcommand: snapshot -----------------------------------


async def cmd_snapshot(args: argparse.Namespace) -> int:
    fleet, _cfg = _make_fleet(args)
    from .tools import fleet as ftool

    res = await ftool.fleet_snapshot(fleet, tags=args.tags)
    for t in res.targets:
        line = f"{t.name:20s} type={t.type:8s}"
        if t.error:
            line += f"  ERROR: {t.error}"
        else:
            line += f"  mcpd={t.mcpd_reachable!s:5s}"
            if t.uptime_s is not None:
                line += f"  up={t.uptime_s:.0f}s"
            if t.free_ram_mb is not None:
                line += f"  free={t.free_ram_mb:.0f}MiB"
            if t.last_alert is not None:
                line += f"  alert=0x{t.last_alert:x}"
        print(line)
    await fleet.close_all()
    return 0


# ---------- subcommand: deploy-mcpd --------------------------------


async def cmd_deploy_mcpd(args: argparse.Namespace) -> int:
    fleet, _cfg = _make_fleet(args)
    target = _resolve(fleet, args.target)

    binary_path = Path(args.binary)
    if not binary_path.is_file():
        print(f"binary not found: {binary_path}")
        return 1
    binary = binary_path.read_bytes()
    print(f"deploying {len(binary)} bytes from {binary_path}")

    from .tools import exec as etool
    from .tools import fs as fstool

    guest_path = args.path
    print("\n[stat] existing daemon on disk:")
    try:
        st = await fstool.fs_stat(fleet, target, guest_path)
        print(f"  {st.name}: {st.size} bytes, modified {st.modified}")
    except Exception as e:
        print(f"  (none / {e})")

    print(f"\n[upload] {len(binary)} bytes -> {guest_path}")
    t0 = time.monotonic()
    await fstool.fs_write(
        fleet, target, guest_path,
        base64.b64encode(binary).decode("ascii"),
    )
    elapsed = time.monotonic() - t0
    print(f"  PASS in {elapsed:.1f}s "
          f"({len(binary)/1024/elapsed:.0f} KiB/s)")

    print("\n[protect] +rwed")
    r = await etool.exec_cmd(
        fleet, target,
        f'Protect "{guest_path}" +rwed',
    )
    print(f"  exit={r.exit_code}")

    print()
    print("Reboot the target to activate the new daemon "
          "(Network-Startup will Run it from disk).")
    await fleet.close_all()
    return 0


# ---------- subcommand: upload -------------------------------------


async def cmd_upload(args: argparse.Namespace) -> int:
    fleet, _cfg = _make_fleet(args)
    target = _resolve(fleet, args.target)
    from .upload import chunked_upload

    print(f"upload {args.local} -> {target}:{args.remote}")
    t0 = time.monotonic()
    stats = await chunked_upload(
        fleet, target, args.local, args.remote,
        chunk_size=args.chunk_size,
        compression="auto" if args.compression == "auto" else args.compression,
    )
    elapsed = time.monotonic() - t0
    print(f"  {stats.bytes_total/1024/1024:.1f} MiB in "
          f"{elapsed:.1f}s = {stats.bytes_total/1024/1024/elapsed:.2f} MiB/s")
    print(f"  chunks={stats.chunks}  compressed={stats.compressed_chunks}  "
          f"compression_ratio={stats.compression_ratio:.2%}")

    if args.verify:
        print("[verify] sha256 round-trip")
        from .tools import fs as fstool
        local_sha = hashlib.sha256(Path(args.local).read_bytes()).hexdigest()
        h = await fstool.fs_hash(fleet, target, args.remote, algo="sha256")
        if h.hash == local_sha:
            print(f"  MATCH: {h.hash[:16]}...")
        else:
            print(f"  MISMATCH local={local_sha[:16]}... "
                  f"remote={h.hash[:16]}...")
            await fleet.close_all()
            return 1
    await fleet.close_all()
    return 0


# ---------- subcommand: events-soak --------------------------------


async def cmd_events_soak(args: argparse.Namespace) -> int:
    fleet, _cfg = _make_fleet(args)
    target = _resolve(fleet, args.target)
    from .tools import events as etool
    from .tools import sys as stool

    # Warmup is now in the transport (#4) but a few visible iters are
    # still useful for the operator to see the daemon come up cleanly.
    for i in range(3):
        try:
            await stool.sys_uptime(fleet, target)
            print(f"  warmup {i+1}/3 ok")
        except Exception as e:
            print(f"  warmup {i+1}/3 dropped: {e}")
        await asyncio.sleep(0.5)

    print(f"[soak] {args.iterations} events.wait calls "
          f"timeout_ms={args.timeout_ms}")
    fails = drops = 0
    latencies: list[float] = []
    t0 = time.monotonic()
    for i in range(args.iterations):
        ts = time.monotonic()
        try:
            await etool.events_wait(
                fleet, target,
                topics=["sys.lastalert"],
                timeout_ms=args.timeout_ms,
            )
            latencies.append((time.monotonic() - ts) * 1000)
        except Exception as e:
            drops += 1
            print(f"  [{i+1:3d}] drop: {e}")
        if (i+1) % 10 == 0 and latencies:
            print(f"  [{i+1:3d}] ok  med={sorted(latencies)[len(latencies)//2]:.0f}ms")
    elapsed = time.monotonic() - t0

    ok = args.iterations - fails - drops
    print("\n=== events.wait soak ===")
    print(f"  ok:     {ok}/{args.iterations}")
    print(f"  fails:  {fails}")
    print(f"  drops:  {drops}")
    if latencies:
        print(f"  latency ms: min={min(latencies):.0f}  "
              f"med={sorted(latencies)[len(latencies)//2]:.0f}  "
              f"max={max(latencies):.0f}")
    print(f"  total time: {elapsed:.1f}s")

    await fleet.close_all()
    return 0 if (fails == 0 and drops == 0) else 1


# ---------- argparse glue ------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="amiga_fleet_mcp.cli",
                                description=__doc__)
    p.add_argument("--config", help="path to config.toml (defaults to "
                                    "user-scope path)")
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("health", help="probe a target's basic vitals")
    h.add_argument("--target", help="target name (else server.default_target)")
    h.set_defaults(func=cmd_health)

    s = sub.add_parser("snapshot", help="parallel health probe across the fleet")
    s.add_argument("--tags", nargs="+", help="restrict to targets with all "
                                              "these tags")
    s.set_defaults(func=cmd_snapshot)

    d = sub.add_parser("deploy_mcpd", help="upload an MCPd binary to the target")
    d.add_argument("--target", help="target name")
    d.add_argument("--binary", default=str(
        Path(__file__).parent.parent.parent.parent / "mcpd" / "MCPd"
    ), help="local MCPd binary (defaults to repo's mcpd/MCPd)")
    d.add_argument("--path", default="SYS:System/MCPd/MCPd",
                   help="guest path")
    d.set_defaults(func=cmd_deploy_mcpd)

    u = sub.add_parser("upload", help="chunked upload a local file")
    u.add_argument("local", help="local file path")
    u.add_argument("remote", help="remote (Amiga) path")
    u.add_argument("--target", help="target name")
    u.add_argument("--chunk-size", type=int, default=10 * 1024 * 1024,
                   help="bytes per chunk (default 10 MiB)")
    u.add_argument("--compression", choices=["auto", "none", "zlib"],
                   default="auto")
    u.add_argument("--verify", action="store_true",
                   help="round-trip sha256 after upload")
    u.set_defaults(func=cmd_upload)

    e = sub.add_parser("events_soak",
                        help="N rounds of events.wait to soak the daemon")
    e.add_argument("--target", help="target name")
    e.add_argument("--iterations", type=int, default=100)
    e.add_argument("--timeout-ms", type=int, default=1000,
                   dest="timeout_ms")
    e.set_defaults(func=cmd_events_soak)

    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return asyncio.run(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
