/* crashhook.c - live system-fault capture via IExec->AddDebugHook.
 *
 * Background:
 *   The events.wait long-poll watches ExecBase->LastAlert for change,
 *   but a crash whose alert code matches an already-recorded prior
 *   alert is missed (only the address payload changes). Doomdark's
 *   Revenge ran into exactly that on the X5000: ACPU_AddressErr
 *   (0x80000003) was already in LastAlert from a previous test, so
 *   the poller saw "no change" when the game crashed with the same
 *   code at a new address.
 *
 *   IExec->AddDebugHook(NULL, &hook) registers a global debug hook
 *   that the kernel invokes on every task fault BEFORE the
 *   GrimReaper handler runs. The hook fires in a forbid-style
 *   context so we do the absolute minimum: snapshot a few register
 *   fields from the ExceptionContext that arrived in the hook
 *   message, set a flag, signal the main task, return. The main
 *   task's WaitSelect loop wakes on the signal and ships a
 *   `debug.exception` JSON-RPC notification with the captured
 *   data, riding the same idle-poll plumbing used by
 *   events_emit_pending().
 *
 * Hook message layout (per exec.doc/AddDebugHook):
 *   [ uint32 type ; APTR content ]
 *   For DBHMT_EXCEPTION the content is a struct ExceptionContext *
 *   covering the trapping task's saved registers. We only handle
 *   that type; everything else returns 0 (continue normal handling).
 *
 * Limits:
 *   - One pending crash record at a time. Two crashes in <200ms
 *     could lose the second; in practice users see the first
 *     GrimReaper before they trigger another.
 *   - The hook returns 0 unconditionally, so normal exception
 *     handling (GrimReaper) still runs after us.
 */

#include "rpc.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <proto/exec.h>
#include <exec/exec.h>
#include <exec/tasks.h>
#include <exec/interrupts.h>
#include <exec/exectags.h>
#include <utility/hooks.h>


extern int frame_write(int sock, const char *buf, size_t len);


/* ---- captured state, written by hook, read by drain --------- */

struct _crash_record {
    volatile int   armed;          /* 1 while a record awaits drain */
    volatile int   fire_count;     /* total times hook has been entered */
    volatile int   exception_count;/* times hook saw DBHMT_EXCEPTION */
    int            registered;     /* 1 if AddDebugHook returned TRUE */
    char           task_name[64];
    uint32_t       traptype;
    uint32_t       pc;             /* effective PC at fault */
    uint32_t       sp;             /* GPR1 at fault */
    uint32_t       lr;             /* link register */
    uint32_t       msr;
    uint32_t       cr;
    uint32_t       dar;            /* data-access fault address */
    uint32_t       dsisr;
    uint32_t       gpr[8];         /* first eight GPRs (0..7) */
};

static struct _crash_record _crash;
static struct Hook          _hook;
static int8_t               _signal_bit = -1;
static struct Task         *_main_task  = NULL;


/* ---- hook entry ---------------------------------------------
 *
 * AOS4's CallHookPkt convention: (struct Hook *h, APTR object,
 * APTR message). For an IExec debug hook, `object` is the trapping
 * task and `message` is `[uint32 type; APTR content]`. For
 * DBHMT_EXCEPTION, content is a struct ExceptionContext *.
 *
 * Forbid-style context. No malloc, no Wait, no library calls
 * except the tightly-scoped IExec->Signal at the end.
 */
static uint32 _crash_hook_entry(struct Hook *h, APTR object, APTR message) {
    (void)h;

    _crash.fire_count++;
    if (!message) return 0;
    uint32_t *m = (uint32_t *)message;
    uint32_t mtype = m[0];
    if (mtype != DBHMT_EXCEPTION) return 0;
    _crash.exception_count++;

    /* m[1] is APTR content (the ExceptionContext). On PPC32 ABI
     * pointers are 4 bytes naturally aligned. */
    struct ExceptionContext *ctx = (struct ExceptionContext *)m[1];
    if (!ctx) return 0;

    struct Task *task = (struct Task *)object;
    const char *src = (task && task->tc_Node.ln_Name) ?
                      task->tc_Node.ln_Name : "<unnamed>";
    size_t i = 0;
    while (i < sizeof(_crash.task_name) - 1 && src[i]) {
        _crash.task_name[i] = src[i];
        i++;
    }
    _crash.task_name[i] = '\0';

    _crash.traptype = (uint32_t)ctx->Traptype;
    _crash.pc       = (uint32_t)ctx->ip;
    _crash.sp       = (uint32_t)ctx->gpr[1];
    _crash.lr       = (uint32_t)ctx->lr;
    _crash.msr      = (uint32_t)ctx->msr;
    _crash.cr       = (uint32_t)ctx->cr;
    _crash.dar      = (uint32_t)ctx->dar;
    _crash.dsisr    = (uint32_t)ctx->dsisr;
    for (int g = 0; g < 8; g++) {
        _crash.gpr[g] = (uint32_t)ctx->gpr[g];
    }
    _crash.armed = 1;

    if (_main_task && _signal_bit >= 0) {
        IExec->Signal(_main_task, 1u << (uint32)_signal_bit);
    }
    /* Return 0 = continue normal trap handling (GrimReaper still
     * runs after us). */
    return 0;
}


