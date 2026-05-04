"""MCP server entry + tool registration.

Wires the JSON-RPC method surface into FastMCP. Tool names are
flattened (`fs_list`, etc.) for the MCP layer; the dotted JSON-RPC
names (`fs.list`, `fs.stat`, ...) live in the proto.capabilities
advertisement.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from . import __version__
from .archive import Archive
from .config import Config, load_config
from .errors import JsonRpcError, MethodNotFound
from .fleet import Fleet
from .resources import archive_resources
from .tools import debug as debug_tool
from .tools import events as events_tool
from .tools import exec as exec_tool
from .tools import fleet as fleet_tool
from .tools import fleet_discover as discover_tool
from .tools import fs as fs_tool
from .tools import installer as installer_tool
from .tools import power as power_tool
from .tools import qemu as qemu_tool
from .tools import serial as serial_tool
from .tools import snapshots as snap_tool
from .tools import sys as sys_tool
from .tools import tests as tests_tool
from .tools import wb as wb_tool
from .tools._helpers import archived

SERVER_NAME = "amiga-fleet"
log = logging.getLogger("amiga-fleet-mcp")


def build_server(
    fleet: Fleet,
    archive: Archive,
    *,
    name: str = SERVER_NAME,
) -> FastMCP:
    """Construct a FastMCP instance with phase-1 tools registered."""
    mcp = FastMCP(name)
    register_tools(mcp, fleet, archive)
    return mcp


def register_tools(mcp: FastMCP, fleet: Fleet, archive: Archive) -> None:
    def _default(value: str | None, attr: str, param_name: str) -> str:
        """If `value` is set (not None / not empty), return it. Else
        fall back to fleet.config.defaults.<attr>. Raise InvalidParams
        with a clear message if neither is set."""
        if value:
            return value
        d = getattr(fleet.config.defaults, attr, None)
        if d:
            return str(d)
        from .errors import InvalidParams as _IP
        raise _IP(
            f"missing required parameter {param_name!r}; pass it "
            f"explicitly or set `[defaults] {attr} = ...` in your "
            f"config.toml.",
        )

    @mcp.tool(name="proto_capabilities", title="proto.capabilities")
    @archived("proto.capabilities", archive, target_arg="_unused_")
    async def proto_capabilities() -> sys_tool.CapabilitiesResult:
        """Advertise the methods the host MCP server has wired.

        Method list is auto-derived from the live FastMCP tool registry
        - whatever is currently registered with @mcp.tool(...,
        title="...") gets advertised. No hand-maintained list to drift
        out of date.
        """
        registered = await mcp.list_tools()
        # Each registered tool's `title` is the dotted JSON-RPC method
        # name (e.g. "fs.write_chunk"); fall back to the underscored
        # `name` if no title was set.
        methods = [t.title or t.name for t in registered]
        return sys_tool.host_capabilities(fleet, __version__, methods)

    @mcp.tool(name="fs_list", title="fs.list")
    @archived("fs.list", archive)
    async def fs_list(
        *,
        target: str | None = None,
        path: str,
        recursive: bool = False,
        max_depth: int = 8,
    ) -> fs_tool.FsListResult:
        """List a directory on a target Amiga.

        If `recursive` is true, descends up to `max_depth` levels (1-16).
        """
        return await fs_tool.fs_list(
            fleet, fleet.resolve_target(target), path, recursive=recursive, max_depth=max_depth,
        )

    @mcp.tool(name="fs_stat", title="fs.stat")
    @archived("fs.stat", archive)
    async def fs_stat(*, target: str | None = None, path: str) -> fs_tool.StatResult:
        """Stat one file or directory."""
        return await fs_tool.fs_stat(fleet, fleet.resolve_target(target), path)

    @mcp.tool(name="fs_read", title="fs.read")
    @archived("fs.read", archive)
    async def fs_read(
        *,
        target: str | None = None,
        path: str,
        offset: int | None = None,
        length: int | None = None,
    ) -> fs_tool.FsReadResult:
        """Read a file. Returns size + base64 content. If `offset`
        and/or `length` are given, returns just that slice and includes
        `total_size` in the result for chunked reads of large files."""
        return await fs_tool.fs_read(
            fleet, fleet.resolve_target(target), path, offset=offset, length=length,
        )

    @mcp.tool(name="fs_write", title="fs.write")
    @archived("fs.write", archive)
    async def fs_write(
        *,
        target: str | None = None, path: str, content_b64: str,
        compression: str = "none",
        raw_size: int | None = None,
    ) -> fs_tool.FsWriteResult:
        """Write a file in one shot from base64-encoded bytes.

        Set compression='zlib' to send zlib-compressed bytes
        (pair with raw_size, the decompressed length, so the daemon
        pre-allocates exactly). For files larger than ~24 MiB raw
        use fs_write_chunk instead - the JSON-RPC frame cap is 32 MiB
        post-base64."""
        return await fs_tool.fs_write(
            fleet, fleet.resolve_target(target), path, content_b64,
            compression="zlib" if compression == "zlib" else "none",
            raw_size=raw_size,
        )

    @mcp.tool(name="fs_write_chunk", title="fs.write_chunk")
    @archived("fs.write_chunk", archive)
    async def fs_write_chunk(
        *,
        target: str | None = None, path: str, offset: int, content_b64: str,
        compression: str = "none",
        raw_size: int | None = None,
        truncate: bool | None = None,
        total_size: int | None = None,
    ) -> fs_tool.FsWriteChunkResult:
        """Write a chunk of a file at the given byte offset.

        Symmetric with fs_read's offset/length slicing. First chunk
        (offset=0) truncates by default; subsequent chunks seek-and-
        write. Pass compression='zlib' (with raw_size) to push
        compressed chunks. Pass total_size on the first chunk to let
        the daemon pre-extend the file. Resumable: a partial file
        from a failed upload stays on disk; retry with the appropriate
        next offset."""
        return await fs_tool.fs_write_chunk(
            fleet, fleet.resolve_target(target), path, offset, content_b64,
            compression="zlib" if compression == "zlib" else "none",
            raw_size=raw_size,
            truncate=truncate,
            total_size=total_size,
        )

    @mcp.tool(name="fs_delete", title="fs.delete")
    @archived("fs.delete", archive)
    async def fs_delete(
        *,
        target: str | None = None, path: str, recursive: bool = False,
    ) -> fs_tool.FsOk:
        """Delete one path. With `recursive=True`, removes a tree
        (shells to AmigaDOS `Delete ALL QUIET` on the target)."""
        return await fs_tool.fs_delete(
            fleet, fleet.resolve_target(target), path, recursive=recursive,
        )

    @mcp.tool(name="fs_makedir", title="fs.makedir")
    @archived("fs.makedir", archive)
    async def fs_makedir(*, target: str | None = None, path: str) -> fs_tool.FsOk:
        """Create a directory."""
        return await fs_tool.fs_makedir(fleet, fleet.resolve_target(target), path)

    @mcp.tool(name="fs_rename", title="fs.rename")
    @archived("fs.rename", archive)
    async def fs_rename(
        *,
        target: str | None = None, src: str, dst: str
    ) -> fs_tool.FsRenameResult:
        """Rename / move a file or directory (IDOS->Rename)."""
        return await fs_tool.fs_rename(fleet, fleet.resolve_target(target), src, dst)

    @mcp.tool(name="fs_protect", title="fs.protect")
    @archived("fs.protect", archive)
    async def fs_protect(
        *,
        target: str | None = None, path: str, bits: int
    ) -> fs_tool.FsProtectResult:
        """Set protection bits via IDOS->SetProtection. `bits` is the
        raw 32-bit AmigaDOS protection mask."""
        return await fs_tool.fs_protect(fleet, fleet.resolve_target(target), path, bits)

    @mcp.tool(name="fs_copy", title="fs.copy")
    @archived("fs.copy", archive)
    async def fs_copy(
        *,
        target: str | None = None, src: str, dst: str
    ) -> fs_tool.FsCopyResult:
        """Copy a file (`Copy ... CLONE` preserves protection + date)."""
        return await fs_tool.fs_copy(fleet, fleet.resolve_target(target), src, dst)

    @mcp.tool(name="fs_hash", title="fs.hash")
    @archived("fs.hash", archive)
    async def fs_hash(
        *,
        target: str | None = None, path: str, algo: str = "sha256",
    ) -> fs_tool.FsHashResult:
        """Streaming SHA-256 of a file (only sha256 supported for now)."""
        return await fs_tool.fs_hash(fleet, fleet.resolve_target(target), path, algo)

    @mcp.tool(name="exec_cmd", title="exec.cmd")
    @archived("exec.cmd", archive)
    async def exec_cmd(
        *,
        target: str | None = None,
        command: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        timeout_s: float = 30.0,
    ) -> exec_tool.ExecResult:
        """Run an AmigaDOS command, capture stdout. `args` (if given)
        are appended with conservative AmigaDOS quoting; `cwd` (if
        given) is locked + swapped in for the duration of the call."""
        return await exec_tool.exec_cmd(
            fleet, fleet.resolve_target(target), command,
            args=args, cwd=cwd, timeout_s=timeout_s,
        )

    @mcp.tool(name="sys_version", title="sys.version")
    @archived("sys.version", archive)
    async def sys_version(*, target: str | None = None) -> sys_tool.VersionResult:
        """Run `Version FULL` on the target, parse Kickstart / Workbench."""
        return await sys_tool.sys_version(fleet, fleet.resolve_target(target))

    @mcp.tool(name="sys_tasks", title="sys.tasks")
    @archived("sys.tasks", archive)
    async def sys_tasks(*, target: str | None = None) -> sys_tool.TasksResult:
        """List ready + waiting tasks on the target Amiga."""
        return await sys_tool.sys_tasks(fleet, fleet.resolve_target(target))

    @mcp.tool(name="sys_libraries", title="sys.libraries")
    @archived("sys.libraries", archive)
    async def sys_libraries(*, target: str | None = None) -> sys_tool.LibrariesResult:
        """List opened libraries (name, version, revision, open count)."""
        return await sys_tool.sys_libraries(fleet, fleet.resolve_target(target))

    @mcp.tool(name="sys_devices", title="sys.devices")
    @archived("sys.devices", archive)
    async def sys_devices(*, target: str | None = None) -> sys_tool.DevicesResult:
        """List opened devices (same shape as libraries)."""
        return await sys_tool.sys_devices(fleet, fleet.resolve_target(target))

    @mcp.tool(name="sys_ports", title="sys.ports")
    @archived("sys.ports", archive)
    async def sys_ports(*, target: str | None = None) -> sys_tool.PortsResult:
        """List public message ports."""
        return await sys_tool.sys_ports(fleet, fleet.resolve_target(target))

    @mcp.tool(name="sys_lastalert", title="sys.lastalert")
    @archived("sys.lastalert", archive)
    async def sys_lastalert(*, target: str | None = None) -> sys_tool.LastAlertResult:
        """ExecBase->LastAlert (most recent system alert code, plus
        the four-word raw payload)."""
        return await sys_tool.sys_lastalert(fleet, fleet.resolve_target(target))

    @mcp.tool(name="sys_uptime", title="sys.uptime")
    @archived("sys.uptime", archive)
    async def sys_uptime(*, target: str | None = None) -> sys_tool.UptimeResult:
        """Monotonic uptime via ITimer->ReadEClock."""
        return await sys_tool.sys_uptime(fleet, fleet.resolve_target(target))

    @mcp.tool(name="sys_memory", title="sys.memory")
    @archived("sys.memory", archive)
    async def sys_memory(*, target: str | None = None) -> sys_tool.MemoryResult:
        """Free / largest / total memory across MEMF_ANY / SHARED / VIRTUAL."""
        return await sys_tool.sys_memory(fleet, fleet.resolve_target(target))

    @mcp.tool(name="sys_volumes", title="sys.volumes")
    @archived("sys.volumes", archive)
    async def sys_volumes(*, target: str | None = None) -> sys_tool.VolumesResult:
        """Mounted volumes (LDF_VOLUMES walk)."""
        return await sys_tool.sys_volumes(fleet, fleet.resolve_target(target))

    @mcp.tool(name="sys_assigns", title="sys.assigns")
    @archived("sys.assigns", archive)
    async def sys_assigns(*, target: str | None = None) -> sys_tool.AssignsResult:
        """Active AmigaDOS assigns (LDF_ASSIGNS walk)."""
        return await sys_tool.sys_assigns(fleet, fleet.resolve_target(target))

    @mcp.tool(name="sys_hardware", title="sys.hardware")
    @archived("sys.hardware", archive)
    async def sys_hardware(*, target: str | None = None) -> sys_tool.HardwareResult:
        """CPU details (GetCPUInfo: family, model, speed, caches),
        AttnFlags, and presence probes for xena/i2c/acpi/performance
        monitor/fsldma resources. Backs proto.capabilities.board."""
        return await sys_tool.sys_hardware(fleet, fleet.resolve_target(target))

    @mcp.tool(name="sys_hardware_i2c", title="sys.hardware.i2c")
    @archived("sys.hardware.i2c", archive)
    async def sys_hardware_i2c(
        *,
        target: str | None = None, addr_low: int = 0x08, addr_high: int = 0x77,
    ) -> sys_tool.I2CResult:
        """Enumerate I2C buses + probe addresses via i2c.resource.
        Returns one entry per bus with its probed devices."""
        return await sys_tool.sys_hardware_i2c(
            fleet, fleet.resolve_target(target), addr_low=addr_low, addr_high=addr_high,
        )

    @mcp.tool(name="sys_hardware_perfcounters",
              title="sys.hardware.perfcounters")
    @archived("sys.hardware.perfcounters", archive)
    async def sys_hardware_perfcounters(
        *,
        target: str | None = None,
    ) -> sys_tool.PerfCountersResult:
        """Live CPU performance-monitor counters via
        performancemonitor.resource (cycles, instr, L1/L2 hits/misses)."""
        return await sys_tool.sys_hardware_perfcounters(fleet, fleet.resolve_target(target))

    @mcp.tool(name="sys_executable_symbols",
              title="sys.executable.symbols")
    @archived("sys.executable.symbols", archive)
    async def sys_executable_symbols(
        *,
        target: str | None = None,
        path: str,
        match: str | None = None,
        binding: str | None = None,
        type: str | None = None,
        max: int = 256,
    ) -> sys_tool.ExecutableSymbolsResult:
        """Read the ELF symbol table of `path` on the target. Optional
        filters: name substring, binding (local/global/weak),
        type (notype/object/func/section/file). max caps the result
        (default 256, hard ceiling 4096)."""
        return await sys_tool.sys_executable_symbols(
            fleet, fleet.resolve_target(target), path,
            match=match, binding=binding, type=type, max=max,
        )

    @mcp.tool(name="sys_applications", title="sys.applications")
    @archived("sys.applications", archive)
    async def sys_applications(*, target: str | None = None) -> sys_tool.ApplicationsResult:
        """List applications registered with application.library v53+
        (GetApplicationList walk + per-app GetApplicationAttrs)."""
        return await sys_tool.sys_applications(fleet, fleet.resolve_target(target))

    @mcp.tool(name="sys_alert_decode", title="sys.alert_decode")
    @archived("sys.alert_decode", archive)
    async def sys_alert_decode(
        *,
        target: str | None = None, code: int,
    ) -> sys_tool.AlertDecodeResult:
        """Decode an arbitrary AmigaOS alert code per the format
        in exec/alerts.h: subsystem / general / specific."""
        return await sys_tool.sys_alert_decode(fleet, fleet.resolve_target(target), code)

    @mcp.tool(name="sys_cold_reboot", title="sys.cold_reboot")
    @archived("sys.cold_reboot", archive)
    async def sys_cold_reboot(
        *,
        target: str | None = None, confirm: bool, delay_ms: int = 500,
    ) -> sys_tool.ColdRebootResult:
        """Software cold reboot of the guest via IExec->ColdReboot.
        Requires confirm=true (gate against accidents). Daemon spawns
        a delayed-reboot subtask so the RPC response flushes before
        the machine resets; delay_ms is clamped daemon-side to
        [100, 5000]. Filesystems are NOT synced - intended for
        recovery from a wedged guest under autonomous test loops."""
        return await sys_tool.sys_cold_reboot(
            fleet, fleet.resolve_target(target), confirm, delay_ms=delay_ms,
        )

    @mcp.tool(name="sys_read_ccsr", title="sys.read_ccsr")
    @archived("sys.read_ccsr", archive)
    async def sys_read_ccsr(
        *,
        target: str | None = None, offset: int, count: int = 1,
    ) -> sys_tool.ReadCcsrResult:
        """Read 32-bit register(s) from the Freescale QorIQ
        Configuration, Control and Status Register (CCSR) at CPU PA
        `0xFE000000 + offset`. P5020 (X5000) / P1022 (A1222) only.

        Supervisor-mode read on the daemon side; safe to call on a
        live system. Used to probe SoC internal registers — PCIe
        outbound translation windows (PEXOWAR0/PEXOTAR0), Local
        Access Window registers (LAW_BARH/L/AR), DDR controller
        registers, etc — when bringing up a QEMU machine model or
        debugging hardware setup.

        offset must be 4-byte aligned; count is the number of
        consecutive 32-bit reads (max 256, i.e. 1 KiB)."""
        return await sys_tool.sys_read_ccsr(
            fleet, fleet.resolve_target(target),
            offset=offset, count=count,
        )

    @mcp.tool(name="sys_mcu_cmd", title="sys.mcu_cmd")
    @archived("sys.mcu_cmd", archive)
    async def sys_mcu_cmd(
        *, target: str | None = None,
        cmd: str, unit: int = 1, confirm: bool = False,
    ) -> sys_tool.McuCmdResult:
        """X5000 Cyrus MCU serial supervisor protocol (TRM 8.1).
        Opens AOS serial.device at the given unit (default 1 = UART1
        connected to the on-board MCU), sends '#cmd\\n' at 38400 8N1,
        reads back '$reply\\n'. 5 s timeout.

        Documented commands: 't' temperatures, 'v' voltages, 'f' fan,
        's' POWER OFF (requires confirm=True; daemon rejects without
        it). Returns raw reply string + parsed structured fields.
        The canonical sensor path on X5000."""
        return await sys_tool.sys_mcu_cmd(
            fleet, fleet.resolve_target(target),
            cmd=cmd, unit=unit, confirm=confirm,
        )

    @mcp.tool(name="sys_read_pa", title="sys.read_pa")
    @archived("sys.read_pa", archive)
    async def sys_read_pa(
        *, target: str | None = None,
        pa: int, count: int = 1, width: int = 4,
    ) -> sys_tool.ReadPaResult:
        """DANGEROUS supervisor-mode read at any 36-bit PA.

        *** WRONG pa CRASHES THE CONNECTION-HANDLER TASK *** (DSI in
        supervisor mode). The daemon's main listener and other
        clients' connections survive (MCPd is spawn-per-connection
        for exactly this reason), but THIS RPC's connection drops
        mid-call -- the client must reconnect.

        width = 1 (byte), 2 (halfword), 4 (word). pa aligned to
        width; count*width <= 4096. Returns values, values_hex,
        bytes_b64.

        SAFE regions on real X5000 (verified live):
          - 0xFE000000..0xFEFFFFFF (CCSR — all peripheral registers)
          - 0x01000000..0x0246FFFF (kernel-IPROT'd DMA buffers)

        UNSAFE / will trap on supervisor read despite TLB1 mapping:
          - 0x00000000..0x00FFFFFF (kernel-critical low memory)
          - 0x40000000..0x7FFFFFFF (entry 63 — partial coverage,
            many addresses crash)
          - 0x80000000..0xFDFFFFFF and 0xFF000000..0xFFFFFFFF
            (entirely unmapped)

        Run sys.tlb_dump first if unsure; even mapped regions can
        trap kernel-side. Recovery: catch the connection error on
        the client side, reconnect, log the bad pa for next time."""
        return await sys_tool.sys_read_pa(
            fleet, fleet.resolve_target(target),
            pa=pa, count=count, width=width,
        )

    @mcp.tool(name="sys_tlb_dump", title="sys.tlb_dump")
    @archived("sys.tlb_dump", archive)
    async def sys_tlb_dump(
        *, target: str | None = None,
    ) -> sys_tool.TlbDumpResult:
        """Dump all 64 TLB1 entries from a P5020 / P1022 target.
        TLB1 holds all kernel + driver mappings (including CCSR and
        PCI windows). Each entry includes raw MAS1/MAS2/MAS3/MAS7
        plus decoded V / IPROT / TID / TS / TSIZE / EPN / RPN
        (36-bit) / WIMG / permissions, and a `rpn_36bit_hex` field
        that combines RPNH | RPNL for the full physical address.

        Used to verify exactly what PA each kernel VA translates to
        (specifically: whether PCI I/O at VA 0xF8000000 maps to
        PA 0xF_F800_0000 in the 36-bit world). Read-only; safe."""
        return await sys_tool.sys_tlb_dump(fleet, fleet.resolve_target(target))

    # ---------- power.* (host-side P18 / P15 MCU debug shell) -----

    @mcp.tool(name="power_help", title="power.help")
    @archived("power.help", archive)
    async def power_help(
        *, target: str | None = None,
    ) -> power_tool.ShellReply:
        """List the MCU debug-shell commands (`help`). Talks to the
        host-side serial cable wired to the target's MCU header
        (X5000 P18 / A1222 P15) -- bypasses MCPd entirely. Requires
        `[targets.<name>.channels.mcu]` configured."""
        return await power_tool.power_help(
            fleet, fleet.resolve_target(target),
        )

    @mcp.tool(name="power_identify", title="power.identify")
    @archived("power.identify", archive)
    async def power_identify(
        *, target: str | None = None,
    ) -> power_tool.ShellReply:
        """MCU H/W + F/W revisions and build type (`id`)."""
        return await power_tool.power_identify(
            fleet, fleet.resolve_target(target),
        )

    @mcp.tool(name="power_identify_dates", title="power.identify_dates")
    @archived("power.identify_dates", archive)
    async def power_identify_dates(
        *, target: str | None = None,
    ) -> power_tool.ShellReply:
        """MCU + CPLD build date and time (`id date`)."""
        return await power_tool.power_identify_dates(
            fleet, fleet.resolve_target(target),
        )

    @mcp.tool(name="power_sensors", title="power.sensors")
    @archived("power.sensors", archive)
    async def power_sensors(
        *, target: str | None = None,
    ) -> power_tool.ShellReply:
        """One-shot voltage + temperature read via the debug shell
        (`v`). Reply is human-formatted ASCII; use `sys.mcu_cmd
        cmd="v"` instead for the wire `$vXXYY...` form parsed into
        structured fields."""
        return await power_tool.power_sensors(
            fleet, fleet.resolve_target(target),
        )

    @mcp.tool(name="power_toggle_stream", title="power.toggle_stream")
    @archived("power.toggle_stream", archive)
    async def power_toggle_stream(
        *, target: str | None = None,
        watch_s: float = 0.0, confirm: bool = False,
    ) -> Any:
        """Toggle the MCU's continuous-emission state (`q`).

        With `watch_s > 0`: toggle on, capture the stream for that
        many seconds, toggle back off, return the captured ASCII.
        With `watch_s = 0`: send `q` once and return the immediate
        reply -- caller is responsible for sending `q` again to
        disable.

        Hardware-destructive: requires `confirm=True`."""
        return await power_tool.power_toggle_stream(
            fleet, fleet.resolve_target(target),
            watch_s=watch_s, confirm=confirm,
        )

    @mcp.tool(name="power_on", title="power.on")
    @archived("power.on", archive)
    async def power_on(
        *, target: str | None = None, confirm: bool = False,
    ) -> power_tool.ShellReply:
        """Power up all supplies (`p`). Boots a powered-off X5000;
        **issues a hard reset if the box is already on**.

        The only software path to power an X5000 ON from a fully-off
        state. Hardware-destructive: requires `confirm=True`."""
        return await power_tool.power_on(
            fleet, fleet.resolve_target(target), confirm=confirm,
        )

    @mcp.tool(name="power_off", title="power.off")
    @archived("power.off", archive)
    async def power_off(
        *, target: str | None = None, confirm: bool = False,
    ) -> power_tool.ShellReply:
        """Shut down all supplies (`s`). Hardware-destructive:
        requires `confirm=True`."""
        return await power_tool.power_off(
            fleet, fleet.resolve_target(target), confirm=confirm,
        )

    @mcp.tool(name="power_shell", title="power.shell")
    @archived("power.shell", archive)
    async def power_shell(
        *, target: str | None = None,
        cmd: str, confirm: bool = False,
    ) -> power_tool.ShellReply:
        """Generic MCU debug-shell passthrough. Anything other than
        the documented `help` / `id` / `id date` / `v` / `q` / `p`
        / `s` commands is untested. Hardware-destructive (could be
        `s` etc.); requires `confirm=True`."""
        return await power_tool.power_shell(
            fleet, fleet.resolve_target(target),
            cmd=cmd, confirm=confirm,
        )

    @mcp.tool(name="app_notify", title="app.notify")
    @archived("app.notify", archive)
    async def app_notify(
        *,
        target: str | None = None, title: str, text: str, priority: int = 0,
    ) -> sys_tool.NotifyResult:
        """Post a Ringhio popup notification on the target via
        application.library NotifyA. Requires v53.11+."""
        return await sys_tool.app_notify(
            fleet, fleet.resolve_target(target), title, text, priority,
        )

    @mcp.tool(name="notify_fleet", title="notify.fleet")
    @archived("notify.fleet", archive)
    async def notify_fleet(
        title: str, text: str, priority: int = 0,
        targets: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> sys_tool.NotifyFleetResult:
        """Broadcast a Ringhio popup to every (selected) target in
        parallel. `targets` or `tags` narrow the broadcast; default
        is every configured target."""
        return await sys_tool.notify_fleet(
            fleet, title, text, priority, targets=targets, tags=tags,
        )

    @mcp.tool(name="notify_on_alert", title="notify.on_alert")
    @archived("notify.on_alert", archive)
    async def notify_on_alert(
        *,
        target: str | None = None, timeout_ms: int = 60000,
        title: str = "MCPd alert detected",
    ) -> sys_tool.NotifyOnAlertResult:
        """Long-poll the target's events.wait for a debug.exception
        topic, then post a Ringhio notification when one fires. The
        host-visible bridge between MCPd's event channel and the
        end-user."""
        return await sys_tool.notify_on_alert(
            fleet, fleet.resolve_target(target), timeout_ms=timeout_ms, title=title,
        )

    @mcp.tool(name="wb_screens", title="wb.screens")
    @archived("wb.screens", archive)
    async def wb_screens(*, target: str | None = None) -> wb_tool.ScreensResult:
        """List public screens with title / dimensions / window count."""
        return await wb_tool.wb_screens(fleet, fleet.resolve_target(target))

    @mcp.tool(name="wb_windows", title="wb.windows")
    @archived("wb.windows", archive)
    async def wb_windows(*, target: str | None = None) -> wb_tool.WindowsResult:
        """List all open windows across screens with their titles + geometry."""
        return await wb_tool.wb_windows(fleet, fleet.resolve_target(target))

    @mcp.tool(name="wb_publicscreens", title="wb.publicscreens")
    @archived("wb.publicscreens", archive)
    async def wb_publicscreens(*, target: str | None = None) -> wb_tool.PublicScreensResult:
        """Public-screen registry (LockPubScreenList walk)."""
        return await wb_tool.wb_publicscreens(fleet, fleet.resolve_target(target))

    @mcp.tool(name="wb_frontmost", title="wb.frontmost")
    @archived("wb.frontmost", archive)
    async def wb_frontmost(*, target: str | None = None) -> wb_tool.FrontmostResult:
        """Frontmost screen + active screen + active window."""
        return await wb_tool.wb_frontmost(fleet, fleet.resolve_target(target))

    # ---------- phase 5c (partial): GDB-stub debug --------------------

    @mcp.tool(name="debug_read_registers", title="debug.read_registers")
    @archived("debug.read_registers", archive)
    async def debug_read_registers(
        *,
        target: str | None = None,
    ) -> debug_tool.DebugReadRegistersResult:
        """Read CPU registers via QEMU's GDB-RSP stub. Whole-system view -
        not per-task. Requires the target to have a `gdb` channel
        configured + QEMU launched with `-gdb tcp::PORT`."""
        return await debug_tool.debug_read_registers(fleet, fleet.resolve_target(target))

    @mcp.tool(name="debug_read_memory", title="debug.read_memory")
    @archived("debug.read_memory", archive)
    async def debug_read_memory(
        *,
        target: str | None = None, addr: int, length: int,
    ) -> debug_tool.DebugReadMemoryResult:
        """Read a slice of guest physical memory via QEMU's GDB stub.
        Returns the bytes as base64. 64 KiB cap per request."""
        return await debug_tool.debug_read_memory(fleet, fleet.resolve_target(target), addr, length)

    @mcp.tool(name="debug_stop_reason", title="debug.stop_reason")
    @archived("debug.stop_reason", archive)
    async def debug_stop_reason(*, target: str | None = None) -> debug_tool.DebugStopReason:
        """Send `?` to the GDB stub and return the raw stop reply.
        Useful for sanity-checking stub connectivity."""
        return await debug_tool.debug_stop_reason(fleet, fleet.resolve_target(target))

    @mcp.tool(name="debug_detach", title="debug.detach")
    @archived("debug.detach", archive)
    async def debug_detach(*, target: str | None = None) -> debug_tool.DebugDetachResult:
        """Detach the GDB stub so the CPU resumes normal execution.
        Any subsequent debug.* call re-attaches (pausing the CPU)."""
        return await debug_tool.debug_detach(fleet, fleet.resolve_target(target))

    @mcp.tool(name="debug_set_breakpoint", title="debug.set_breakpoint")
    @archived("debug.set_breakpoint", archive)
    async def debug_set_breakpoint(
        *,
        target: str | None = None, addr: int, kind: int = 4, hardware: bool = False,
    ) -> debug_tool.DebugBreakpointResult:
        """Set a software (Z0) or hardware (Z1) breakpoint at `addr`."""
        return await debug_tool.debug_set_breakpoint(
            fleet, fleet.resolve_target(target), addr, kind=kind, hardware=hardware,
        )

    @mcp.tool(name="debug_clear_breakpoint", title="debug.clear_breakpoint")
    @archived("debug.clear_breakpoint", archive)
    async def debug_clear_breakpoint(
        *,
        target: str | None = None, addr: int, kind: int = 4, hardware: bool = False,
    ) -> debug_tool.DebugBreakpointResult:
        """Clear a previously-set breakpoint at `addr`."""
        return await debug_tool.debug_clear_breakpoint(
            fleet, fleet.resolve_target(target), addr, kind=kind, hardware=hardware,
        )

    @mcp.tool(name="debug_step", title="debug.step")
    @archived("debug.step", archive)
    async def debug_step(*, target: str | None = None) -> debug_tool.DebugStepResult:
        """Single-step one instruction (CPU stays paused). Returns the
        stub's stop reply."""
        return await debug_tool.debug_step(fleet, fleet.resolve_target(target))

    @mcp.tool(name="debug_continue", title="debug.continue")
    @archived("debug.continue", archive)
    async def debug_continue(
        *,
        target: str | None = None, timeout_s: float = 5.0,
    ) -> debug_tool.DebugContinueResult:
        """Resume CPU execution. Returns when the CPU next halts
        (breakpoint, signal). On `timeout_s` expiry, sends an
        out-of-band Ctrl-C to interrupt the CPU and returns the
        resulting stop reply."""
        return await debug_tool.debug_continue(fleet, fleet.resolve_target(target), timeout_s)

    @mcp.tool(name="debug_backtrace", title="debug.backtrace")
    @archived("debug.backtrace", archive)
    async def debug_backtrace(
        *,
        target: str | None = None, max_frames: int = 16,
    ) -> debug_tool.DebugBacktraceResult:
        """Walk the PowerPC SVR4 frame chain. Reads PC + SP + LR via
        the GDB `p` packet, then walks back-chain pointers + saved-LR
        slots in guest memory. Returns up to `max_frames` frames."""
        return await debug_tool.debug_backtrace(fleet, fleet.resolve_target(target), max_frames)

    @mcp.tool(name="debug_task_snapshot", title="debug.task_snapshot")
    @archived("debug.task_snapshot", archive)
    async def debug_task_snapshot(
        *,
        target: str | None = None, name: str, max_frames: int = 16,
    ) -> debug_tool.TaskSnapshotResult:
        """Snapshot a named AmigaOS task on the guest via MCPd's
        IDebug->ReadTaskContext. Returns registers + backtrace; this
        is the per-task counterpart to the GDB-stub whole-system
        view from debug.read_registers."""
        return await debug_tool.debug_task_snapshot(
            fleet, fleet.resolve_target(target), name, max_frames,
        )

    @mcp.tool(name="debug_symbol", title="debug.symbol")
    @archived("debug.symbol", archive)
    async def debug_symbol(
        *,
        target: str | None = None, address: int,
    ) -> debug_tool.DebugSymbolResult:
        """Resolve `address` to a DebugSymbol via IDebug->ObtainDebugSymbol
        (returns module / source file / line / function when known)."""
        return await debug_tool.debug_symbol(fleet, fleet.resolve_target(target), address)

    @mcp.tool(name="debug_stacktrace", title="debug.stacktrace")
    @archived("debug.stacktrace", archive)
    async def debug_stacktrace(
        *,
        target: str | None = None, name: str, max_frames: int = 32,
    ) -> debug_tool.DebugStacktraceResult:
        """Symbolicated backtrace of a named task via IDebug->StackTrace.
        Each frame includes module/function/source-file/line where
        IDebug can resolve them."""
        return await debug_tool.debug_stacktrace(
            fleet, fleet.resolve_target(target), name, max_frames,
        )

    @mcp.tool(name="debug_write_memory", title="debug.write_memory")
    @archived("debug.write_memory", archive)
    async def debug_write_memory(
        *,
        target: str | None = None, address: int, bytes_b64: str,
    ) -> debug_tool.DebugWriteMemoryResult:
        """Write base64-decoded bytes to an arbitrary memory address.
        DESTRUCTIVE - no per-task memory protection on AOS4. Use with
        care; intended for debug pokes only."""
        return await debug_tool.debug_write_memory(
            fleet, fleet.resolve_target(target), address, bytes_b64,
        )

    @mcp.tool(name="debug_write_register", title="debug.write_register")
    @archived("debug.write_register", archive)
    async def debug_write_register(
        *,
        target: str | None = None, name: str, register: str, value: int,
    ) -> debug_tool.DebugWriteRegisterResult:
        """Modify one register in a task's saved ExceptionContext via
        IDebug->WriteTaskContext. Register names: pc, lr, ctr, xer, cr,
        msr, dar, dsisr, or gpr0..gpr31."""
        return await debug_tool.debug_write_register(
            fleet, fleet.resolve_target(target), name, register, value,
        )

    # ---------- phase 5 events ---------------------------------------

    @mcp.tool(name="events_wait", title="events.wait")
    @archived("events.wait", archive)
    async def events_wait(
        *,
        target: str | None = None,
        topics: list[str] | None = None,
        timeout_ms: int = 5000,
    ) -> events_tool.EventsWaitResult:
        """Long-poll for events on subscribed topics. The daemon
        snapshots state at call start, polls every 200 ms, and
        returns the first list of deltas it sees (or [] on timeout).
        Topics: 'sys.lastalert', 'sys.task'."""
        return await events_tool.events_wait(
            fleet, fleet.resolve_target(target),
            topics=topics,  # type: ignore[arg-type]
            timeout_ms=timeout_ms,
        )

    # ---------- phase 11 polish: archive resources ------------------

    archive_root = Path(fleet.config.server.archive_root)

    @mcp.resource("amiga://runs/", name="runs",
                  title="Archive run index",
                  mime_type="application/json")
    def runs_index() -> list[dict[str, Any]]:
        """List of past run directories under archive_root, with the
        on-disk size of each run's tool-calls.ndjson."""
        return archive_resources.list_runs(archive_root)

    @mcp.resource("amiga://runs/{run_id}", name="run_metadata",
                  title="Per-run summary",
                  mime_type="application/json")
    def run_metadata(run_id: str) -> dict[str, Any]:
        """Summary of one run: tool-call count, set of tools used,
        total wall-clock duration, error count, first / last
        timestamp."""
        return archive_resources.run_metadata(archive_root, run_id)

    @mcp.resource("amiga://runs/{run_id}/calls", name="run_calls",
                  title="Per-run NDJSON tool-call log",
                  mime_type="application/x-ndjson")
    def run_calls(run_id: str) -> str:
        """Raw NDJSON content of one run's tool-calls.ndjson, capped
        at 1 MiB."""
        return archive_resources.run_calls_text(archive_root, run_id)

    # ---------- live fleet / target resources ------------------------

    @mcp.resource("amiga://fleet/targets", name="fleet_targets",
                  title="Configured targets",
                  mime_type="application/json")
    async def fleet_targets() -> list[dict[str, Any]]:
        """List every target in the active configuration with its
        type, channels, and (for QEMU targets) whether the qemu
        process we manage is currently running."""
        out: list[dict[str, Any]] = []
        for name in fleet.list_targets():
            cfg = fleet.target_config(name)
            mc = cfg.channels.mcpd
            qmp = cfg.channels.qmp
            gdb = cfg.channels.gdb
            proc = fleet.qemu_process(name)
            running = (proc is not None and proc.poll() is None
                       if cfg.type == "qemu" else None)
            out.append({
                "name": name,
                "type": cfg.type,
                "display_name": cfg.display_name,
                "machine": cfg.machine,
                "mcpd_endpoint": mc.endpoint if mc and mc.enabled else None,
                "qmp_endpoint": qmp.endpoint if qmp and qmp.enabled else None,
                "gdb_endpoint": gdb.endpoint if gdb and gdb.enabled else None,
                "qemu_running": running,
            })
        return out

    @mcp.resource("amiga://{target}/sys/version", name="sys_version",
                  title="AmigaOS version per target",
                  mime_type="application/json")
    async def per_target_sys_version(*, target: str | None = None) -> dict[str, Any]:
        """Live snapshot of `sys.version` for a target. Reads on
        demand - this is not cached; each MCP read fans out to MCPd."""
        try:
            v = await sys_tool.sys_version(fleet, fleet.resolve_target(target))
            return v.model_dump()
        except JsonRpcError as e:
            return {"error": e.to_dict()}

    @mcp.resource("amiga://{target}/proto/capabilities",
                  name="proto_capabilities",
                  title="Methods the daemon advertises on a target",
                  mime_type="application/json")
    async def per_target_proto_capabilities(*, target: str | None = None) -> dict[str, Any]:
        """Live snapshot of MCPd's `proto.capabilities` for a target.
        Returns the full daemon-side capability response (server,
        protocol, methods)."""
        try:
            r: dict[str, Any] = await fleet.mcpd(
                fleet.resolve_target(target),
            ).request("proto.capabilities")
            return r
        except JsonRpcError as e:
            return {"error": e.to_dict()}

    @mcp.resource("amiga://{target}/sys/tasks", name="sys_tasks",
                  title="Live task list per target",
                  mime_type="application/json")
    async def per_target_sys_tasks(*, target: str | None = None) -> dict[str, Any]:
        """Live snapshot of TaskReady + TaskWait. Each read walks
        ExecBase under Forbid; treat as a moment-in-time view."""
        try:
            t = await sys_tool.sys_tasks(fleet, fleet.resolve_target(target))
            return t.model_dump()
        except JsonRpcError as e:
            return {"error": e.to_dict()}

    @mcp.resource("amiga://{target}/qemu/serial_log", name="qemu_serial_log",
                  title="Captured QEMU serial output",
                  mime_type="text/plain")
    def per_target_serial_log(target: str) -> str:
        """Tail of the QEMU -serial output captured to disk when
        qemu.start launched this target. Empty when no QEMU has been
        started by this server, or when target is `type=remote`.
        Last 1 MiB only - earlier output is silently dropped."""
        path = fleet.serial_log_path(target)
        if path is None or not path.exists():
            return ""
        cap = 1024 * 1024
        size = path.stat().st_size
        if size <= cap:
            return path.read_text(encoding="utf-8", errors="replace")
        with path.open("rb") as fh:
            fh.seek(size - cap)
            tail = fh.read(cap)
        return (
            f"# truncated: {size} bytes total; last {cap} shown\n"
            + tail.decode("utf-8", errors="replace")
        )

    # ---------- phase 2: fleet + qemu ---------------------------------

    @mcp.tool(name="fleet_list_targets", title="fleet.list_targets")
    @archived("fleet.list_targets", archive, target_arg="_unused")
    async def fleet_list_targets(
        tags: list[str] | None = None,
    ) -> fleet_tool.TargetListResult:
        """List configured targets + their channel summary. Pass
        `tags` to filter to targets that carry every named tag
        (AND-match)."""
        return await fleet_tool.fleet_list_targets(fleet, tags=tags)

    @mcp.tool(name="fleet_target_status", title="fleet.target_status")
    @archived("fleet.target_status", archive)
    async def fleet_target_status(*, target: str | None = None) -> fleet_tool.TargetStatus:
        """Probe a target: QEMU running? channels reachable?"""
        return await fleet_tool.fleet_target_status(fleet, fleet.resolve_target(target))

    @mcp.tool(name="fleet_snapshot", title="fleet.snapshot")
    @archived("fleet.snapshot", archive, target_arg="_unused")
    async def fleet_snapshot(
        tags: list[str] | None = None,
    ) -> fleet_tool.FleetSnapshotResult:
        """One-call health summary across the fleet.

        Probes every configured target (or just those matching
        `tags`) in parallel and returns reachability + uptime + free
        RAM + lastalert per target. Replaces the multi-RPC probe
        pattern that every script used to reinvent. Per-target
        failures populate the entry's `error` field rather than
        aborting; you always get one entry per target."""
        return await fleet_tool.fleet_snapshot(fleet, tags=tags)

    @mcp.tool(name="fleet_run_on_all", title="fleet.run_on_all")
    @archived("fleet.run_on_all", archive, target_arg="_unused")
    async def fleet_run_on_all(
        method: str,
        params: dict[str, object] | None = None,
        targets: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> fleet_tool.FanOutResult:
        """Fan a method out across targets in parallel; per-target
        ok/error. `tags` filters targets via TargetConfig.tags."""
        return await fleet_tool.fleet_run_on_all(
            fleet, method, params, targets, tags=tags,
        )

    @mcp.tool(name="fleet_barrier", title="fleet.barrier")
    @archived("fleet.barrier", archive, target_arg="_unused")
    async def fleet_barrier(
        method: str,
        params: dict[str, object] | None = None,
        targets: list[str] | None = None,
        per_target_timeout_s: float = 30.0,
        tags: list[str] | None = None,
    ) -> fleet_tool.FanOutResult:
        """Fan-out with a per-target timeout; slow targets get a -32002
        timeout entry rather than blocking the whole barrier."""
        return await fleet_tool.fleet_barrier(
            fleet, method, params, targets, per_target_timeout_s,
            tags=tags,
        )

    @mcp.tool(name="fleet_discover", title="fleet.discover")
    @archived("fleet.discover", archive, target_arg="_unused")
    async def fleet_discover(
        timeout_ms: int = 1500,
    ) -> discover_tool.FleetDiscoverResult:
        """Find MCPd instances on the LAN via UDP broadcast on port
        4323. Returns each responder's IP + TCP port + server version
        + advertised method count + round-trip latency. The host
        learns each daemon's IP from the response's source address."""
        return await discover_tool.fleet_discover(fleet, timeout_ms)

    @mcp.tool(name="fleet_relay", title="fleet.relay")
    @archived("fleet.relay", archive, target_arg="_unused")
    async def fleet_relay(
        src_target: str, src_path: str,
        dst_target: str, dst_path: str,
    ) -> fleet_tool.FleetRelayResult:
        """Relay a file between two targets (read on src, write on
        dst, pure host-side coordination)."""
        return await fleet_tool.fleet_relay(
            fleet, src_target, src_path, dst_target, dst_path,
        )

    @mcp.tool(name="fleet_quorum_run", title="fleet.quorum_run")
    @archived("fleet.quorum_run", archive, target_arg="_unused")
    async def fleet_quorum_run(
        method: str,
        quorum: int,
        params: dict[str, object] | None = None,
        targets: list[str] | None = None,
        overall_timeout_s: float = 60.0,
        tags: list[str] | None = None,
    ) -> fleet_tool.QuorumRunResult:
        """Return as soon as `quorum` targets succeed; cancel the rest."""
        return await fleet_tool.fleet_quorum_run(
            fleet, method, quorum, params, targets, overall_timeout_s,
            tags=tags,
        )

    @mcp.tool(name="qemu_start", title="qemu.start")
    @archived("qemu.start", archive)
    async def qemu_start(*, target: str | None = None) -> qemu_tool.QemuStartResult:
        """Launch QEMU for a configured qemu target. Returns its PID."""
        return await qemu_tool.qemu_start(fleet, fleet.resolve_target(target))

    @mcp.tool(name="qemu_stop", title="qemu.stop")
    @archived("qemu.stop", archive)
    async def qemu_stop(
        *,
        target: str | None = None, qmp_timeout_s: float = 10.0
    ) -> qemu_tool.QemuStopResult:
        """Stop QEMU. Tries QMP `quit`, falls back to terminate / kill."""
        return await qemu_tool.qemu_stop(
            fleet, fleet.resolve_target(target),
            qmp_timeout_s=qmp_timeout_s,
        )

    @mcp.tool(name="qemu_reset", title="qemu.reset")
    @archived("qemu.reset", archive)
    async def qemu_reset(*, target: str | None = None) -> qemu_tool.QemuResetResult:
        """QMP `system_reset` (like pressing the reset button)."""
        return await qemu_tool.qemu_reset(fleet, fleet.resolve_target(target))

    @mcp.tool(name="qemu_status", title="qemu.status")
    @archived("qemu.status", archive)
    async def qemu_status(*, target: str | None = None) -> qemu_tool.QemuStatusResult:
        """Process / QMP / SerialShell reachability summary."""
        return await qemu_tool.qemu_status(fleet, fleet.resolve_target(target))

    @mcp.tool(name="qemu_screenshot", title="qemu.screenshot")
    @archived("qemu.screenshot", archive)
    async def qemu_screenshot(
        *,
        target: str | None = None, save_path: str | None = None
    ) -> qemu_tool.QemuScreenshotResult:
        """QMP `screendump` PNG. Returns image_b64 + optional saved_to."""
        return await qemu_tool.qemu_screenshot(
            fleet, fleet.resolve_target(target), save_path=save_path,
        )

    # ---------- phase 11: snapshots -----------------------------------

    @mcp.tool(name="qemu_savevm", title="qemu.savevm")
    @archived("qemu.savevm", archive)
    async def qemu_savevm(*, target: str | None = None, name: str) -> snap_tool.SavevmResult:
        """Save VM state to a named snapshot (HMP `savevm`)."""
        return await snap_tool.qemu_savevm(fleet, fleet.resolve_target(target), name)

    @mcp.tool(name="qemu_loadvm", title="qemu.loadvm")
    @archived("qemu.loadvm", archive)
    async def qemu_loadvm(*, target: str | None = None, name: str) -> snap_tool.LoadvmResult:
        """Restore VM state from a named snapshot (HMP `loadvm`)."""
        return await snap_tool.qemu_loadvm(fleet, fleet.resolve_target(target), name)

    @mcp.tool(name="qemu_list_snapshots", title="qemu.list_snapshots")
    @archived("qemu.list_snapshots", archive)
    async def qemu_list_snapshots(
        *,
        target: str | None = None,
    ) -> snap_tool.ListSnapshotsResult:
        """List existing VM snapshots (HMP `info snapshots`)."""
        return await snap_tool.qemu_list_snapshots(fleet, fleet.resolve_target(target))

    @mcp.tool(name="qemu_delete_snapshot", title="qemu.delete_snapshot")
    @archived("qemu.delete_snapshot", archive)
    async def qemu_delete_snapshot(
        *,
        target: str | None = None, name: str
    ) -> snap_tool.DeleteSnapshotResult:
        """Delete a named VM snapshot (HMP `delvm`)."""
        return await snap_tool.qemu_delete_snapshot(fleet, fleet.resolve_target(target), name)

    # ---------- phase 3: tests ----------------------------------------

    @mcp.tool(name="tests_list_suites", title="tests.list_suites")
    @archived("tests.list_suites", archive, target_arg="_unused")
    async def tests_list_suites() -> tests_tool.SuitesResult:
        """List AmigaQemuTests project suites."""
        return await tests_tool.tests_list_suites(fleet)

    @mcp.tool(name="tests_parse_output", title="tests.parse_output")
    @archived("tests.parse_output", archive, target_arg="_unused")
    async def tests_parse_output(output_b64: str) -> tests_tool.ParseResult:
        """Parse [PASS]/[FAIL] lines from a base64-encoded test log."""
        return await tests_tool.tests_parse_output(fleet, output_b64)

    @mcp.tool(name="tests_run_standard_dos_tests",
              title="tests.run_standard_dos_tests")
    @archived("tests.run_standard_dos_tests", archive)
    async def tests_run_standard_dos_tests(
        *,
        target: str | None = None,
    ) -> tests_tool.StandardDosResult:
        """Run AmigaQemuTests's dos_basic suite on a target."""
        return await tests_tool.tests_run_standard_dos_tests(fleet, fleet.resolve_target(target))

    @mcp.tool(name="tests_run_suite", title="tests.run_suite")
    @archived("tests.run_suite", archive)
    async def tests_run_suite(
        *,
        target: str | None = None, suite: str, skip_uploads: bool = False,
    ) -> tests_tool.SuiteRunResult:
        """Run a project suite (config/projects/<suite>.json) on a target."""
        return await tests_tool.tests_run_suite(
            fleet, fleet.resolve_target(target), suite, skip_uploads=skip_uploads,
        )

    # ============================================================
    # NAMESPACE DISPATCHERS (#6 from the API tidy pass)
    # ============================================================
    # 9 thin wrappers that fold the 73 fine-grained tools into 9
    # namespaced dispatchers. Each takes a `method` arg (the dotted-
    # form suffix, e.g. "list" / "write_chunk") plus the underlying
    # method's kwargs. Lets MCP clients scan 9 docstrings to find
    # what's available rather than 73 individual tools. Fine-grained
    # tools above are kept registered for backward-compat with
    # existing scripts; clients that prefer the consolidated surface
    # can use these.

    def _ns(handlers: dict[str, Any], method: str, kwargs: dict) -> Any:
        """Common dispatch helper: resolve target, look up handler,
        invoke. Raises InvalidParams on unknown method."""
        target = fleet.resolve_target(kwargs.pop("target", None))
        fn = handlers.get(method)
        if fn is None:
            raise MethodNotFound(
                f"unknown method: {method!r}",
                data={"namespace_methods": sorted(handlers)},
            )
        return fn(fleet, target, **kwargs)

    def _ns_no_target(handlers: dict[str, Any], method: str,
                      kwargs: dict) -> Any:
        """Dispatch helper for namespaces whose methods don't take a
        target (proto, fleet for some methods)."""
        fn = handlers.get(method)
        if fn is None:
            raise MethodNotFound(
                f"unknown method: {method!r}",
                data={"namespace_methods": sorted(handlers)},
            )
        return fn(fleet, **kwargs)

    @mcp.tool(name="fs", title="fs.dispatch")
    @archived("fs.dispatch", archive)
    async def fs_ns(*, method: str, params: dict[str, Any] | None = None) -> Any:
        """fs.* dispatcher. `method` is one of:

          list(target, path, recursive=False, max_depth=8)
          stat(target, path)
          read(target, path, offset=None, length=None)
          write(target, path, content_b64, compression="none", raw_size=None)
          write_chunk(target, path, offset, content_b64, compression="none",
                      raw_size=None, truncate=None, total_size=None)
          delete(target, path, recursive=False)
          makedir(target, path)
          rename(target, src, dst)
          protect(target, path, bits)
          copy(target, src, dst)
          hash(target, path, algo="sha256")

        target=None falls back to server.default_target.
        """
        return await _ns({
            "list":        fs_tool.fs_list,
            "stat":        fs_tool.fs_stat,
            "read":        fs_tool.fs_read,
            "write":       fs_tool.fs_write,
            "write_chunk": fs_tool.fs_write_chunk,
            "delete":      fs_tool.fs_delete,
            "makedir":     fs_tool.fs_makedir,
            "rename":      fs_tool.fs_rename,
            "protect":     fs_tool.fs_protect,
            "copy":        fs_tool.fs_copy,
            "hash":        fs_tool.fs_hash,
        }, method, params or {})

    @mcp.tool(name="sys", title="sys.dispatch")
    @archived("sys.dispatch", archive)
    async def sys_ns(*, method: str, params: dict[str, Any] | None = None) -> Any:
        """sys.* dispatcher. `method` is one of, grouped logically:

          identity / state   : version, uptime
          live enumeration   : tasks, libraries, devices, ports, applications
          system resources   : memory, volumes, assigns
          hardware probing   : hardware, hardware_i2c, hardware_perfcounters
          executable / debug : executable_symbols
          alerts / power     : lastalert, alert_decode, cold_reboot
          memory introspection (PA / TLB / CCSR, P5020/P1022 only):
                               read_ccsr, read_pa, tlb_dump
          board supervisor   : mcu_cmd

        Destructive methods that need params.confirm=true: cold_reboot,
        mcu_cmd (when cmd='s' = POWER OFF).

        See individual sys_* tools for argument details. All take
        target (default from server.default_target).
        """
        return await _ns({
            # identity / state
            "version":               sys_tool.sys_version,
            "uptime":                sys_tool.sys_uptime,
            # live enumeration
            "tasks":                 sys_tool.sys_tasks,
            "libraries":             sys_tool.sys_libraries,
            "devices":               sys_tool.sys_devices,
            "ports":                 sys_tool.sys_ports,
            "applications":          sys_tool.sys_applications,
            # system resources
            "memory":                sys_tool.sys_memory,
            "volumes":               sys_tool.sys_volumes,
            "assigns":               sys_tool.sys_assigns,
            # hardware probing
            "hardware":              sys_tool.sys_hardware,
            "hardware_i2c":          sys_tool.sys_hardware_i2c,
            "hardware_perfcounters": sys_tool.sys_hardware_perfcounters,
            # executable / debug
            "executable_symbols":    sys_tool.sys_executable_symbols,
            # alerts / power
            "lastalert":             sys_tool.sys_lastalert,
            "alert_decode":          sys_tool.sys_alert_decode,
            "cold_reboot":           sys_tool.sys_cold_reboot,
            # memory introspection (PA / TLB / CCSR)
            "read_ccsr":             sys_tool.sys_read_ccsr,
            "read_pa":               sys_tool.sys_read_pa,
            "tlb_dump":              sys_tool.sys_tlb_dump,
            # board supervisor
            "mcu_cmd":               sys_tool.sys_mcu_cmd,
        }, method, params or {})

    @mcp.tool(name="power", title="power.dispatch")
    @archived("power.dispatch", archive)
    async def power_ns(
        *, method: str, params: dict[str, Any] | None = None,
    ) -> Any:
        """power.* dispatcher -- host-side X5000 / A1222 MCU debug
        shell over the configured `[channels.mcu]` cable. `method`
        is one of:

          help, identify, identify_dates, sensors          (no confirm)
          toggle_stream(watch_s, confirm),                 (confirm: true)
          on(confirm), off(confirm), shell(cmd, confirm)   (confirm: true)

        on / off / toggle_stream / shell are hardware-destructive
        and require `confirm=True` in their params dict.
        """
        return await _ns({
            "help":            power_tool.power_help,
            "identify":        power_tool.power_identify,
            "identify_dates":  power_tool.power_identify_dates,
            "sensors":         power_tool.power_sensors,
            "toggle_stream":   power_tool.power_toggle_stream,
            "on":              power_tool.power_on,
            "off":             power_tool.power_off,
            "shell":           power_tool.power_shell,
        }, method, params or {})

    @mcp.tool(name="wb", title="wb.dispatch")
    @archived("wb.dispatch", archive)
    async def wb_ns(*, method: str, params: dict[str, Any] | None = None) -> Any:
        """wb.* (Workbench) dispatcher. `method` is one of:

          screens, windows, publicscreens, frontmost
        """
        return await _ns({
            "screens":       wb_tool.wb_screens,
            "windows":       wb_tool.wb_windows,
            "publicscreens": wb_tool.wb_publicscreens,
            "frontmost":     wb_tool.wb_frontmost,
        }, method, params or {})

    @mcp.tool(name="debug", title="debug.dispatch")
    @archived("debug.dispatch", archive)
    async def debug_ns(*, method: str, params: dict[str, Any] | None = None) -> Any:
        """debug.* dispatcher (GDB stub + IDebug). `method` is one of:

          read_registers, read_memory, stop_reason, detach,
          set_breakpoint, clear_breakpoint, step, continue,
          backtrace, task_snapshot, symbol, stacktrace,
          write_memory, write_register
        """
        return await _ns({
            "read_registers":   debug_tool.debug_read_registers,
            "read_memory":      debug_tool.debug_read_memory,
            "stop_reason":      debug_tool.debug_stop_reason,
            "detach":           debug_tool.debug_detach,
            "set_breakpoint":   debug_tool.debug_set_breakpoint,
            "clear_breakpoint": debug_tool.debug_clear_breakpoint,
            "step":             debug_tool.debug_step,
            "continue":         debug_tool.debug_continue,
            "backtrace":        debug_tool.debug_backtrace,
            "task_snapshot":    debug_tool.debug_task_snapshot,
            "symbol":           debug_tool.debug_symbol,
            "stacktrace":       debug_tool.debug_stacktrace,
            "write_memory":     debug_tool.debug_write_memory,
            "write_register":   debug_tool.debug_write_register,
        }, method, params or {})

    @mcp.tool(name="qemu", title="qemu.dispatch")
    @archived("qemu.dispatch", archive)
    async def qemu_ns(*, method: str, params: dict[str, Any] | None = None) -> Any:
        """qemu.* dispatcher (QEMU lifecycle, snapshots). `method`:

          start, stop, reset, status, screenshot,
          savevm, loadvm, list_snapshots, delete_snapshot
        """
        return await _ns({
            "start":           qemu_tool.qemu_start,
            "stop":            qemu_tool.qemu_stop,
            "reset":           qemu_tool.qemu_reset,
            "status":          qemu_tool.qemu_status,
            "screenshot":      qemu_tool.qemu_screenshot,
            "savevm":          snap_tool.qemu_savevm,
            "loadvm":          snap_tool.qemu_loadvm,
            "list_snapshots":  snap_tool.qemu_list_snapshots,
            "delete_snapshot": snap_tool.qemu_delete_snapshot,
        }, method, params or {})

    @mcp.tool(name="fleet", title="fleet.dispatch")
    @archived("fleet.dispatch", archive, target_arg="_unused")
    async def fleet_ns(*, method: str, params: dict[str, Any] | None = None) -> Any:
        """fleet.* dispatcher (multi-target). `method` is one of:

          list_targets, target_status(target), snapshot(tags=[]),
          run_on_all(method, params, targets, tags),
          barrier(method, params, targets, per_target_timeout_s, tags),
          quorum_run(method, quorum, params, targets, overall_timeout_s, tags),
          discover(timeout_s)

        target_status takes a target arg (default from config).
        Other methods don't take a target.
        """
        if method == "target_status":
            return await _ns(
                {"target_status": fleet_tool.fleet_target_status},
                method, params or {},
            )
        handlers = {
            "list_targets":  lambda fleet_, **kw: fleet_tool.fleet_list_targets(fleet_, **kw),
            "snapshot":      lambda fleet_, **kw: fleet_tool.fleet_snapshot(fleet_, **kw),
            "run_on_all":    lambda fleet_, **kw: fleet_tool.fleet_run_on_all(fleet_, **kw),
            "barrier":       lambda fleet_, **kw: fleet_tool.fleet_barrier(fleet_, **kw),
            "quorum_run":    lambda fleet_, **kw: fleet_tool.fleet_quorum_run(fleet_, **kw),
            "discover":      lambda fleet_, **kw: discover_tool.fleet_discover(fleet_, **kw),
        }
        return await _ns_no_target(handlers, method, params or {})

    @mcp.tool(name="tests", title="tests.dispatch")
    @archived("tests.dispatch", archive, target_arg="_unused")
    async def tests_ns(*, method: str, params: dict[str, Any] | None = None) -> Any:
        """tests.* dispatcher (AmigaQemuTests harness). `method`:

          list_suites(), parse_output(output_b64),
          run_standard_dos_tests(target),
          run_suite(target, suite, skip_uploads=False)
        """
        if method in ("list_suites", "parse_output"):
            handlers = {
                "list_suites":  lambda fleet_, **kw: tests_tool.tests_list_suites(fleet_, **kw),
                "parse_output": lambda fleet_, **kw: tests_tool.tests_parse_output(fleet_, **kw),
            }
            return await _ns_no_target(handlers, method, params or {})
        # Both run_* methods take target.
        return await _ns({
            "run_standard_dos_tests": tests_tool.tests_run_standard_dos_tests,
            "run_suite":              tests_tool.tests_run_suite,
        }, method, params or {})

    @mcp.tool(name="events", title="events.dispatch")
    @archived("events.dispatch", archive)
    async def events_ns(*, method: str, params: dict[str, Any] | None = None) -> Any:
        """events.* dispatcher. `method` is one of:

          wait(target, topics=[], timeout_ms=5000)
        """
        return await _ns({
            "wait": events_tool.events_wait,
        }, method, params or {})

    @mcp.tool(name="app", title="app.dispatch")
    @archived("app.dispatch", archive)
    async def app_ns(*, method: str, params: dict[str, Any] | None = None) -> Any:
        """app.* dispatcher. `method` is one of:

          notify(target, title, text, priority=0)
        """
        return await _ns({
            "notify": sys_tool.app_notify,
        }, method, params or {})

    # ---------- phase 9a: native installer foundation --------------

    @mcp.tool(name="installer_list_machines",
              title="installer.list_machines")
    @archived("installer.list_machines", archive, target_arg="_unused_")
    async def installer_list_machines() -> installer_tool.ListMachinesResult:
        """List supported AmigaOS 4.1 FE target machines, with each
        machine's ISO prefix, accepted aliases, applied updates, and
        any per-machine quirks. Pure host-side static data."""
        return await installer_tool.installer_list_machines()

    @mcp.tool(name="installer_required_files",
              title="installer.required_files")
    @archived("installer.required_files", archive, target_arg="_unused_")
    async def installer_required_files(
        *, machine: str | None = None,
    ) -> installer_tool.RequiredFilesResult:
        """For a given machine, return the file manifest the installer
        will need on disk: ISO prefix, update LHAs, enhancer LHA, and
        per-machine extras. `machine` accepts canonical IDs (X5000)
        and aliases (amigaone x5000, etc, case-insensitive). Defaults
        to `[defaults] machine`."""
        machine = _default(machine, "machine", "machine")
        return await installer_tool.installer_required_files(machine)

    @mcp.tool(name="installer_scan_sources",
              title="installer.scan_sources")
    @archived("installer.scan_sources", archive, target_arg="_unused_")
    async def installer_scan_sources(
        *, sources_dir: str | None = None,
    ) -> installer_tool.ScanSourcesResult:
        """Walk a host-side directory and report what install files
        are present: detected machine (if exactly one supported ISO),
        all ISO files keyed by machine, unsupported ISOs, and all
        *.lha files. Defaults to `[defaults] sources_dir`."""
        sources_dir = _default(sources_dir, "sources_dir", "sources_dir")
        return await installer_tool.installer_scan_sources(sources_dir)

    @mcp.tool(name="installer_preflight",
              title="installer.preflight")
    @archived("installer.preflight", archive)
    async def installer_preflight(
        *, target: str | None = None,
        dest_volume: str | None = None,
        sources_dir: str | None = None,
        machine: str | None = None,
        iso: str | None = None,
    ) -> installer_tool.PreflightResult:
        """Compose a pre-install safety check. Validates sources_dir
        layout, resolves machine + ISO, and confirms dest_volume is
        mounted on the target, is not a system volume, and contains
        no existing AOS install. No mutation. dest_volume / sources_dir
        / machine fall back to `[defaults]`."""
        dest_volume = _default(dest_volume, "dest_volume", "dest_volume")
        sources_dir = _default(sources_dir, "sources_dir", "sources_dir")
        if machine is None:
            machine = fleet.config.defaults.machine
        return await installer_tool.installer_preflight(
            fleet, fleet.resolve_target(target),
            dest_volume=dest_volume, sources_dir=sources_dir,
            machine=machine, iso=iso,
        )

    # --- phase 9b step primitives ---

    @mcp.tool(name="installer_mount_iso", title="installer.mount_iso")
    @archived("installer.mount_iso", archive)
    async def installer_mount_iso(
        *, target: str | None = None,
        iso_path: str,
        dos_device: str = "COMBI:",
        unit: int = 50,
        expected_volume: str | None = "AmigaOS 4.1 Final Edition:",
        timeout_s: float = 30.0,
        settle_s: float = 3.0,
    ) -> installer_tool.MountIsoResult:
        """Mount an ISO via diskimage.device + MountDiskImage and
        wait for the volume to appear. Reversible via unmount_iso.
        Requires the diskimage bootstrap files to be present on
        the target (per-machine install sequences stage them before
        calling)."""
        return await installer_tool.installer_mount_iso(
            fleet, fleet.resolve_target(target),
            iso_path=iso_path, dos_device=dos_device, unit=unit,
            expected_volume=expected_volume,
            timeout_s=timeout_s, settle_s=settle_s,
        )

    @mcp.tool(name="installer_unmount_iso",
              title="installer.unmount_iso")
    @archived("installer.unmount_iso", archive)
    async def installer_unmount_iso(
        *, target: str | None = None,
        unit: int = 50,
        timeout_s: float = 15.0,
    ) -> installer_tool.UnmountIsoResult:
        """Eject the ISO from a diskimage.device unit. Idempotent."""
        return await installer_tool.installer_unmount_iso(
            fleet, fleet.resolve_target(target),
            unit=unit, timeout_s=timeout_s,
        )

    @mcp.tool(name="installer_copy_tree", title="installer.copy_tree")
    @archived("installer.copy_tree", archive)
    async def installer_copy_tree(
        *, target: str | None = None,
        src: str, dst: str,
        all_: bool = True,
        clone: bool = True,
        quiet: bool = True,
        confirm: bool = False,
        timeout_s: float = 600.0,
    ) -> installer_tool.CopyTreeResult:
        """Recursive AmigaDOS Copy with ALL/CLONE/QUIET flags.
        Mutating — requires confirm=True. 10-minute default timeout."""
        return await installer_tool.installer_copy_tree(
            fleet, fleet.resolve_target(target),
            src=src, dst=dst, all_=all_, clone=clone, quiet=quiet,
            confirm=confirm, timeout_s=timeout_s,
        )

    @mcp.tool(name="installer_apply_lha", title="installer.apply_lha")
    @archived("installer.apply_lha", archive)
    async def installer_apply_lha(
        *, target: str | None = None,
        archive: str, dest: str,
        confirm: bool = False,
        timeout_s: float = 600.0,
    ) -> installer_tool.ApplyLhaResult:
        """Extract an LHA archive into a destination directory via
        `LhA x`. Caller must ensure dest exists. Mutating — requires
        confirm=True."""
        return await installer_tool.installer_apply_lha(
            fleet, fleet.resolve_target(target),
            archive=archive, dest=dest,
            confirm=confirm, timeout_s=timeout_s,
        )

    @mcp.tool(name="installer_read_kicklayout",
              title="installer.read_kicklayout")
    @archived("installer.read_kicklayout", archive)
    async def installer_read_kicklayout(
        *, target: str | None = None,
        dest_volume: str | None = None,
    ) -> installer_tool.ReadKicklayoutResult:
        """Read <dest_volume>Kickstart/Kicklayout as text. dest_volume
        falls back to `[defaults] dest_volume`."""
        dest_volume = _default(dest_volume, "dest_volume", "dest_volume")
        return await installer_tool.installer_read_kicklayout(
            fleet, fleet.resolve_target(target),
            dest_volume=dest_volume,
        )

    @mcp.tool(name="installer_write_kicklayout",
              title="installer.write_kicklayout")
    @archived("installer.write_kicklayout", archive)
    async def installer_write_kicklayout(
        *, target: str | None = None,
        dest_volume: str | None = None,
        content: str,
        confirm: bool = False,
        backup_suffix: str | None = ".bak",
    ) -> installer_tool.WriteKicklayoutResult:
        """Write Kicklayout content. Backs up the existing file
        (if any) to <Kicklayout>.bak by default. Mutating —
        requires confirm=True. dest_volume falls back to
        `[defaults] dest_volume`."""
        dest_volume = _default(dest_volume, "dest_volume", "dest_volume")
        return await installer_tool.installer_write_kicklayout(
            fleet, fleet.resolve_target(target),
            dest_volume=dest_volume, content=content,
            confirm=confirm, backup_suffix=backup_suffix,
        )

    @mcp.tool(name="installer_patch_kicklayout",
              title="installer.patch_kicklayout")
    @archived("installer.patch_kicklayout", archive)
    async def installer_patch_kicklayout(
        *, target: str | None = None,
        dest_volume: str | None = None,
        add_modules: list[dict] | None = None,
        replace_text: list[dict] | None = None,
        confirm: bool = False,
        backup_suffix: str | None = ".bak",
    ) -> installer_tool.PatchKicklayoutResult:
        """Atomic read-modify-write of Kicklayout. add_modules entries
        are `{modules: [str], label: str}`; replace_text entries are
        `{old: str, new: str}`. No-op if patches don't change content.
        Mutating — requires confirm=True. dest_volume falls back to
        `[defaults] dest_volume`."""
        dest_volume = _default(dest_volume, "dest_volume", "dest_volume")
        return await installer_tool.installer_patch_kicklayout(
            fleet, fleet.resolve_target(target),
            dest_volume=dest_volume,
            add_modules=add_modules, replace_text=replace_text,
            confirm=confirm, backup_suffix=backup_suffix,
        )

    # --- phase 9c per-machine sequences ---

    @mcp.tool(name="installer_install_x5000",
              title="installer.install_x5000")
    @archived("installer.install_x5000", archive)
    async def installer_install_x5000(
        *, target: str | None = None,
        dest_volume: str | None = None,
        iso_filename: str | None = None,
        sources_dir: str | None = None,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> Any:
        """Run the AmigaOne X5000 install sequence. Default dry_run=True
        returns the planned step list; pass dry_run=False, confirm=True
        to actually execute. Caller must stage ISO + LHAs +
        diskimage-bootstrap/ into <dest>:tmp/ before calling.
        dest_volume / sources_dir / iso_filename fall back to
        `[defaults]`."""
        dest_volume = _default(dest_volume, "dest_volume", "dest_volume")
        if sources_dir is None:
            sources_dir = fleet.config.defaults.sources_dir
        if iso_filename is None:
            iso_filename = fleet.config.defaults.iso_filename
        return await installer_tool.installer_install_x5000(
            fleet, fleet.resolve_target(target),
            dest_volume=dest_volume,
            iso_filename=iso_filename,
            sources_dir=sources_dir,
            dry_run=dry_run, confirm=confirm,
        )

    @mcp.tool(name="installer_run", title="installer.run")
    @archived("installer.run", archive)
    async def installer_run(
        *, target: str | None = None,
        dest_volume: str | None = None,
        machine: str | None = None,
        iso_filename: str | None = None,
        sources_dir: str | None = None,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> Any:
        """Dispatcher to per-machine install sequences. Currently
        implemented: X5000. Other machines raise InvalidParams.
        Default dry_run=True; pass dry_run=False, confirm=True to
        execute. dest_volume / machine / sources_dir / iso_filename
        fall back to `[defaults]`."""
        dest_volume = _default(dest_volume, "dest_volume", "dest_volume")
        machine = _default(machine, "machine", "machine")
        if sources_dir is None:
            sources_dir = fleet.config.defaults.sources_dir
        if iso_filename is None:
            iso_filename = fleet.config.defaults.iso_filename
        return await installer_tool.installer_run(
            fleet, fleet.resolve_target(target),
            dest_volume=dest_volume, machine=machine,
            iso_filename=iso_filename, sources_dir=sources_dir,
            dry_run=dry_run, confirm=confirm,
        )

    @mcp.tool(name="installer_stage", title="installer.stage")
    @archived("installer.stage", archive)
    async def installer_stage(
        *, target: str | None = None,
        dest_volume: str | None = None,
        sources_dir: str | None = None,
        machine: str | None = None,
        iso_filename: str | None = None,
        iso_lha_path: str | None = None,
        bootstrap_dir: str | None = None,
        confirm: bool = False,
    ) -> Any:
        """Upload ISO + LHAs + diskimage-bootstrap/ into <dest>:tmp/.
        Mutating; requires confirm=True (multi-GB upload).

        Auto-detects iso_filename from sources_dir scan if omitted.
        bootstrap_dir defaults to a search of standard host paths.

        If `iso_lha_path` is provided (or `<iso>.lha` exists next to
        the iso in sources_dir), the LHA-of-ISO form is uploaded
        instead of the raw ISO -- saves ~60% bandwidth on incompressible
        ISO data. The install sequence's `extract_iso_lha` step
        recovers the .iso file on the target before mount.
        dest_volume / sources_dir / machine / bootstrap_dir /
        iso_filename fall back to `[defaults]`."""
        dest_volume = _default(dest_volume, "dest_volume", "dest_volume")
        sources_dir = _default(sources_dir, "sources_dir", "sources_dir")
        machine = _default(machine, "machine", "machine")
        if bootstrap_dir is None:
            bootstrap_dir = fleet.config.defaults.bootstrap_dir
        if iso_filename is None:
            iso_filename = fleet.config.defaults.iso_filename
        return await installer_tool.installer_stage(
            fleet, fleet.resolve_target(target),
            dest_volume=dest_volume,
            sources_dir=sources_dir,
            machine=machine,
            iso_filename=iso_filename,
            iso_lha_path=iso_lha_path,
            bootstrap_dir=bootstrap_dir,
            confirm=confirm,
        )

    @mcp.tool(name="installer_verify", title="installer.verify")
    @archived("installer.verify", archive)
    async def installer_verify(
        *, target: str | None = None,
        dest_volume: str | None = None,
        machine: str | None = None,
        extra_paths: list[str] | None = None,
        check_sha256: bool = False,
    ) -> Any:
        """Walk the per-machine post-install manifest and verify each
        file exists on the target. Optional sha256 check (only fires
        for entries with expected hashes — populated by oracle runs).
        dest_volume / machine fall back to `[defaults]`."""
        dest_volume = _default(dest_volume, "dest_volume", "dest_volume")
        machine = _default(machine, "machine", "machine")
        return await installer_tool.installer_verify(
            fleet, fleet.resolve_target(target),
            dest_volume=dest_volume,
            machine=machine,
            extra_paths=extra_paths,
            check_sha256=check_sha256,
        )

    @mcp.tool(name="installer", title="installer.dispatch")
    @archived("installer.dispatch", archive)
    async def installer_ns(
        *, method: str, params: dict[str, Any] | None = None,
    ) -> Any:
        """installer.* dispatcher. `method` is one of:

        Read-only:
          list_machines()
          required_files(machine)
          scan_sources(sources_dir)
          preflight(target, dest_volume, sources_dir,
                    machine=None, iso=None)

        Step primitives (confirm=True for mutating):
          mount_iso(target, iso_path, dos_device="...", unit=50,
                    expected_volume=..., timeout_s=30, settle_s=3)
          unmount_iso(target, unit=50)
          copy_tree(target, src, dst, all_=True, clone=True,
                    quiet=True, confirm)
          apply_lha(target, archive, dest, confirm)
          read_kicklayout(target, dest_volume)
          write_kicklayout(target, dest_volume, content, confirm,
                           backup_suffix=".bak")
          patch_kicklayout(target, dest_volume, add_modules,
                           replace_text, confirm,
                           backup_suffix=".bak")
        """
        p = dict(params or {})
        if method == "list_machines":
            return await installer_tool.installer_list_machines()
        if method == "required_files":
            return await installer_tool.installer_required_files(
                p.get("machine", ""),
            )
        if method == "scan_sources":
            return await installer_tool.installer_scan_sources(
                p.get("sources_dir", ""),
            )
        # Methods that take a target follow the standard dispatch:
        # resolve target then call the function with kwargs.
        target = fleet.resolve_target(p.pop("target", None))
        if method == "preflight":
            return await installer_tool.installer_preflight(
                fleet, target,
                dest_volume=p.get("dest_volume", ""),
                sources_dir=p.get("sources_dir", ""),
                machine=p.get("machine"),
                iso=p.get("iso"),
            )
        if method == "mount_iso":
            return await installer_tool.installer_mount_iso(
                fleet, target, **p,
            )
        if method == "unmount_iso":
            return await installer_tool.installer_unmount_iso(
                fleet, target, **p,
            )
        if method == "copy_tree":
            return await installer_tool.installer_copy_tree(
                fleet, target, **p,
            )
        if method == "apply_lha":
            return await installer_tool.installer_apply_lha(
                fleet, target, **p,
            )
        if method == "read_kicklayout":
            return await installer_tool.installer_read_kicklayout(
                fleet, target, **p,
            )
        if method == "write_kicklayout":
            return await installer_tool.installer_write_kicklayout(
                fleet, target, **p,
            )
        if method == "patch_kicklayout":
            return await installer_tool.installer_patch_kicklayout(
                fleet, target, **p,
            )
        if method == "install_x5000":
            return await installer_tool.installer_install_x5000(
                fleet, target, **p,
            )
        if method == "run":
            return await installer_tool.installer_run(
                fleet, target, **p,
            )
        if method == "stage":
            return await installer_tool.installer_stage(
                fleet, target, **p,
            )
        if method == "verify":
            return await installer_tool.installer_verify(
                fleet, target, **p,
            )
        raise MethodNotFound(
            f"unknown method: {method!r}",
            data={"namespace_methods": [
                "list_machines", "required_files",
                "scan_sources", "preflight",
                "mount_iso", "unmount_iso",
                "copy_tree", "apply_lha",
                "read_kicklayout", "write_kicklayout",
                "patch_kicklayout",
                "install_x5000", "run",
                "stage", "verify",
            ]},
        )

    # ---------- host-side serial capture (phase 7 prep) -------------

    @mcp.tool(name="serial_start", title="serial.start")
    @archived("serial.start", archive)
    async def serial_start(
        *, target: str | None = None,
        channel: str = "uboot",
        truncate: bool = False,
    ) -> serial_tool.SerialStartResult:
        """Open the configured serial port and start a background
        capture writing raw bytes to a log file. Default channel is
        'uboot' (rear-panel DB9 — U-Boot console + AOS4 kernel debug);
        pass channel='mcu' for the internal MCU UART. Pass
        truncate=true to wipe the previous log on start."""
        return await serial_tool.serial_start(
            fleet, fleet.resolve_target(target),
            channel=channel, truncate=truncate,
        )

    @mcp.tool(name="serial_stop", title="serial.stop")
    @archived("serial.stop", archive)
    async def serial_stop(
        *, target: str | None = None,
        channel: str = "uboot",
    ) -> serial_tool.SerialStopResult:
        """Stop a running capture. Idempotent."""
        return await serial_tool.serial_stop(
            fleet, fleet.resolve_target(target), channel=channel,
        )

    @mcp.tool(name="serial_status", title="serial.status")
    @archived("serial.status", archive)
    async def serial_status(
        *, target: str | None = None,
        channel: str | None = None,
    ) -> serial_tool.SerialStatusResult:
        """List active serial captures. Optional filters by target +
        channel. target=None means 'all targets'."""
        return await serial_tool.serial_status(
            fleet,
            target=target if target else None,
            channel=channel,
        )

    @mcp.tool(name="serial_read", title="serial.read")
    @archived("serial.read", archive)
    async def serial_read(
        *, target: str | None = None,
        channel: str = "uboot",
        offset: int = 0,
        max_bytes: int = 65536,
    ) -> serial_tool.SerialReadResult:
        """Read bytes from a capture log starting at `offset`. Returns
        base64-encoded bytes plus the next offset for incremental
        polling. Works whether capture is running or stopped."""
        return await serial_tool.serial_read(
            fleet, fleet.resolve_target(target),
            channel=channel, offset=offset, max_bytes=max_bytes,
        )

    @mcp.tool(name="serial_tail", title="serial.tail")
    @archived("serial.tail", archive)
    async def serial_tail(
        *, target: str | None = None,
        channel: str = "uboot",
        max_bytes: int = 8192,
    ) -> serial_tool.SerialReadResult:
        """Read the last `max_bytes` of a capture log. Convenience for
        'what just happened' queries."""
        return await serial_tool.serial_tail(
            fleet, fleet.resolve_target(target),
            channel=channel, max_bytes=max_bytes,
        )

    @mcp.tool(name="serial_clear", title="serial.clear")
    @archived("serial.clear", archive)
    async def serial_clear(
        *, target: str | None = None,
        channel: str = "uboot",
    ) -> serial_tool.SerialChannelInfo:
        """Truncate the capture log. Capture must be stopped."""
        return await serial_tool.serial_clear(
            fleet, fleet.resolve_target(target), channel=channel,
        )

    @mcp.tool(name="serial", title="serial.dispatch")
    @archived("serial.dispatch", archive)
    async def serial_ns(*, method: str, params: dict[str, Any] | None = None) -> Any:
        """serial.* dispatcher. `method` is one of:

          start(target, channel="uboot", truncate=False)
          stop(target, channel="uboot")
          status(target=None, channel=None)
          read(target, channel="uboot", offset=0, max_bytes=65536)
          tail(target, channel="uboot", max_bytes=8192)
          clear(target, channel="uboot")

        target=None on status means 'all targets'; on the others
        falls back to server.default_target.
        """
        p = params or {}
        if method == "status":
            return await serial_tool.serial_status(
                fleet,
                target=p.get("target") or None,
                channel=p.get("channel"),
            )
        return await _ns({
            "start": serial_tool.serial_start,
            "stop":  serial_tool.serial_stop,
            "read":  serial_tool.serial_read,
            "tail":  serial_tool.serial_tail,
            "clear": serial_tool.serial_clear,
        }, method, p)


def _build_runtime(config: Config) -> tuple[Fleet, Archive]:
    fleet = Fleet(config)
    archive = Archive(Path(config.server.archive_root))
    return fleet, archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="amiga-fleet-mcp",
        description="MCP server for AmigaOS 4 fleets.",
    )
    parser.add_argument(
        "--version", action="version",
        version=f"amiga-fleet-mcp {__version__}",
    )
    parser.add_argument(
        "--config", type=Path, default=None,
        help="Path to config.toml (default: $AMIGA_FLEET_CONFIG or "
             "platform default).",
    )
    parser.add_argument(
        "--inspect", action="store_true",
        help="Load config, list configured targets, exit. Doesn't start MCP.",
    )
    parser.add_argument(
        "--health-check", action="store_true",
        help="Probe every configured target's MCPd channel; report "
             "advertised methods + AmigaOS version per target. Doesn't "
             "start MCP. Exits non-zero if any target is down.",
    )
    parser.add_argument(
        "--list-tools", action="store_true",
        help="Print every registered MCP tool name + title + first "
             "line of its docstring. Doesn't start MCP.",
    )
    parser.add_argument(
        "--list-resources", action="store_true",
        help="Print every registered MCP resource URI / template + "
             "title + mime type. Doesn't start MCP.",
    )
    parser.add_argument(
        "--discover", action="store_true",
        help="UDP-broadcast probe for MCPd instances on the LAN. "
             "Prints discovered targets and exits. Doesn't start MCP.",
    )
    parser.add_argument(
        "--discover-timeout-ms", type=int, default=1500,
        help="Window to wait for discovery responses (default 1500).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    try:
        config = load_config(args.config)
    except FileNotFoundError as e:
        print(f"amiga-fleet-mcp: {e}", file=sys.stderr)
        return 64
    except (JsonRpcError, ValueError) as e:
        print(f"amiga-fleet-mcp: bad config: {e}", file=sys.stderr)
        return 64

    fleet, archive = _build_runtime(config)
    log.info("archive run dir: %s", archive.run_dir)
    log.info("targets: %s", fleet.list_targets())

    if args.inspect:
        for name in fleet.list_targets():
            tc = fleet.target_config(name)
            mc = tc.channels.mcpd
            mc_str = f"mcpd={mc.endpoint}" if mc and mc.enabled else "no-mcpd"
            print(f"  {name}: {tc.type} ({mc_str})")
        return 0

    if args.health_check:
        import asyncio
        rc = asyncio.run(_run_health_check(fleet))
        return rc

    if args.list_tools:
        import asyncio
        server = build_server(fleet, archive)
        asyncio.run(_print_tools(server))
        return 0

    if args.list_resources:
        import asyncio
        server = build_server(fleet, archive)
        asyncio.run(_print_resources(server))
        return 0

    if args.discover:
        import asyncio
        rc = asyncio.run(_run_discover(args.discover_timeout_ms))
        return rc

    server = build_server(fleet, archive)
    server.run(transport=config.server.mcp_transport)
    return 0


async def _print_tools(server: FastMCP) -> None:
    tools = await server.list_tools()
    width = max((len(t.name) for t in tools), default=0)
    for t in tools:
        title = getattr(t, "title", None) or t.name
        first_line = ""
        if t.description:
            first_line = t.description.strip().splitlines()[0][:80]
        print(f"  {t.name.ljust(width)}  [{title}]  {first_line}")
    print(f"\n  ({len(tools)} tools registered)")


async def _print_resources(server: FastMCP) -> None:
    concrete = await server.list_resources()
    templates = await server.list_resource_templates()

    if concrete:
        print("Concrete resources:")
        for r in concrete:
            mime = getattr(r, "mimeType", None) or "?"
            title = getattr(r, "title", None) or r.name
            print(f"  {r.uri!s:42}  [{title}]  ({mime})")

    if templates:
        print("\nTemplates:")
        for t in templates:
            mime = getattr(t, "mimeType", None) or "?"
            title = getattr(t, "title", None) or t.name
            print(f"  {t.uriTemplate:42}  [{title}]  ({mime})")

    n = len(concrete) + len(templates)
    print(f"\n  ({len(concrete)} concrete + {len(templates)} templates "
          f"= {n} resources)")


async def _run_discover(timeout_ms: int) -> int:
    """CLI mode for --discover. Returns 0 if at least one MCPd
    responded, 1 otherwise."""
    from .transports import discovery as discovery_transport

    targets = await discovery_transport.discover(
        timeout_s=timeout_ms / 1000.0
    )
    if not targets:
        print(f"  (no MCPd responded within {timeout_ms}ms)")
        return 1
    width = max(len(t["endpoint"]) for t in targets)
    for t in targets:
        ep = t["endpoint"].ljust(width)
        print(f"  {ep}  {t['server']:12}  proto={t['protocol']:4}  "
              f"host={t['host']:18}  methods={t['methods']:>3}  "
              f"({t['latency_ms']}ms)")
    return 0


async def _run_health_check(fleet: Fleet) -> int:
    """Probe every target's MCPd channel + report status. Exit code
    is the number of unreachable / errored targets (0 = all healthy)."""
    import asyncio

    from .errors import JsonRpcError, NotCapable

    targets = fleet.list_targets()
    if not targets:
        print("(no targets configured)")
        return 0

    width = max(len(n) for n in targets)
    fail = 0

    async def _probe(name: str) -> tuple[str, str | None, dict[str, Any] | None]:
        cfg = fleet.target_config(name)
        mc = cfg.channels.mcpd
        if mc is None or not mc.enabled:
            return name, "no MCPd channel configured", None
        try:
            t = fleet.mcpd(name)
            caps = await asyncio.wait_for(t.request("proto.capabilities"), 5.0)
            try:
                ver_raw = await asyncio.wait_for(t.request("sys.version"), 5.0)
                kick = ver_raw.get("kickstart") or "?"
            except (TimeoutError, JsonRpcError, NotCapable):
                kick = "?"
            return name, None, {
                "endpoint": mc.endpoint,
                "type": cfg.type,
                "method_count": len(caps.get("methods", [])),
                "server": caps.get("server", "?"),
                "kickstart": kick,
            }
        except (JsonRpcError, NotCapable) as e:
            return name, f"{type(e).__name__}: {e}", None
        except TimeoutError:
            return name, "timeout", None
        except OSError as e:
            return name, str(e), None

    results = await asyncio.gather(*(_probe(n) for n in targets))
    for name, err, info in results:
        pad = name.ljust(width)
        if err:
            print(f"  {pad}  DOWN  {err}")
            fail += 1
        else:
            assert info is not None
            print(f"  {pad}  UP    {info['type']:6} "
                  f"mcpd={info['endpoint']:22}"
                  f"  methods={info['method_count']:>3}"
                  f"  kickstart={info['kickstart']}")

    await fleet.close_all()
    return fail


if __name__ == "__main__":
    sys.exit(main())
