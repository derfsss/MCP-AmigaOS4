/* debug.* method handlers (in-guest debug via IDebug).
 *
 * IDebug per-task path: debug.task_snapshot reads a named AmigaOS
 * task's saved CPU context via IDebug->ReadTaskContext, then walks
 * the PowerPC SVR4 frame chain in the task's own stack to produce
 * a backtrace. The walk happens under Forbid() so the task can't
 * be rescheduled mid-snapshot.
 *
 * Cross-debug (whole-system memory + registers via QEMU's GDB stub)
 * is host-side - lives in transports/gdb.py + tools/debug.py. The
 * two views are complementary: the GDB stub sees raw CPU state; this
 * method sees AOS task state (per-task PC/SP, library list visible to
 * that task, etc.).
 */

#include "../rpc.h"
#include "methods.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <proto/exec.h>
#include <exec/exec.h>
#include <exec/tasks.h>
#include <exec/interrupts.h>
#include <exec/debug.h>
#include <utility/hooks.h>
#include <stdarg.h>

#include "../b64.h"


static const char *_task_state(BYTE st) {
    switch (st) {
        case TS_INVALID:   return "invalid";
        case TS_ADDED:     return "added";
        case TS_RUN:       return "running";
        case TS_READY:     return "ready";
        case TS_WAIT:      return "waiting";
        case TS_EXCEPT:    return "exception";
        case TS_REMOVED:   return "removed";
        default:           return "unknown";
    }
}


static char *_sanitize(const char *src, char *dst, size_t cap) {
    if (!src) src = "";
    size_t n = 0;
    while (src[n] && n < cap - 1) {
        unsigned char c = (unsigned char)src[n];
        dst[n] = (c >= 0x20 && c < 0x7f) ? (char)c : '?';
        n++;
    }
    dst[n] = '\0';
    return dst;
}


/* Walk a PowerPC SVR4 frame chain from `start_sp` and write up to
 * `max` (pc, sp) pairs into `out_pc / out_sp`. Stops on null
 * back-chain, sentinel (-1), or non-monotonic SP. Caller has
 * Forbid()'d so the chain doesn't move mid-walk.
 *
 * Returns the number of frames written (not counting frame 0 which
 * the caller has already filled from the saved register set).
 */
static int _walk_chain(uint32_t start_sp, int max,
                       uint32_t *out_pc, uint32_t *out_sp) {
    int n = 0;
    uint32_t cur = start_sp;
    while (n < max) {
        if (cur == 0 || cur == 0xffffffff) break;
        uint32_t back_sp = *(volatile uint32_t *)cur;
        if (back_sp == 0 || back_sp == 0xffffffff) break;
        if (back_sp <= cur) break;  /* sanity: stack grows up. */
        uint32_t saved_lr = *(volatile uint32_t *)(back_sp + 4);
        out_pc[n] = saved_lr;
        out_sp[n] = back_sp;
        n++;
        cur = back_sp;
    }
    return n;
}