/* ---- public entry points ----------------------------------- */

int crashhook_init(void) {
    _main_task = IExec->FindTask(NULL);

    int8_t sigbit = (int8_t)IExec->AllocSignal(-1);
    if (sigbit < 0) return -1;
    _signal_bit = sigbit;

    memset(&_hook, 0, sizeof(_hook));
    _hook.h_Entry = (uint32(*)())_crash_hook_entry;
    _hook.h_Data  = NULL;

    /* AddDebugHook(task, hook): register on the given task. Pass our
     * own task so child tasks inherit the hook at creation time per
     * the autodoc ("Tasks created by a task with a debugger hook set
     * will automatically inherit the hook"). NULL is also accepted by
     * the kernel but doesn't seem to register the hook globally on
     * AOS 4.1 FE - children don't inherit. */
    if (!IDebug->AddDebugHook(_main_task, &_hook)) {
        IExec->FreeSignal((LONG)_signal_bit);
        _signal_bit = -1;
        return -1;
    }
    _crash.registered = 1;
    return 0;
}

int crashhook_fire_count(void) { return _crash.fire_count; }
int crashhook_exception_count(void) { return _crash.exception_count; }
int crashhook_registered(void) { return _crash.registered; }


void crashhook_shutdown(void) {
    /* Best-effort de-register: pass NULL hook to clear. */
    IDebug->AddDebugHook(NULL, NULL);
    if (_signal_bit >= 0) {
        IExec->FreeSignal((LONG)_signal_bit);
        _signal_bit = -1;
    }
}


uint32_t crashhook_signal_mask(void) {
    if (_signal_bit < 0) return 0;
    return 1u << (uint32_t)_signal_bit;
}


/* Public extern - sys.c provides this. */
extern void _add_decoded_alert(cJSON *parent, uint32_t code);


int crashhook_drain(int sock) {
    if (!_crash.armed) return 0;
    _crash.armed = 0;

    cJSON *env = cJSON_CreateObject();
    if (!env) return 0;
    cJSON_AddStringToObject(env, "jsonrpc", "2.0");
    cJSON_AddStringToObject(env, "method", "events.notify");
    cJSON *params = cJSON_AddObjectToObject(env, "params");
    cJSON_AddStringToObject(params, "topic", "debug.exception");
    cJSON *data = cJSON_AddObjectToObject(params, "data");

    cJSON_AddStringToObject(data, "task", _crash.task_name);
    cJSON_AddNumberToObject(data, "traptype", (double)_crash.traptype);
    cJSON_AddNumberToObject(data, "pc",       (double)_crash.pc);
    cJSON_AddNumberToObject(data, "sp",       (double)_crash.sp);
    cJSON_AddNumberToObject(data, "lr",       (double)_crash.lr);
    cJSON_AddNumberToObject(data, "msr",      (double)_crash.msr);
    cJSON_AddNumberToObject(data, "cr",       (double)_crash.cr);
    cJSON_AddNumberToObject(data, "dar",      (double)_crash.dar);
    cJSON_AddNumberToObject(data, "dsisr",    (double)_crash.dsisr);
    cJSON *gpr = cJSON_AddArrayToObject(data, "gpr");
    for (int g = 0; g < 8; g++) {
        cJSON_AddItemToArray(gpr,
            cJSON_CreateNumber((double)_crash.gpr[g]));
    }

    /* Synthetic alert code for downstream decoders. PowerPC traps
     * map roughly to the AOS 0x80000000 + traptype convention. */
    uint32_t synthetic = 0x80000000u | (_crash.traptype & 0xFFFFu);
    cJSON_AddNumberToObject(data, "alert_code", (double)synthetic);
    _add_decoded_alert(data, synthetic);

    char *json = cJSON_PrintUnformatted(env);
    cJSON_Delete(env);
    if (!json) return 0;
    frame_write(sock, json, strlen(json));
    free(json);
    return 1;
}
