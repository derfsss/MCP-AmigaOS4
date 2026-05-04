"""debug.* - whole-system debug via QEMU GDB stub.

Uses QEMU's `-gdb tcp::PORT` stub for whole-system memory + register
inspection. PowerPC CPU state only; per-task / IDebug integration
is provided separately by the in-guest debug.* methods.

GPR layout for the PowerPC e500 / pegasos2 stub:
  64 GPRs (32 + GPR + LR/CTR/XER/MSR/PC/CR/...) - we expose the raw
  blob plus a parsed `gpr` array (registers 0..31) so callers don't
  have to know the byte order. The blob is whatever QEMU's gdb stub
  returns; SDK doesn't ship a manifest of register order so we
  document the offset assumptions next to the parser.
"""

from __future__ import annotations

import base64
import struct

from pydantic import BaseModel, Field

from ..fleet import Fleet


class DebugReadRegistersResult(BaseModel):
    target: str
    raw_size: int
    raw_b64: str = ""  # whole register dump, base64 (truncated if huge)
    gpr: list[int] = []  # GPR0..GPR31 if we can parse them
    pc: int | None = None  # phase-5c partial: best-effort


class DebugReadMemoryResult(BaseModel):
    target: str
    addr: int
    length: int
    content_b64: str


class DebugStopReason(BaseModel):
    target: str
    reply: str


# PowerPC GPR width = 4 bytes (32-bit pegasos2). 32 GPRs first in
# the gdb register dump.
_GPR_COUNT = 32
_GPR_WIDTH = 4