int debug_task_snapshot(cJSON *params, cJSON **out_result, cJSON **out_err) {
    const char *name = p_str(params, "name", out_err);
    if (!name) return 0;

    long long max_frames_ll = p_int(params, "max_frames", 16);
    int max_frames = (int)max_frames_ll;
    if (max_frames < 1) max_frames = 1;
    if (max_frames > 64) max_frames = 64;

    /* Snapshot under Forbid so the target task can't reschedule and
     * mutate its stack while we're walking. We capture everything we
     * need into local copies before Permit. */
    IExec->Forbid();

    struct Task *t = IExec->FindTask((STRPTR)name);
    if (!t) {
        IExec->Permit();
        cJSON *data = cJSON_CreateObject();
        if (data) cJSON_AddStringToObject(data, "name", name);
        *out_err = rpc_make_error(MCPD_ERR_TARGET, "Task not found", data);
        return 0;
    }

    struct ExceptionContext ctx;
    memset(&ctx, 0, sizeof(ctx));
    uint32_t rtcf = IDebug->ReadTaskContext(
        t, &ctx, RTCF_GENERAL | RTCF_STATE | RTCF_INFO
    );

    char tname_buf[128];
    _sanitize((const char *)t->tc_Node.ln_Name, tname_buf, sizeof(tname_buf));
    BYTE tstate = t->tc_State;
    int8_t tpri = t->tc_Node.ln_Pri;

    uint32_t pc = ctx.ip;
    uint32_t sp = ctx.gpr[1];
    uint32_t lr = ctx.lr;

    /* Walk the chain. We're reading the target task's stack memory -
     * legitimate because AOS classic has no per-task memory protection
     * and we're under Forbid. */
    uint32_t bt_pc[64];
    uint32_t bt_sp[64];
    int bt_n = _walk_chain(sp, max_frames - 1, bt_pc, bt_sp);

    IExec->Permit();

    /* Build the JSON result. */
    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "name", tname_buf);
    cJSON_AddNumberToObject(r, "priority", (double)tpri);
    cJSON_AddStringToObject(r, "state", _task_state(tstate));
    cJSON_AddNumberToObject(r, "rtcf_filled", (double)(unsigned)rtcf);

    cJSON *regs = cJSON_AddObjectToObject(r, "registers");
    cJSON_AddNumberToObject(regs, "pc",       (double)(unsigned)pc);
    cJSON_AddNumberToObject(regs, "sp",       (double)(unsigned)sp);
    cJSON_AddNumberToObject(regs, "lr",       (double)(unsigned)lr);
    cJSON_AddNumberToObject(regs, "msr",      (double)(unsigned)ctx.msr);
    cJSON_AddNumberToObject(regs, "ctr",      (double)(unsigned)ctx.ctr);
    cJSON_AddNumberToObject(regs, "xer",      (double)(unsigned)ctx.xer);
    cJSON_AddNumberToObject(regs, "cr",       (double)(unsigned)ctx.cr);
    cJSON_AddNumberToObject(regs, "traptype", (double)(unsigned)ctx.Traptype);
    cJSON_AddNumberToObject(regs, "dar",      (double)(unsigned)ctx.dar);
    cJSON_AddNumberToObject(regs, "dsisr",    (double)(unsigned)ctx.dsisr);

    cJSON *gpr_arr = cJSON_AddArrayToObject(regs, "gpr");
    for (int i = 0; i < 32; i++) {
        cJSON_AddItemToArray(
            gpr_arr,
            cJSON_CreateNumber((double)(unsigned)ctx.gpr[i])
        );
    }

    cJSON *bt = cJSON_AddArrayToObject(r, "backtrace");
    {
        /* Frame 0 is the current PC/SP from the saved context. */
        cJSON *f0 = cJSON_CreateObject();
        cJSON_AddNumberToObject(f0, "index", 0.0);
        cJSON_AddNumberToObject(f0, "pc", (double)(unsigned)pc);
        cJSON_AddNumberToObject(f0, "sp", (double)(unsigned)sp);
        cJSON_AddItemToArray(bt, f0);
    }
    for (int i = 0; i < bt_n; i++) {
        cJSON *fr = cJSON_CreateObject();
        cJSON_AddNumberToObject(fr, "index", (double)(i + 1));
        cJSON_AddNumberToObject(fr, "pc", (double)(unsigned)bt_pc[i]);
        cJSON_AddNumberToObject(fr, "sp", (double)(unsigned)bt_sp[i]);
        cJSON_AddItemToArray(bt, fr);
    }

    *out_result = r;
    return 0;
}


/* ---- debug.symbol --------------------------------------------- */

/* Resolve an address to a DebugSymbol via IDebug->ObtainDebugSymbol.
 * Returns the module/source/line/function metadata if available. */
int debug_symbol(cJSON *params, cJSON **out_result, cJSON **out_err) {
    cJSON *addr_node = cJSON_GetObjectItemCaseSensitive(params, "address");
    if (!cJSON_IsNumber(addr_node)) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
                                  "missing required number param: address",
                                  NULL);
        return 0;
    }
    uint32_t addr = (uint32_t)addr_node->valuedouble;

    struct DebugSymbol *sym = IDebug->ObtainDebugSymbol(
        (CONST_APTR)(uintptr_t)addr, NULL);
    cJSON *r = cJSON_CreateObject();
    cJSON_AddNumberToObject(r, "address", (double)addr);
    if (!sym) {
        cJSON_AddBoolToObject(r, "resolved", 0);
        *out_result = r;
        return 0;
    }
    cJSON_AddBoolToObject(r, "resolved", 1);
    cJSON_AddNumberToObject(r, "type", (double)sym->Type);
    if (sym->Name) cJSON_AddStringToObject(r, "module", sym->Name);
    cJSON_AddNumberToObject(r, "offset", (double)sym->Offset);
    cJSON_AddNumberToObject(r, "segment", (double)sym->SegmentNumber);
    cJSON_AddNumberToObject(r, "segment_offset",
                            (double)sym->SegmentOffset);
    if (sym->SourceFileName)
        cJSON_AddStringToObject(r, "source_file", sym->SourceFileName);
    if (sym->SourceFunctionName)
        cJSON_AddStringToObject(r, "function", sym->SourceFunctionName);
    if (sym->SourceBaseName)
        cJSON_AddStringToObject(r, "source_base", sym->SourceBaseName);
    if (sym->SourceLineNumber)
        cJSON_AddNumberToObject(r, "source_line",
                                (double)sym->SourceLineNumber);
    IDebug->ReleaseDebugSymbol(sym);
    *out_result = r;
    return 0;
}


