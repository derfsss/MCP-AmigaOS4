"""sys.* introspection methods + host-level proto.capabilities."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from ..fleet import Fleet


class VersionResult(BaseModel):
    raw: str
    kickstart: str | None = None
    workbench: str | None = None


async def sys_version(fleet: Fleet, target: str) -> VersionResult:
    t = fleet.mcpd(target)
    raw = await t.request("sys.version")
    return VersionResult.model_validate(raw)


# ---------- introspection (phase 5a) -------------------------------


class TaskInfo(BaseModel):
    name: str
    type: str
    priority: int
    state: str


class TasksResult(BaseModel):
    ready: list[TaskInfo]
    waiting: list[TaskInfo]


class LibraryInfo(BaseModel):
    name: str
    version: int
    revision: int
    open_count: int


class LibrariesResult(BaseModel):
    libraries: list[LibraryInfo]


class DevicesResult(BaseModel):
    devices: list[LibraryInfo]


class PortInfo(BaseModel):
    name: str
    priority: int
    flags: int


class PortsResult(BaseModel):
    ports: list[PortInfo]


class LastAlertResult(BaseModel):
    alert_code: int
    values: list[int]


async def sys_tasks(fleet: Fleet, target: str) -> TasksResult:
    raw = await fleet.mcpd(target).request("sys.tasks")
    return TasksResult.model_validate(raw)


async def sys_libraries(fleet: Fleet, target: str) -> LibrariesResult:
    raw = await fleet.mcpd(target).request("sys.libraries")
    return LibrariesResult(
        libraries=[LibraryInfo.model_validate(e) for e in raw]
    )


async def sys_devices(fleet: Fleet, target: str) -> DevicesResult:
    raw = await fleet.mcpd(target).request("sys.devices")
    return DevicesResult(
        devices=[LibraryInfo.model_validate(e) for e in raw]
    )


async def sys_ports(fleet: Fleet, target: str) -> PortsResult:
    raw = await fleet.mcpd(target).request("sys.ports")
    return PortsResult(
        ports=[PortInfo.model_validate(e) for e in raw]
    )


async def sys_lastalert(fleet: Fleet, target: str) -> LastAlertResult:
    raw: dict[str, Any] = await fleet.mcpd(target).request("sys.lastalert")
    return LastAlertResult.model_validate(raw)


# ---------- phase-7 surface (sys.uptime / memory / volumes / assigns) ----


class UptimeResult(BaseModel):
    seconds: float
    eclock_freq: float


class MemorySliceResult(BaseModel):
    free: int
    largest: int = 0
    total: int = 0


class MemoryResult(BaseModel):
    any: MemorySliceResult
    shared: MemorySliceResult
    virtual: MemorySliceResult


class VolumeInfo(BaseModel):
    name: str
    type: int
    port: int = 0


class VolumesResult(BaseModel):
    volumes: list[VolumeInfo]


class AssignInfo(BaseModel):
    name: str
    type: int


class AssignsResult(BaseModel):
    assigns: list[AssignInfo]


async def sys_uptime(fleet: Fleet, target: str) -> UptimeResult:
    raw = await fleet.mcpd(target).request("sys.uptime")
    return UptimeResult.model_validate(raw)


async def sys_memory(fleet: Fleet, target: str) -> MemoryResult:
    raw = await fleet.mcpd(target).request("sys.memory")
    return MemoryResult.model_validate(raw)


async def sys_volumes(fleet: Fleet, target: str) -> VolumesResult:
    raw = await fleet.mcpd(target).request("sys.volumes")
    return VolumesResult(
        volumes=[VolumeInfo.model_validate(e) for e in raw]
    )


async def sys_assigns(fleet: Fleet, target: str) -> AssignsResult:
    raw = await fleet.mcpd(target).request("sys.assigns")
    return AssignsResult(
        assigns=[AssignInfo.model_validate(e) for e in raw]
    )


# ---------- sys.hardware (round 3) --------------------------------


class CpuInfo(BaseModel):
    count: int = 0
    family_id: int = 0
    family: str = "unknown"
    model_id: int = 0
    model: str = "unknown"
    model_string: str | None = None
    speed_hz: int = 0
    fsb_hz: int = 0
    timebase_hz: int = 0
    l1_cache: int = 0
    l2_cache: int = 0
    l3_cache: int = 0
    vector_unit: int = 0
    page_size: int = 0
    cache_line: int = 0


class ResourceProbes(BaseModel):
    xena: bool = False
    i2c: bool = False
    acpi: bool = False
    performancemonitor: bool = False
    fsldma: bool = False


class HardwareResult(BaseModel):
    attn_flags: int
    attn_flags_decoded: list[str] = []
    cpu: CpuInfo
    resources: ResourceProbes


async def sys_hardware(fleet: Fleet, target: str) -> HardwareResult:
    raw = await fleet.mcpd(target).request("sys.hardware")
    return HardwareResult.model_validate(raw)


# ---------- sys.hardware.i2c -------------------------------------


class I2CDevice(BaseModel):
    addr: int
    device_id: int | None = None
    hint: str | None = None


class I2CBus(BaseModel):
    bus_number: int
    name: str | None = None
    devices: list[I2CDevice] = []


class I2CResult(BaseModel):
    available: bool
    error: str | None = None
    buses: list[I2CBus] = []


async def sys_hardware_i2c(
    fleet: Fleet, target: str,
    addr_low: int = 0x08, addr_high: int = 0x77,
) -> I2CResult:
    raw = await fleet.mcpd(target).request(
        "sys.hardware.i2c",
        {"addr_low": int(addr_low), "addr_high": int(addr_high)},
        timeout_s=60.0,
    )
    return I2CResult.model_validate(raw)


# ---------- sys.hardware.perfcounters ----------------------------


class PerfCounter(BaseModel):
    index: int
    item_id: int
    item: str
    value: int


class PerfCountersResult(BaseModel):
    available: bool
    error: str | None = None
    counter_count: int = 0
    instr_breakpoint: bool = False
    breakpoint_mask: bool = False
    counters: list[PerfCounter] = []


async def sys_hardware_perfcounters(
    fleet: Fleet, target: str,
) -> PerfCountersResult:
    raw = await fleet.mcpd(target).request("sys.hardware.perfcounters")
    return PerfCountersResult.model_validate(raw)


# ---------- sys.executable.symbols -------------------------------


class ElfSymbol(BaseModel):
    name: str
    value: int
    size: int
    abs_value: int
    binding: int
    type: int
    binding_name: str | None = None
    type_name: str | None = None


class ExecutableSymbolsResult(BaseModel):
    path: str
    num_sections: int
    symbol_count: int
    truncated: bool
    symbols: list[ElfSymbol]


async def sys_executable_symbols(
    fleet: Fleet, target: str, path: str,
    *, match: str | None = None,
    binding: str | None = None,
    type: str | None = None,
    max: int = 256,
) -> ExecutableSymbolsResult:
    """Read the ELF symbol table of `path` on `target`.

    Optional filters narrow the result:
      * `match` - case-sensitive substring match on symbol name
      * `binding` - "local" / "global" / "weak"
      * `type` - "notype" / "object" / "func" / "section" / "file"
    """
    params: dict[str, object] = {"path": path, "max": int(max)}
    if match:
        params["match"] = match
    if binding:
        params["binding"] = binding
    if type:
        params["type"] = type
    raw = await fleet.mcpd(target).request(
        "sys.executable.symbols", params, timeout_s=120.0,
    )
    return ExecutableSymbolsResult.model_validate(raw)


# ---------- sys.applications + app.notify ------------------------


class AppEntry(BaseModel):
    appID: int
    name: str | None = None
    url_identifier: str | None = None
    filename: str | None = None
    description: str | None = None
    hidden: bool = False


class ApplicationsResult(BaseModel):
    available: bool
    applications: list[AppEntry] = []


async def sys_applications(
    fleet: Fleet, target: str,
) -> ApplicationsResult:
    raw = await fleet.mcpd(target).request("sys.applications")
    return ApplicationsResult.model_validate(raw)


class NotifyResult(BaseModel):
    result_code: int
    queued: bool


async def app_notify(
    fleet: Fleet, target: str, title: str, text: str, priority: int = 0,
) -> NotifyResult:
    """Post a Ringhio popup notification on `target`. Requires
    application.library v53.11+ on the daemon side."""
    raw = await fleet.mcpd(target).request(
        "app.notify",
        {"title": title, "text": text, "priority": int(priority)},
    )
    return NotifyResult.model_validate(raw)


# ---------- notify_fleet (broadcast) -----------------------------


import asyncio  # noqa: E402


class NotifyFleetEntry(BaseModel):
    target: str
    ok: bool
    result_code: int = 0
    error: str | None = None


class NotifyFleetResult(BaseModel):
    title: str
    text: str
    priority: int
    results: list[NotifyFleetEntry]


async def notify_fleet(
    fleet: Fleet, title: str, text: str,
    priority: int = 0,
    targets: list[str] | None = None,
    tags: list[str] | None = None,
) -> NotifyFleetResult:
    """Post the same Ringhio notification to every target in parallel.

    `targets` selects an explicit subset; `tags` filters by per-target
    tags (AND-match, like fleet.run_on_all). Pass neither to broadcast
    to every configured target.
    """
    if targets is None:
        targets = fleet.list_targets(tags=tags)

    async def _one(name: str) -> NotifyFleetEntry:
        try:
            r = await app_notify(fleet, name, title, text, priority)
            return NotifyFleetEntry(
                target=name, ok=r.queued, result_code=r.result_code,
            )
        except Exception as e:
            return NotifyFleetEntry(target=name, ok=False, error=str(e))

    entries = await asyncio.gather(*(_one(t) for t in targets))
    return NotifyFleetResult(
        title=title, text=text, priority=int(priority),
        results=list(entries),
    )


# ---------- notify_on_alert (event-driven) -----------------------


class NotifyOnAlertResult(BaseModel):
    target: str
    notified: bool
    elapsed_ms: int
    alert_code: int | None = None
    alert_name: str | None = None
    notify_result_code: int | None = None
    timeout: bool = False


async def notify_on_alert(
    fleet: Fleet, target: str,
    timeout_ms: int = 60000,
    title: str = "MCPd alert detected",
) -> NotifyOnAlertResult:
    """Long-poll for a `debug.exception` event on `target`; if one
    fires, post a Ringhio popup with the decoded alert info. Returns
    the notify result (or `timeout=True` if no event).

    Useful as a host-side wrapper that turns the daemon's long-poll
    event channel into a user-visible notification.
    """
    import time
    t = fleet.mcpd(target)
    t0 = time.monotonic()
    raw = await t.request(
        "events.wait",
        {"topics": ["debug.exception"], "timeout_ms": int(timeout_ms)},
        timeout_s=max(timeout_ms / 1000.0 + 5.0, 30.0),
    )
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    events = raw.get("events", [])
    if not events:
        return NotifyOnAlertResult(
            target=target, notified=False, elapsed_ms=elapsed_ms,
            timeout=True,
        )
    ev = events[0]
    data = ev.get("data", {})
    decoded = data.get("decoded", {})
    code = data.get("alert_code")
    name = decoded.get("name") or "alert"
    body = (
        f"code=0x{code:08x}\n"
        f"subsystem={decoded.get('subsystem', '?')}\n"
        f"name={name}"
    )
    nf = await app_notify(fleet, target, title, body, priority=2)
    return NotifyOnAlertResult(
        target=target, notified=nf.queued, elapsed_ms=elapsed_ms,
        alert_code=code, alert_name=name,
        notify_result_code=nf.result_code,
    )


# ---------- sys.alert_decode ------------------------------------


class AlertDecoded(BaseModel):
    code: int
    dead_end: bool
    subsystem_id: int
    subsystem: str
    general_id: int
    general: str | None = None
    specific: int
    name: str | None = None


class AlertDecodeResult(BaseModel):
    code: int
    decoded: AlertDecoded


async def sys_alert_decode(
    fleet: Fleet, target: str, code: int,
) -> AlertDecodeResult:
    raw = await fleet.mcpd(target).request(
        "sys.alert_decode", {"code": int(code)},
    )
    return AlertDecodeResult.model_validate(raw)


# ---------- sys.cold_reboot ---------------------------------------


class ColdRebootResult(BaseModel):
    queued: bool
    delay_ms: int


async def sys_cold_reboot(
    fleet: Fleet, target: str, confirm: bool, delay_ms: int = 500,
) -> ColdRebootResult:
    """Software cold reboot via IExec->ColdReboot.

    The daemon spawns a tiny delayed-reboot subtask so the JSON-RPC
    reply flushes before the machine resets. `confirm` must be true
    (gate against accidents). `delay_ms` is clamped daemon-side to
    [100, 5000]. Returns once the subtask is queued; the caller can
    poll `fleet.target_status` to detect when the machine is back.
    """
    raw = await fleet.mcpd(target).request(
        "sys.cold_reboot",
        {"confirm": bool(confirm), "delay_ms": int(delay_ms)},
    )
    return ColdRebootResult.model_validate(raw)


# ---------- proto.capabilities (host side) -------------------------


class CapabilitiesResult(BaseModel):
    server: str
    version: str
    methods: list[str]
    targets: list[str]


class ReadCcsrResult(BaseModel):
    target: str
    ccsr_base: int
    offset: int
    count: int
    values: list[int]
    values_hex: list[str]


async def sys_read_ccsr(
    fleet: Fleet, target: str, *,
    offset: int, count: int = 1,
) -> ReadCcsrResult:
    """Read 32-bit register(s) from the Freescale QorIQ CCSR
    (CPU PA `0xFE000000 + offset`) on a P5020 / P1022 target.
    Used for SoC introspection — PCIe outbound translation windows
    (PEXOWAR0/PEXOTAR0), Local Access Windows, etc — when bringing
    up a QEMU machine model.
    """
    raw = await fleet.mcpd(target).request(
        "sys.read_ccsr",
        {"offset": offset, "count": count},
    )
    return ReadCcsrResult(
        target=target,
        ccsr_base=int(raw["ccsr_base"]),
        offset=int(raw["offset"]),
        count=int(raw["count"]),
        values=[int(v) for v in raw["values"]],
        values_hex=list(raw["values_hex"]),
    )


class ReadPaResult(BaseModel):
    target: str
    pa: int
    count: int
    width: int
    bytes: int
    values: list[int]
    values_hex: list[str]
    bytes_b64: str | None = None


async def sys_read_pa(
    fleet: Fleet, target: str, *,
    pa: int, count: int = 1, width: int = 4,
) -> ReadPaResult:
    """DANGEROUS: supervisor-mode read at any 36-bit physical address.

    *** A WRONG `pa` CRASHES THE CONNECTION-HANDLER TASK on the
    target. *** The daemon main listener stays alive (MCPd is spawn-
    per-connection — fault isolation by design), but THIS connection
    drops mid-call. Reconnect to continue. Other in-flight RPCs from
    other clients are unaffected.

    width = 1 (byte), 2 (halfword), or 4 (word). pa must be aligned
    to width. count*width <= 4096 per call.

    Returns decoded values + values_hex + bytes_b64 (raw blob, useful
    for 256/1024-byte structure dumps fed to a hex viewer).

    --- WHICH PAs ARE SAFE? ---
    Empirically-verified safe regions on real X5000:
      * CCSR aperture: 0xFE000000 .. 0xFEFFFFFF
        (all on-chip peripheral registers — DUART, SATA, MPIC, etc.)
      * Explicit-IPROT'd kernel RAM: 0x01000000 .. 0x0246FFFF
        (kernel DMA buffers, SATA CHBA-class structures, etc.)

    UNSAFE (DSI traps even though TLB1 nominally covers them):
      * 0x00000000 .. 0x00FFFFFF — kernel-critical low memory
      * 0x02470000 .. 0x3FFFFFFF — gap, kernel-protected
      * 0x40000000 .. 0x7FFFFFFF — TLB1 entry 63 territory; some
        addresses readable, some trap (use sys.tlb_dump + caution)
      * 0x80000000+ outside CCSR — entirely unmapped, hard crash
      * 0xFF000000+ — past CCSR aperture, traps

    Run `sys.tlb_dump` first if you need a non-CCSR / non-explicit-
    IPROT address. Even mapped regions can trap kernel-side.
    """
    raw = await fleet.mcpd(target).request(
        "sys.read_pa",
        {"pa": int(pa), "count": int(count), "width": int(width)},
    )
    return ReadPaResult(
        target=target,
        pa=int(raw["pa"]),
        count=int(raw["count"]),
        width=int(raw["width"]),
        bytes=int(raw.get("bytes", raw["count"] * raw["width"])),
        values=[int(v) for v in raw["values"]],
        values_hex=list(raw["values_hex"]),
        bytes_b64=raw.get("bytes_b64"),
    )


class McuCmdResult(BaseModel):
    target: str
    cmd: str
    unit: int
    baud: int
    tx_len: int
    reply: str
    reply_len: int
    timed_out: bool
    aborted: bool
    parsed: dict[str, object] | None = None


# Voltage rail order, corrected per amigans.net thread (12 fields, not
# the 13 in the published TRM). Index = position in the $v reply.
_VOLT_RAILS = [
    "cpld",         # 3.3V CPLD
    "xorro_3v3",    # 3.3V Xorro (XenaXorroA)
    "idt_1v0",      # 1.0V IDT (was XenaXorroB in the TRM, non-functional)
    "xorro_1v0",    # 1.0V Xorro (was PCI in the TRM)
    "main_3v3",     # 3.3V main rail (was Xena in the TRM)
    "main_2v5",     # 2.5V main rail (was Main33)
    "eth_1v2",      # 1.2V Ethernet (was Main25)
    "platform_1v0", # 1.0V Platform (was Ethernet)
    "core_a",       # CoreA voltage (1.1V on P5020)
    "core_b",       # CoreB voltage (1.1V on P5020)
    "ddr3_io",      # 1.5V DDR3 IO
    "serdes_1v8",   # 1.8V SerDes (was DDR in the TRM)
]


def _parse_mcu_reply(cmd: str, reply: str) -> dict[str, object] | None:
    """Decode '$cmd...' replies per TRM 8.1 (with amigans.net corrections).
    Returns None if reply is malformed / wrong cmd / truncated."""
    if not reply or len(reply) < 2 or reply[0] != "$":
        return None
    if cmd == "t":
        # $t+HH+HH+HH\n  -> three signed-hex degC: PCB, CPU, PCIe-switch
        if reply[1] != "t" or len(reply) < 11:
            return None
        try:
            sign1, t1 = reply[2], int(reply[3:5], 16)
            sign2, t2 = reply[5], int(reply[6:8], 16)
            sign3, t3 = reply[8], int(reply[9:11], 16)
        except ValueError:
            return None
        return {
            "pcb_c":  -t1 if sign1 == "-" else t1,
            "cpu_c":  -t2 if sign2 == "-" else t2,
            "pcie_c": -t3 if sign3 == "-" else t3,
        }
    if cmd == "v":
        # $v + 12 * (XXYY) + \n  ->  XX volts whole, YY 10-mV units, both ASCII hex
        if reply[1] != "v" or len(reply) < 2 + 12 * 4:
            return None
        out: dict[str, object] = {}
        try:
            for i, name in enumerate(_VOLT_RAILS):
                base = 2 + i * 4
                whole = int(reply[base:base+2], 16)
                cents = int(reply[base+2:base+4], 16)
                out[name + "_v"] = round(whole + cents / 100.0, 3)
        except ValueError:
            return None
        return out
    if cmd == "f":
        # $fPPRRRR\n  ->  PWM byte (0..255) + RPM 16-bit, all ASCII hex
        if reply[1] != "f" or len(reply) < 8:
            return None
        try:
            pwm = int(reply[2:4], 16)
            rpm = int(reply[4:8], 16)
        except ValueError:
            return None
        return {"pwm": pwm, "pwm_pct": round(pwm / 255.0 * 100, 1), "rpm": rpm}
    return None


async def sys_mcu_cmd(
    fleet: Fleet, target: str, *,
    cmd: str, unit: int = 1, confirm: bool = False,
) -> McuCmdResult:
    """Talk to the X5000 Cyrus MCU via UART1 serial supervisor protocol
    (TRM Cyrus 1.1.1 sec 8.1).

    Sends '#<cmd>\\n' to serial.device unit (default 1) at 38400 8N1
    and reads back the '$<reply>\\n' response with a 5 s timeout.

    Documented commands:
      cmd='t'  -> 3 temperatures (PCB, CPU, PCIe-switch) as $t+HH+HH+HH
      cmd='v'  -> 13 voltage rails as $vXXYY repeating 13 times
      cmd='f'  -> fan PWM + RPM as $fPPRRRR
      cmd='s'  -> POWER OFF (board powers down). Use with care.

    The canonical sensor path on X5000: talks the documented MCU
    protocol directly via AOS's serial.device. Returns the raw reply
    string; callers parse the format documented in the TRM.

    cmd='s' (POWER OFF) requires confirm=True; otherwise the daemon
    rejects with InvalidParams. Mirrors the sys.cold_reboot guard.
    """
    params: dict[str, object] = {"cmd": cmd, "unit": int(unit)}
    if confirm:
        params["confirm"] = True
    raw = await fleet.mcpd(target).request("sys.mcu_cmd", params)
    reply = str(raw["reply"]).rstrip("\n")
    parsed = _parse_mcu_reply(cmd, reply) if not raw.get("timed_out") else None
    return McuCmdResult(
        target=target,
        cmd=str(raw["cmd"]),
        unit=int(raw["unit"]),
        baud=int(raw["baud"]),
        tx_len=int(raw["tx_len"]),
        reply=reply,
        reply_len=int(raw["reply_len"]),
        timed_out=bool(raw["timed_out"]),
        aborted=bool(raw["aborted"]),
        parsed=parsed,
    )


class TlbEntry(BaseModel):
    entry: int
    v: int
    iprot: int
    tid: int
    ts: int
    tsize_raw: int
    epn: int
    wimge: int
    rpnh: int
    rpnl: int
    perms: int
    mas1: int
    mas2: int
    mas3: int
    mas7: int
    rpn_36bit_hex: str


class TlbDumpResult(BaseModel):
    target: str
    tlb: int
    entries: int
    table: list[TlbEntry]


async def sys_tlb_dump(fleet: Fleet, target: str) -> TlbDumpResult:
    """Dump all 64 TLB1 entries from a P5020 (X5000) or P1022 (A1222)
    target. TLB1 is the kernel's variable-size-page TLB used for all
    kernel + driver mappings, including CCSR and PCI windows. Lets us
    verify exactly what PA each kernel VA translates to (specifically:
    the high 4 bits of the RPN, which determine whether a window is
    in 36-bit-extended physical space).

    Read-only; safe to call on a running system. Some entries will be
    invalid (V=0) -- that's normal, the TLB is rarely fully used.
    """
    raw = await fleet.mcpd(target).request("sys.tlb_dump", {})
    return TlbDumpResult(
        target=target,
        tlb=int(raw["tlb"]),
        entries=int(raw["entries"]),
        table=[TlbEntry.model_validate(e) for e in raw["table"]],
    )


def host_capabilities(
    fleet: Fleet, server_version: str, methods: list[str],
) -> CapabilitiesResult:
    """Build the proto.capabilities envelope.

    `methods` is the list of advertised JSON-RPC method names (dotted
    form, e.g. "fs.write_chunk"). The caller passes the live list -
    typically derived from FastMCP's tool registry by walking
    `mcp.list_tools()` and pulling each tool's `title` - so the
    advertisement stays in sync with what's actually registered. No
    more hand-maintained PHASE_X constants drifting out of date.
    """
    return CapabilitiesResult(
        server="amiga-fleet-mcp",
        version=server_version,
        methods=sorted(methods),
        targets=fleet.list_targets(),
    )