def _parse_gprs(blob: bytes) -> list[int]:
    n = min(_GPR_COUNT, len(blob) // _GPR_WIDTH)
    out: list[int] = []
    for i in range(n):
        # PowerPC stub emits big-endian register values.
        (v,) = struct.unpack(">I", blob[i * _GPR_WIDTH:(i + 1) * _GPR_WIDTH])
        out.append(v)
    return out


async def debug_read_registers(
    fleet: Fleet, target: str
) -> DebugReadRegistersResult:
    gdb = fleet.gdb(target)
    blob = await gdb.read_registers()
    gprs = _parse_gprs(blob)
    return DebugReadRegistersResult(
        target=target,
        raw_size=len(blob),
        raw_b64=base64.b64encode(blob).decode("ascii"),
        gpr=gprs,
        pc=None,
    )


async def debug_read_memory(
    fleet: Fleet, target: str, addr: int, length: int
) -> DebugReadMemoryResult:
    gdb = fleet.gdb(target)
    data = await gdb.read_memory(addr, length)
    return DebugReadMemoryResult(
        target=target,
        addr=addr,
        length=len(data),
        content_b64=base64.b64encode(data).decode("ascii"),
    )


async def debug_stop_reason(fleet: Fleet, target: str) -> DebugStopReason:
    gdb = fleet.gdb(target)
    return DebugStopReason(target=target, reply=await gdb.stop_reason())


class DebugDetachResult(BaseModel):
    target: str
    detached: bool


async def debug_detach(fleet: Fleet, target: str) -> DebugDetachResult:
    """Detach the GDB stub so the CPU resumes normal execution.

    Note: any subsequent debug.* call on this target will re-attach,
    which will pause the CPU again. The user controls the pause
    window explicitly by interleaving detach + reattach.
    """
    gdb = fleet.gdb(target)
    await gdb.detach()
    return DebugDetachResult(target=target, detached=True)


# ---------- breakpoints + step (phase 5c continued) ---------------


class DebugBreakpointResult(BaseModel):
    target: str
    addr: int
    hardware: bool
    reply: str


class DebugStepResult(BaseModel):
    target: str
    reply: str


async def debug_set_breakpoint(
    fleet: Fleet, target: str, addr: int,
    *, kind: int = 4, hardware: bool = False,
) -> DebugBreakpointResult:
    """Z0 (sw) / Z1 (hw) packet. `kind` defaults to 4 (PowerPC insn)."""
    gdb = fleet.gdb(target)
    reply = await gdb.set_breakpoint(addr, kind, hardware)
    return DebugBreakpointResult(
        target=target, addr=addr, hardware=hardware, reply=reply,
    )


async def debug_clear_breakpoint(
    fleet: Fleet, target: str, addr: int,
    *, kind: int = 4, hardware: bool = False,
) -> DebugBreakpointResult:
    """z0 (sw) / z1 (hw) packet."""
    gdb = fleet.gdb(target)
    reply = await gdb.clear_breakpoint(addr, kind, hardware)
    return DebugBreakpointResult(
        target=target, addr=addr, hardware=hardware, reply=reply,
    )


async def debug_step(fleet: Fleet, target: str) -> DebugStepResult:
    """Single-step one instruction (`s` packet). Returns the stop reply
    string (typically `T05thread:NN;...`). Works while CPU is paused -
    the stub executes one instruction and reports the new state."""
    gdb = fleet.gdb(target)
    reply = await gdb.step()
    return DebugStepResult(target=target, reply=reply)


# ---------- continue + backtrace (phase 5c rest) -----------------


class DebugContinueResult(BaseModel):
    target: str
    reply: str
    interrupted: bool  # True if we had to send Ctrl-C


class DebugBacktraceFrame(BaseModel):
    index: int
    pc: int
    sp: int


class DebugBacktraceResult(BaseModel):
    target: str
    frames: list[DebugBacktraceFrame]
    truncated: bool = False
    raw_pc: int
    raw_sp: int
    raw_lr: int


# PowerPC GDB-stub register numbers (gdb's powerpc-32.xml convention -
# QEMU's pegasos2 stub follows the same numbering). If a future stub
# variant disagrees, we may need to query qXfer:features:read:target.xml
# at startup. Empirical confirmation in phase-5c-rest validation.
_PPC_REG_R1 = 1
_PPC_REG_PC = 64
_PPC_REG_LR = 67


async def debug_continue(
    fleet: Fleet, target: str, timeout_s: float = 5.0,
) -> DebugContinueResult:
    """Resume CPU execution and wait for the next halt.

    On `timeout_s` expiry, sends Ctrl-C (raw 0x03) to interrupt the
    CPU. The reply field carries the stop notification either way;
    `interrupted` distinguishes the two paths.
    """
    import time as _time
    gdb = fleet.gdb(target)
    t0 = _time.monotonic()
    reply = await gdb.cont(timeout_s)
    elapsed = _time.monotonic() - t0
    # If we exceeded the user's timeout by ~5s, that's the
    # interrupt path having fired (cont() catches the timeout
    # internally and sends Ctrl-C).
    interrupted = elapsed >= timeout_s
    return DebugContinueResult(
        target=target, reply=reply, interrupted=interrupted,
    )


# ---------- IDebug per-task snapshot (phase 5c IDebug) ------------


class TaskSnapshotRegisters(BaseModel):
    pc: int
    sp: int
    lr: int
    msr: int
    ctr: int
    xer: int
    cr: int
    traptype: int
    dar: int
    dsisr: int
    gpr: list[int]


class TaskSnapshotFrame(BaseModel):
    index: int
    pc: int
    sp: int


class TaskSnapshotResult(BaseModel):
    target: str
    name: str
    priority: int
    state: str
    rtcf_filled: int
    registers: TaskSnapshotRegisters
    backtrace: list[TaskSnapshotFrame]


class DebugSymbolResult(BaseModel):
    address: int
    resolved: bool
    type: int = 0
    module: str | None = None
    offset: int = 0
    segment: int = 0
    segment_offset: int = 0
    source_file: str | None = None
    function: str | None = None
    source_base: str | None = None
    source_line: int | None = None


class DebugStacktraceFrame(BaseModel):
    index: int
    state: int
    state_name: str
    address: int
    stack_pointer: int
    module: str | None = None
    function: str | None = None
    source_file: str | None = None
    source_line: int | None = None
    offset: int | None = None


class DebugStacktraceResult(BaseModel):
    name: str
    stacktrace_rc: int
    frame_count: int
    frames: list[DebugStacktraceFrame]


class DebugWriteMemoryResult(BaseModel):
    address: int
    bytes_written: int


class DebugWriteRegisterResult(BaseModel):
    """Daemon returns the field as `register`; alias to dodge the
    Pydantic warning that `register` shadows BaseModel.register."""

    model_config = {"populate_by_name": True}

    name: str
    register_name: str = Field(alias="register")
    value: int
    wtcf_filled: int


async def debug_symbol(
    fleet: Fleet, target: str, address: int,
) -> DebugSymbolResult:
    """Resolve an address to a DebugSymbol via IDebug->ObtainDebugSymbol."""
    raw = await fleet.mcpd(target).request(
        "debug.symbol", {"address": int(address)},
    )
    return DebugSymbolResult.model_validate(raw)


async def debug_stacktrace(
    fleet: Fleet, target: str, name: str, max_frames: int = 32,
) -> DebugStacktraceResult:
    """Symbolicated backtrace via IDebug->StackTrace + ObtainDebugSymbol."""
    raw = await fleet.mcpd(target).request(
        "debug.stacktrace",
        {"name": name, "max_frames": int(max_frames)},
    )
    return DebugStacktraceResult.model_validate(raw)


async def debug_write_memory(
    fleet: Fleet, target: str, address: int, bytes_b64: str,
) -> DebugWriteMemoryResult:
    """Write base64-decoded bytes to a memory address. DESTRUCTIVE -
    no per-task memory protection on AOS4 classic. Daemon requires
    confirm:true, which this wrapper sets unconditionally."""
    raw = await fleet.mcpd(target).request(
        "debug.write_memory",
        {"address": int(address), "bytes_b64": bytes_b64,
         "confirm": True},
    )
    return DebugWriteMemoryResult.model_validate(raw)


async def debug_write_register(
    fleet: Fleet, target: str, name: str, register: str, value: int,
) -> DebugWriteRegisterResult:
    """Modify one register in a task's saved ExceptionContext.
    Register names: pc, lr, ctr, xer, cr, msr, dar, dsisr, or gpr0..gpr31.
    """
    raw = await fleet.mcpd(target).request(
        "debug.write_register",
        {"name": name, "register": register, "value": int(value),
         "confirm": True},
    )
    return DebugWriteRegisterResult.model_validate(raw)


async def debug_task_snapshot(
    fleet: Fleet, target: str, name: str, max_frames: int = 16,
) -> TaskSnapshotResult:
    """Snapshot a named AmigaOS task on the guest.

    Goes through MCPd (NOT the GDB stub); the daemon uses
    IDebug->ReadTaskContext to capture the task's saved register set
    and walks the SVR4 frame chain in the task's own stack under
    Forbid(). Returns registers (PC/SP/LR + 32 GPRs + traptype/dar
    if it was an exception context) and the backtrace.
    """
    raw = await fleet.mcpd(target).request(
        "debug.task_snapshot",
        {"name": name, "max_frames": max_frames},
    )
    return TaskSnapshotResult(target=target, **raw)


async def debug_backtrace(
    fleet: Fleet, target: str, max_frames: int = 16,
) -> DebugBacktraceResult:
    """Walk the PowerPC SVR4 frame chain.

    Reads PC + SP (r1) + LR via the `p` packet, then for each frame
    reads two 4-byte big-endian words from guest memory:
    `*(SP)` = back chain (caller's SP) and `*(back_chain + 4)` =
    saved LR (return address from current to caller).

    Stops when back_chain is 0/all-ones, when the chain doesn't
    grow upward (sanity), or when `max_frames` is hit (truncated=True).
    """
    import struct as _struct

    gdb = fleet.gdb(target)
    pc = await gdb.read_one_register(_PPC_REG_PC)
    sp = await gdb.read_one_register(_PPC_REG_R1)
    lr = await gdb.read_one_register(_PPC_REG_LR)

    frames: list[DebugBacktraceFrame] = [
        DebugBacktraceFrame(index=0, pc=pc, sp=sp),
    ]
    cur_sp = sp
    truncated = False

    for i in range(1, max_frames):
        if cur_sp == 0 or cur_sp == 0xffffffff:
            break
        try:
            bc_bytes = await gdb.read_memory(cur_sp, 4)
        except Exception:
            break
        if len(bc_bytes) != 4:
            break
        (back_sp,) = _struct.unpack(">I", bc_bytes)
        if back_sp == 0 or back_sp == 0xffffffff:
            break
        # Sanity: stack should grow upward (lower SPs are deeper).
        if back_sp <= cur_sp:
            break
        try:
            lr_bytes = await gdb.read_memory(back_sp + 4, 4)
        except Exception:
            break
        if len(lr_bytes) != 4:
            break
        (saved_lr,) = _struct.unpack(">I", lr_bytes)
        frames.append(
            DebugBacktraceFrame(index=i, pc=saved_lr, sp=back_sp),
        )
        cur_sp = back_sp
    else:
        truncated = True

    return DebugBacktraceResult(
        target=target, frames=frames, truncated=truncated,
        raw_pc=pc, raw_sp=sp, raw_lr=lr,
    )