/* ---- debug.stacktrace ----------------------------------------- */

/* Hook callback that IDebug->StackTrace invokes for each frame. */
struct _stacktrace_ctx {
    cJSON *frames;
    int max_frames;
    int count;
};

static int _stacktrace_hook(struct Hook *hook, struct Task *t,
                            struct StackFrameMsg *msg) {
    (void)t;
    struct _stacktrace_ctx *c = (struct _stacktrace_ctx *)hook->h_Data;
    if (c->count >= c->max_frames) return 0;  /* stop */

    cJSON *frame = cJSON_CreateObject();
    cJSON_AddNumberToObject(frame, "index", (double)c->count);
    cJSON_AddNumberToObject(frame, "state", (double)msg->State);
    static const char *state_names[] = {
        "unknown", "decoded", "invalid_backchain",
        "trashed_memory_loop", "backchain_ptr_loop",
    };
    unsigned ix = (msg->State < 5) ? msg->State : 0;
    cJSON_AddStringToObject(frame, "state_name", state_names[ix]);
    cJSON_AddNumberToObject(frame, "address",
        (double)(uintptr_t)msg->MemoryAddress);
    cJSON_AddNumberToObject(frame, "stack_pointer",
        (double)(uintptr_t)msg->StackPointer);

    /* Symbolicate when we have a decoded frame. */
    if (msg->State == STACK_FRAME_DECODED && msg->MemoryAddress) {
        struct DebugSymbol *sym = IDebug->ObtainDebugSymbol(
            msg->MemoryAddress, NULL);
        if (sym) {
            if (sym->Name)
                cJSON_AddStringToObject(frame, "module", sym->Name);
            if (sym->SourceFunctionName)
                cJSON_AddStringToObject(frame, "function",
                                        sym->SourceFunctionName);
            if (sym->SourceFileName)
                cJSON_AddStringToObject(frame, "source_file",
                                        sym->SourceFileName);
            if (sym->SourceLineNumber)
                cJSON_AddNumberToObject(frame, "source_line",
                                        (double)sym->SourceLineNumber);
            cJSON_AddNumberToObject(frame, "offset", (double)sym->Offset);
            IDebug->ReleaseDebugSymbol(sym);
        }
    }
    cJSON_AddItemToArray(c->frames, frame);
    c->count++;
    return 1;  /* keep going */
}


int debug_stacktrace(cJSON *params, cJSON **out_result, cJSON **out_err) {
    const char *name = p_str(params, "name", out_err);
    if (!name) return 0;

    long long max_ll = p_int(params, "max_frames", 32);
    int max_frames = (int)max_ll;
    if (max_frames < 1) max_frames = 1;
    if (max_frames > 128) max_frames = 128;

    IExec->Forbid();
    struct Task *t = IExec->FindTask((STRPTR)name);
    if (!t) {
        IExec->Permit();
        cJSON *data = cJSON_CreateObject();
        if (data) cJSON_AddStringToObject(data, "name", name);
        *out_err = rpc_make_error(MCPD_ERR_TARGET, "Task not found", data);
        return 0;
    }

    cJSON *frames = cJSON_CreateArray();
    struct _stacktrace_ctx c = { frames, max_frames, 0 };
    struct Hook hk;
    memset(&hk, 0, sizeof(hk));
    hk.h_Entry = (uint32(*)())_stacktrace_hook;
    hk.h_Data  = &c;

    int32 ret = IDebug->StackTrace(t, &hk);
    IExec->Permit();

    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "name", name);
    cJSON_AddNumberToObject(r, "stacktrace_rc", (double)ret);
    cJSON_AddNumberToObject(r, "frame_count", (double)c.count);
    cJSON_AddItemToObject(r, "frames", frames);
    *out_result = r;
    return 0;
}


/* ---- debug.write_memory --------------------------------------- */

/* Write base64-decoded bytes to an arbitrary address. CAUTION: there
 * is no per-task memory protection on AOS4 classic; this can crash
 * the entire system. We require an explicit `confirm: true` field to
 * prevent accidental use. */
int debug_write_memory(cJSON *params, cJSON **out_result, cJSON **out_err) {
    cJSON *addr_node = cJSON_GetObjectItemCaseSensitive(params, "address");
    cJSON *b64_node  = cJSON_GetObjectItemCaseSensitive(params, "bytes_b64");
    cJSON *confirm   = cJSON_GetObjectItemCaseSensitive(params, "confirm");
    if (!cJSON_IsNumber(addr_node) || !cJSON_IsString(b64_node) ||
        !cJSON_IsTrue(confirm)) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "requires address (number), bytes_b64 (string), confirm: true",
            NULL);
        return 0;
    }
    uintptr_t addr = (uintptr_t)addr_node->valuedouble;
    size_t n = 0;
    unsigned char *data = b64_decode(b64_node->valuestring, &n);
    if (!data) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
                                  "bytes_b64 is not valid base64", NULL);
        return 0;
    }

    /* Refuse 0-byte writes (no point) and writes to NULL (PowerPC
     * NULL deref crashes the daemon). */
    if (n == 0) {
        b64_free(data);
        cJSON *r = cJSON_CreateObject();
        cJSON_AddNumberToObject(r, "address", (double)addr);
        cJSON_AddNumberToObject(r, "bytes_written", 0);
        *out_result = r;
        return 0;
    }
    if (addr == 0) {
        b64_free(data);
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "refusing to write to NULL", NULL);
        return 0;
    }

    IExec->Forbid();
    memcpy((void *)addr, data, n);
    IExec->CacheClearE((APTR)addr, n,
                       CACRF_ClearI | CACRF_ClearD);
    IExec->Permit();
    b64_free(data);

    cJSON *r = cJSON_CreateObject();
    cJSON_AddNumberToObject(r, "address", (double)addr);
    cJSON_AddNumberToObject(r, "bytes_written", (double)n);
    *out_result = r;
    return 0;
}


/* ---- debug.write_register ------------------------------------- */

/* Modify one register in a task's saved exception context. We name the
 * registers symbolically: pc/lr/ctr/xer/cr/msr/dar/dsisr or "gpr<n>"
 * for n in 0..31. */
int debug_write_register(cJSON *params, cJSON **out_result, cJSON **out_err) {
    const char *name = p_str(params, "name", out_err);
    if (!name) return 0;
    const char *reg  = p_str(params, "register", out_err);
    if (!reg)  return 0;
    cJSON *val_node  = cJSON_GetObjectItemCaseSensitive(params, "value");
    cJSON *confirm   = cJSON_GetObjectItemCaseSensitive(params, "confirm");
    if (!cJSON_IsNumber(val_node) || !cJSON_IsTrue(confirm)) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "requires value (number) and confirm: true", NULL);
        return 0;
    }
    uint32_t value = (uint32_t)val_node->valuedouble;

    IExec->Forbid();
    struct Task *t = IExec->FindTask((STRPTR)name);
    if (!t) {
        IExec->Permit();
        cJSON *data = cJSON_CreateObject();
        if (data) cJSON_AddStringToObject(data, "name", name);
        *out_err = rpc_make_error(MCPD_ERR_TARGET, "Task not found", data);
        return 0;
    }

    struct ExceptionContext ctx;
    memset(&ctx, 0, sizeof(ctx));
    /* Read full general-purpose state. */
    IDebug->ReadTaskContext(t, &ctx, RTCF_GENERAL | RTCF_STATE | RTCF_INFO);

    int matched = 1;
    if      (!strcmp(reg, "pc"))    ctx.ip    = value;
    else if (!strcmp(reg, "lr"))    ctx.lr    = value;
    else if (!strcmp(reg, "ctr"))   ctx.ctr   = value;
    else if (!strcmp(reg, "xer"))   ctx.xer   = value;
    else if (!strcmp(reg, "cr"))    ctx.cr    = value;
    else if (!strcmp(reg, "msr"))   ctx.msr   = value;
    else if (!strcmp(reg, "dar"))   ctx.dar   = value;
    else if (!strcmp(reg, "dsisr")) ctx.dsisr = value;
    else if (!strncmp(reg, "gpr", 3)) {
        char *end;
        long n = strtol(reg + 3, &end, 10);
        if (end == reg + 3 || *end != '\0' || n < 0 || n > 31) {
            matched = 0;
        } else {
            ctx.gpr[n] = value;
        }
    } else {
        matched = 0;
    }

    if (!matched) {
        IExec->Permit();
        cJSON *data = cJSON_CreateObject();
        if (data) cJSON_AddStringToObject(data, "register", reg);
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
                                  "unknown register name", data);
        return 0;
    }

    uint32_t wtcf = IDebug->WriteTaskContext(
        t, &ctx, RTCF_GENERAL | RTCF_STATE | RTCF_INFO);
    IExec->Permit();

    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "name", name);
    cJSON_AddStringToObject(r, "register", reg);
    cJSON_AddNumberToObject(r, "value", (double)value);
    cJSON_AddNumberToObject(r, "wtcf_filled", (double)wtcf);
    *out_result = r;
    return 0;
}
