/* sys.* method handlers. */

#include "../b64.h"
#include "../rpc.h"
#include "amigautil.h"
#include "methods.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <proto/exec.h>
#include <proto/dos.h>
#include <exec/lists.h>
#include <exec/nodes.h>
#include <exec/tasks.h>
#include <exec/libraries.h>
#include <exec/ports.h>
#include <exec/execbase.h>
#include <exec/memory.h>
#include <dos/dos.h>
#include <dos/dosextens.h>
#include <dos/dosasl.h>

static const char *find_token(const char *hay, const char *needle) {
    return strstr(hay, needle);
}

/* Extract "X.YZ" after a token (e.g. "Kickstart "). Returns malloc'd
 * NUL-terminated string or NULL if not found. */
static char *parse_after(const char *hay, const char *token) {
    const char *p = find_token(hay, token);
    if (!p) return NULL;
    p += strlen(token);
    while (*p == ' ' || *p == '\t') p++;
    const char *q = p;
    while (*q && (*q == '.' || (*q >= '0' && *q <= '9'))) q++;
    size_t n = (size_t)(q - p);
    if (n == 0) return NULL;
    char *r = (char *)malloc(n + 1);
    if (!r) return NULL;
    memcpy(r, p, n);
    r[n] = '\0';
    return r;
}

int sys_version(cJSON *params, cJSON **out_result, cJSON **out_err) {
    (void)params; (void)out_err;

    int rc = 0;
    char *out = amiga_run_command("Version FULL", &rc, 5.0);
    if (!out) {
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
                                  "Version FULL: capture failed", NULL);
        return 0;
    }

    char *kick = parse_after(out, "Kickstart ");
    char *wb   = parse_after(out, "Workbench ");

    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "raw", out);
    if (kick) cJSON_AddStringToObject(r, "kickstart", kick);
    else      cJSON_AddNullToObject(r, "kickstart");
    if (wb)   cJSON_AddStringToObject(r, "workbench", wb);
    else      cJSON_AddNullToObject(r, "workbench");

    free(kick);
    free(wb);
    free(out);
    *out_result = r;
    return 0;
}

/* ---- helpers ---------------------------------------------------- */

/* Sanitize an Amiga node name to ASCII. AOS task / port / library
 * names are nominally ISO-8859-1 but in practice can contain raw
 * binary - kernel-internal tasks especially. cJSON's
 * cJSON_AddStringToObject builds UTF-8 JSON, which then fails on the
 * Python side when high-bit bytes appear. Replace anything outside
 * the printable ASCII range with '?'.
 *
 * Writes into `dst` (max `dst_cap` incl NUL). Returns dst.
 */
static char *sanitize(const char *src, char *dst, size_t dst_cap) {
    if (!src) src = "";
    size_t n = 0;
    while (src[n] && n < dst_cap - 1) {
        unsigned char c = (unsigned char)src[n];
        if (c >= 0x20 && c < 0x7f) {
            dst[n] = (char)c;
        } else {
            dst[n] = '?';
        }
        n++;
    }
    dst[n] = '\0';
    return dst;
}

/* Walk an exec list under Forbid()/Permit(), calling `add_node` for
 * each. Returns a cJSON array. */
typedef void (*node_emit_fn)(struct Node *n, cJSON *arr);

static cJSON *walk_list(struct List *l, node_emit_fn emit) {
    cJSON *arr = cJSON_CreateArray();
    IExec->Forbid();
    for (struct Node *n = l->lh_Head; n->ln_Succ; n = n->ln_Succ) {
        emit(n, arr);
    }
    IExec->Permit();
    return arr;
}

static const char *task_state(BYTE st) {
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

static const char *node_type_name(BYTE t) {
    switch (t) {
        case NT_TASK:      return "task";
        case NT_PROCESS:   return "process";
        case NT_LIBRARY:   return "library";
        case NT_DEVICE:    return "device";
        case NT_RESOURCE:  return "resource";
        case NT_MSGPORT:   return "port";
        case NT_INTERRUPT: return "interrupt";
        default:           return "other";
    }
}

/* ---- sys.tasks ------------------------------------------------- */

static void emit_task(struct Node *n, cJSON *arr) {
    struct Task *t = (struct Task *)n;
    char buf[128];
    cJSON *o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "name",
        sanitize(t->tc_Node.ln_Name, buf, sizeof(buf)));
    cJSON_AddStringToObject(o, "type", node_type_name(t->tc_Node.ln_Type));
    cJSON_AddNumberToObject(o, "priority", (double)t->tc_Node.ln_Pri);
    cJSON_AddStringToObject(o, "state", task_state(t->tc_State));
    cJSON_AddItemToArray(arr, o);
}

int sys_tasks(cJSON *params, cJSON **out_result, cJSON **out_err) {
    (void)params; (void)out_err;
    struct ExecBase *eb = (struct ExecBase *)SysBase;
    cJSON *r = cJSON_CreateObject();
    cJSON *ready = walk_list(&eb->TaskReady, emit_task);
    cJSON *wait  = walk_list(&eb->TaskWait,  emit_task);
    cJSON_AddItemToObject(r, "ready", ready);
    cJSON_AddItemToObject(r, "waiting", wait);
    *out_result = r;
    return 0;
}

/* ---- sys.libraries -------------------------------------------- */

static void emit_lib(struct Node *n, cJSON *arr) {
    struct Library *l = (struct Library *)n;
    char buf[128];
    cJSON *o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "name",
        sanitize(l->lib_Node.ln_Name, buf, sizeof(buf)));
    cJSON_AddNumberToObject(o, "version", (double)l->lib_Version);
    cJSON_AddNumberToObject(o, "revision", (double)l->lib_Revision);
    cJSON_AddNumberToObject(o, "open_count", (double)l->lib_OpenCnt);
    cJSON_AddItemToArray(arr, o);
}

int sys_libraries(cJSON *params, cJSON **out_result, cJSON **out_err) {
    (void)params; (void)out_err;
    struct ExecBase *eb = (struct ExecBase *)SysBase;
    *out_result = walk_list(&eb->LibList, emit_lib);
    return 0;
}

/* ---- sys.devices ---------------------------------------------- */

int sys_devices(cJSON *params, cJSON **out_result, cJSON **out_err) {
    (void)params; (void)out_err;
    struct ExecBase *eb = (struct ExecBase *)SysBase;
    /* devices share lib_Library structure, can reuse emit_lib. */
    *out_result = walk_list(&eb->DeviceList, emit_lib);
    return 0;
}

/* ---- sys.modules ---------------------------------------------- *
 *
 * Walk LibList + DeviceList + ResourceList and emit each entry with
 * its load base, code size, and version. Useful for mapping a runtime
 * crash address back to a module: a fault PC inside [base, base+size]
 * tells you which library/device the code came from, even when the
 * faulting binary itself has been stripped.
 *
 * AOS4 layout for a struct Library: jump-table vectors live at
 * `((char *)lib) - lib_NegSize`, the positive structure starts at
 * `lib`, total span = NegSize + PosSize. Devices and resources share
 * the lib_Library prefix so the same field reads work.
 */
static void emit_module(struct Node *n, cJSON *arr, const char *kind) {
    struct Library *l = (struct Library *)n;
    char buf[128];
    cJSON *o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "name",
        sanitize(l->lib_Node.ln_Name, buf, sizeof(buf)));
    cJSON_AddStringToObject(o, "kind", kind);
    /* Code+vector base = lib - NegSize. */
    uintptr_t code_base = (uintptr_t)l - (uintptr_t)l->lib_NegSize;
    uintptr_t total_size = (uintptr_t)l->lib_NegSize + (uintptr_t)l->lib_PosSize;
    cJSON_AddNumberToObject(o, "base", (double)code_base);
    cJSON_AddNumberToObject(o, "lib_base", (double)(uintptr_t)l);
    cJSON_AddNumberToObject(o, "neg_size", (double)l->lib_NegSize);
    cJSON_AddNumberToObject(o, "pos_size", (double)l->lib_PosSize);
    cJSON_AddNumberToObject(o, "size", (double)total_size);
    cJSON_AddNumberToObject(o, "version", (double)l->lib_Version);
    cJSON_AddNumberToObject(o, "revision", (double)l->lib_Revision);
    cJSON_AddNumberToObject(o, "open_count", (double)l->lib_OpenCnt);
    cJSON_AddItemToArray(arr, o);
}

static void emit_module_lib(struct Node *n, cJSON *arr) {
    emit_module(n, arr, "library");
}
static void emit_module_dev(struct Node *n, cJSON *arr) {
    emit_module(n, arr, "device");
}
static void emit_module_res(struct Node *n, cJSON *arr) {
    emit_module(n, arr, "resource");
}

/* ---- sys.crashhook_status ------------------------------------- *
 *
 * Diagnostic readout of the live IDebug crash hook: whether
 * registration succeeded and how many times the hook entry has
 * fired since boot. Used to verify wiring without having to crash
 * something.
 */
extern int crashhook_fire_count(void);
extern int crashhook_exception_count(void);
extern int crashhook_registered(void);

int sys_crashhook_status(cJSON *params, cJSON **out_result, cJSON **out_err) {
    (void)params; (void)out_err;
    cJSON *r = cJSON_CreateObject();
    cJSON_AddBoolToObject(r, "registered", crashhook_registered() ? cJSON_True : cJSON_False);
    cJSON_AddNumberToObject(r, "fire_count", (double)crashhook_fire_count());
    cJSON_AddNumberToObject(r, "exception_count", (double)crashhook_exception_count());
    *out_result = r;
    return 0;
}

int sys_modules(cJSON *params, cJSON **out_result, cJSON **out_err) {
    (void)params; (void)out_err;
    struct ExecBase *eb = (struct ExecBase *)SysBase;
    cJSON *arr = cJSON_CreateArray();

    IExec->Forbid();
    for (struct Node *n = eb->LibList.lh_Head; n->ln_Succ; n = n->ln_Succ) {
        emit_module_lib(n, arr);
    }
    for (struct Node *n = eb->DeviceList.lh_Head; n->ln_Succ; n = n->ln_Succ) {
        emit_module_dev(n, arr);
    }
    for (struct Node *n = eb->ResourceList.lh_Head; n->ln_Succ; n = n->ln_Succ) {
        emit_module_res(n, arr);
    }
    IExec->Permit();

    cJSON *r = cJSON_CreateObject();
    cJSON_AddItemToObject(r, "modules", arr);
    *out_result = r;
    return 0;
}

/* ---- sys.ports ------------------------------------------------ */

static void emit_port(struct Node *n, cJSON *arr) {
    struct MsgPort *p = (struct MsgPort *)n;
    char buf[128];
    cJSON *o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "name",
        sanitize(p->mp_Node.ln_Name, buf, sizeof(buf)));
    cJSON_AddNumberToObject(o, "priority", (double)p->mp_Node.ln_Pri);
    cJSON_AddNumberToObject(o, "flags", (double)p->mp_Flags);
    cJSON_AddItemToArray(arr, o);
}

int sys_ports(cJSON *params, cJSON **out_result, cJSON **out_err) {
    (void)params; (void)out_err;
    struct ExecBase *eb = (struct ExecBase *)SysBase;
    *out_result = walk_list(&eb->PortList, emit_port);
    return 0;
}

/* ---- sys.lastalert -------------------------------------------- */

/* Decode an alert code per the format documented in exec/alerts.h:
 *
 *   bit 31: DeadEnd flag (AT_DeadEnd)
 *   bits 30..24: subsystem ID (1=Exec, 2=Graphics, 4=Intuition, ...)
 *   bits 23..16: general error nibble (AG_NoMemory=1, AG_OpenLib=3, ...)
 *   bits 15..0:  subsystem-specific error / object reference
 *
 * We expose the decomposed parts plus a friendly subsystem name. */
static const char *_alert_subsystem_name(unsigned id) {
    switch (id) {
    case 0x01: return "exec.library";
    case 0x02: return "graphics.library";
    case 0x03: return "layers.library";
    case 0x04: return "intuition.library";
    case 0x05: return "math.library";
    case 0x07: return "dos.library";
    case 0x08: return "ramlib";
    case 0x09: return "icon.library";
    case 0x0A: return "expansion.library";
    case 0x0B: return "diskfont.library";
    case 0x0E: return "newlib.library";
    case 0x10: return "audio.device";
    case 0x11: return "console.device";
    case 0x12: return "gameport.device";
    case 0x13: return "keyboard.device";
    case 0x14: return "trackdisk.device";
    case 0x15: return "timer.device";
    case 0x16: return "cybppc.device";
    case 0x20: return "cia.resource";
    case 0x21: return "disk.resource";
    case 0x22: return "misc.resource";
    case 0x30: return "bootstrap";
    case 0x31: return "Workbench";
    case 0x32: return "DiskCopy";
    case 0x33: return "GadTools";
    case 0x34: return "utility.library";
    case 0x35: return "unknown";
    default:   return "unknown";
    }
}

static const char *_alert_general_name(unsigned ge) {
    switch (ge) {
    case 0x01: return "NoMemory";
    case 0x02: return "MakeLib";
    case 0x03: return "OpenLib";
    case 0x04: return "OpenDev";
    case 0x05: return "OpenRes";
    case 0x06: return "IOError";
    case 0x07: return "NoSignal";
    case 0x08: return "BadParm";
    case 0x09: return "CloseLib";
    case 0x0A: return "CloseDev";
    case 0x0B: return "ProcCreate";
    case 0x0C: return "Obsolete";
    default:   return NULL;
    }
}

/* Pick a known specific-alert name for a few of the most common
 * codes - covers the cases users usually hit. Returns NULL if no
 * recognised constant. */
static const char *_alert_specific_name(uint32_t code) {
    switch (code) {
    case 0x81000005UL: return "AN_MemCorrupt";
    case 0x81000006UL: return "AN_IntrMem";
    case 0x01000007UL: return "AN_InitAPtr";
    case 0x01000008UL: return "AN_SemCorrupt";
    case 0x01000009UL: return "AN_FreeTwice";
    case 0x0100000BUL: return "AN_IOUsedTwice";
    case 0x0100000CUL: return "AN_MemoryInsane";
    case 0x0100000DUL: return "AN_IOAfterClose";
    case 0x0100000EUL: return "AN_StackProbe";
    case 0x0100000FUL: return "AN_BadFreeAddr";
    case 0x01000010UL: return "AN_BadSemaphore";
    case 0x01000011UL: return "AN_BadMemory";
    case 0x01000012UL: return "AN_BadHook";
    case 0x07010001UL: return "AN_StartMem";
    case 0x07000002UL: return "AN_EndTask";
    case 0x07000003UL: return "AN_QPktFail";
    case 0x07000005UL: return "AN_FreeVec";
    case 0x07000007UL: return "AN_BitMap";
    case 0x0700000FUL: return "AN_NoBootNode";
    case 0x80000002UL: return "ACPU_BusErr";
    case 0x80000003UL: return "ACPU_AddressErr";
    case 0x80000004UL: return "ACPU_InstErr";
    case 0x80000005UL: return "ACPU_DivZero";
    case 0x80000008UL: return "ACPU_PrivErr";
    default:           return NULL;
    }
}

void _add_decoded_alert(cJSON *parent, uint32_t code) {
    cJSON *d = cJSON_AddObjectToObject(parent, "decoded");
    cJSON_AddNumberToObject(d, "code", (double)code);
    cJSON_AddBoolToObject(d, "dead_end", (code & 0x80000000UL) != 0);
    unsigned subsys  = (code >> 24) & 0x7F;
    unsigned general = (code >> 16) & 0xFF;
    unsigned specific = code & 0xFFFF;
    cJSON_AddNumberToObject(d, "subsystem_id", (double)subsys);
    cJSON_AddStringToObject(d, "subsystem", _alert_subsystem_name(subsys));
    cJSON_AddNumberToObject(d, "general_id", (double)general);
    const char *gname = _alert_general_name(general);
    if (gname) cJSON_AddStringToObject(d, "general", gname);
    cJSON_AddNumberToObject(d, "specific", (double)specific);
    const char *sname = _alert_specific_name(code);
    if (sname) cJSON_AddStringToObject(d, "name", sname);
}


int sys_lastalert(cJSON *params, cJSON **out_result, cJSON **out_err) {
    (void)params; (void)out_err;
    struct ExecBase *eb = (struct ExecBase *)SysBase;
    cJSON *r = cJSON_CreateObject();
    /* LastAlert is a 4-element array. The SDK declares them as LONG
     * (signed), but the values are bit-patterns (alert codes); cast
     * via uint32_t so high bits don't sign-extend through double.
     */
    cJSON *arr = cJSON_AddArrayToObject(r, "values");
    for (int i = 0; i < 4; i++) {
        unsigned int v = (unsigned int)(uint32_t)eb->LastAlert[i];
        cJSON_AddItemToArray(arr, cJSON_CreateNumber((double)v));
    }
    uint32_t code = (uint32_t)eb->LastAlert[0];
    cJSON_AddNumberToObject(r, "alert_code", (double)code);
    /* AOS4 leaves LastAlert[0] = 0xFFFFFFFF when no alert has been
     * raised since boot. Don't pretend we decoded that. */
    if (code == 0xFFFFFFFFUL) {
        cJSON_AddBoolToObject(r, "no_alert_recorded", 1);
    } else {
        _add_decoded_alert(r, code);
    }
    *out_result = r;
    return 0;
}


int sys_alert_decode(cJSON *params, cJSON **out_result, cJSON **out_err) {
    /* Decode an arbitrary alert code (not just the most recent). */
    cJSON *code_node = cJSON_GetObjectItemCaseSensitive(params, "code");
    if (!cJSON_IsNumber(code_node)) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
                                  "missing required number param: code",
                                  NULL);
        return 0;
    }
    uint32_t code = (uint32_t)code_node->valuedouble;
    cJSON *r = cJSON_CreateObject();
    cJSON_AddNumberToObject(r, "code", (double)code);
    _add_decoded_alert(r, code);
    *out_result = r;
    return 0;
}

/* ---- sys.uptime ----------------------------------------------- */

#include <proto/timer.h>
#include <devices/timer.h>

int sys_uptime(cJSON *params, cJSON **out_result, cJSON **out_err) {
    (void)params;

    /* Open timer.device + ITimer for the duration of this call. */
    struct MsgPort *port = IExec->AllocSysObjectTags(ASOT_PORT, TAG_END);
    if (!port) {
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL, "AllocSysObject port", NULL);
        return 0;
    }
    struct TimeRequest *tr = IExec->AllocSysObjectTags(
        ASOT_IOREQUEST,
        ASOIOR_ReplyPort, (Tag)port,
        ASOIOR_Size,      sizeof(struct TimeRequest),
        TAG_END);
    if (!tr) {
        IExec->FreeSysObject(ASOT_PORT, port);
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL, "AllocSysObject ioreq", NULL);
        return 0;
    }
    if (IExec->OpenDevice("timer.device", UNIT_VBLANK,
                          (struct IORequest *)tr, 0) != 0) {
        IExec->FreeSysObject(ASOT_IOREQUEST, tr);
        IExec->FreeSysObject(ASOT_PORT, port);
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
            "OpenDevice timer.device VBLANK failed", NULL);
        return 0;
    }
    struct TimerIFace *ITimer = (struct TimerIFace *)IExec->GetInterface(
        (struct Library *)tr->Request.io_Device, "main", 1, NULL);
    if (!ITimer) {
        IExec->CloseDevice((struct IORequest *)tr);
        IExec->FreeSysObject(ASOT_IOREQUEST, tr);
        IExec->FreeSysObject(ASOT_PORT, port);
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL, "GetInterface ITimer", NULL);
        return 0;
    }

    struct EClockVal ev;
    uint32 freq = ITimer->ReadEClock(&ev);
    uint64_t ticks = ((uint64_t)ev.ev_hi << 32) | ev.ev_lo;
    double seconds = freq > 0 ? (double)ticks / (double)freq : 0.0;

    IExec->DropInterface((struct Interface *)ITimer);
    IExec->CloseDevice((struct IORequest *)tr);
    IExec->FreeSysObject(ASOT_IOREQUEST, tr);
    IExec->FreeSysObject(ASOT_PORT, port);

    cJSON *r = cJSON_CreateObject();
    cJSON_AddNumberToObject(r, "seconds", seconds);
    cJSON_AddNumberToObject(r, "eclock_freq", (double)freq);
    *out_result = r;
    return 0;
}

/* ---- sys.memory ----------------------------------------------- */

int sys_memory(cJSON *params, cJSON **out_result, cJSON **out_err) {
    (void)params; (void)out_err;

    /* AOS4 doesn't really have CHIP/FAST split anymore (it's all
     * MEMF_ANY). We report MEMF_ANY free + largest, plus the
     * MEMF_SHARED + MEMF_VIRTUAL cuts because those affect what
     * libraries / tasks can use.
     */
    uint64_t any_free   = (uint64_t)IExec->AvailMem(MEMF_ANY);
    uint64_t any_largest= (uint64_t)IExec->AvailMem(MEMF_ANY | MEMF_LARGEST);
    uint64_t any_total  = (uint64_t)IExec->AvailMem(MEMF_ANY | MEMF_TOTAL);
    uint64_t shared_free= (uint64_t)IExec->AvailMem(MEMF_SHARED);
    uint64_t shared_tot = (uint64_t)IExec->AvailMem(MEMF_SHARED | MEMF_TOTAL);
    uint64_t virt_free  = (uint64_t)IExec->AvailMem(MEMF_VIRTUAL);
    uint64_t virt_tot   = (uint64_t)IExec->AvailMem(MEMF_VIRTUAL | MEMF_TOTAL);

    cJSON *r = cJSON_CreateObject();
    cJSON *any = cJSON_AddObjectToObject(r, "any");
    cJSON_AddNumberToObject(any, "free",    (double)any_free);
    cJSON_AddNumberToObject(any, "largest", (double)any_largest);
    cJSON_AddNumberToObject(any, "total",   (double)any_total);
    cJSON *sh = cJSON_AddObjectToObject(r, "shared");
    cJSON_AddNumberToObject(sh, "free",  (double)shared_free);
    cJSON_AddNumberToObject(sh, "total", (double)shared_tot);
    cJSON *vi = cJSON_AddObjectToObject(r, "virtual");
    cJSON_AddNumberToObject(vi, "free",  (double)virt_free);
    cJSON_AddNumberToObject(vi, "total", (double)virt_tot);

    *out_result = r;
    return 0;
}

/* ---- sys.volumes / sys.assigns -------------------------------- */

/* Walk DOS list under LockDosList. Captures volumes (LDF_VOLUMES)
 * or assigns (LDF_ASSIGNS) according to `which`. */
typedef void (*dl_emit_fn)(struct DosList *, cJSON *);

static void _emit_volume(struct DosList *dl, cJSON *arr) {
    /* dol_Name is a BSTR (BCPL string). */
    char name[128];
    name[0] = '\0';
    {
        const char *bs = (const char *)BADDR(dl->dol_Name);
        if (bs) {
            unsigned int len = (unsigned char)bs[0];
            if (len > sizeof(name) - 1) len = sizeof(name) - 1;
            for (unsigned int i = 0; i < len; i++) {
                unsigned char c = (unsigned char)bs[i + 1];
                name[i] = (c >= 0x20 && c < 0x7f) ? (char)c : '?';
            }
            name[len] = '\0';
        }
    }

    cJSON *o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "name", name);
    cJSON_AddNumberToObject(o, "type",
        (double)(unsigned int)dl->dol_Type);
    cJSON_AddNumberToObject(o, "port",
        (double)(uintptr_t)dl->dol_Port);
    cJSON_AddItemToArray(arr, o);
}

static void _emit_assign(struct DosList *dl, cJSON *arr) {
    /* dol_Name is a BSTR. dol_misc.dol_assign.dol_AssignName for
     * single-target assigns; for multidirs we report just one entry.
     */
    char name[128];
    name[0] = '\0';
    const char *bs = (const char *)BADDR(dl->dol_Name);
    if (bs) {
        unsigned int len = (unsigned char)bs[0];
        if (len > sizeof(name) - 1) len = sizeof(name) - 1;
        for (unsigned int i = 0; i < len; i++) {
            unsigned char c = (unsigned char)bs[i + 1];
            name[i] = (c >= 0x20 && c < 0x7f) ? (char)c : '?';
        }
        name[len] = '\0';
    }

    cJSON *o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "name", name);
    cJSON_AddNumberToObject(o, "type",
        (double)(unsigned int)dl->dol_Type);
    cJSON_AddItemToArray(arr, o);
}

static cJSON *_walk_doslist(uint32_t which, dl_emit_fn emit) {
    cJSON *arr = cJSON_CreateArray();
    struct DosList *dl = IDOS->LockDosList(LDF_READ | which);
    if (!dl) return arr;
    while ((dl = IDOS->NextDosEntry(dl, which)) != NULL) {
        emit(dl, arr);
    }
    IDOS->UnLockDosList(LDF_READ | which);
    return arr;
}

int sys_volumes(cJSON *params, cJSON **out_result, cJSON **out_err) {
    (void)params; (void)out_err;
    *out_result = _walk_doslist(LDF_VOLUMES, _emit_volume);
    return 0;
}

int sys_assigns(cJSON *params, cJSON **out_result, cJSON **out_err) {
    (void)params; (void)out_err;
    *out_result = _walk_doslist(LDF_ASSIGNS, _emit_assign);
    return 0;
}


/* ---- sys.hardware ---------------------------------------------- */

#include <exec/exectags.h>

static const char *_cpu_family(uint32_t f) {
    switch (f) {
    case 1: return "60X";
    case 2: return "7X0";
    case 3: return "74XX";
    case 4: return "4XX";
    case 5: return "PA6T";
    case 6: return "E300";
    case 7: return "E5500";
    case 8: return "E500";
    default: return "unknown";
    }
}

static const char *_cpu_model(uint32_t m) {
    switch (m) {
    case  1: return "PPC603E";
    case  2: return "PPC604E";
    case  3: return "PPC750CXE";
    case  4: return "PPC750FX";
    case  5: return "PPC750GX";
    case  6: return "PPC7410";
    case  7: return "PPC74XX_VGER";
    case  8: return "PPC74XX_APOLLO";
    case  9: return "PPC405LP";
    case 10: return "PPC405EP";
    case 11: return "PPC405GP";
    case 12: return "PPC405GPR";
    case 13: return "PPC440EP";
    case 14: return "PPC440GP";
    case 15: return "PPC440GX";
    case 16: return "PPC440SX";
    case 17: return "PPC440SP";
    case 18: return "PA6T_1682M";
    case 19: return "PPC460EX";
    case 20: return "PPC5121E";
    case 21: return "P50XX";
    case 22: return "P10XX";
    default: return "unknown";
    }
}


/* Wrapper. GCIT_FrontsideSpeed/ProcessorSpeed/TimeBaseSpeed write a
 * uint64; GCIT_ModelString writes a STRPTR (pointer to const string).
 * Caller passes typed buffers; we hide the Tag-array setup. */
static void _read_cpu_info(uint32 *ncpus, uint32 *family, uint32 *model,
                           const char **model_str,
                           uint64 *fsb_hz, uint64 *cpu_hz, uint64 *tb_hz,
                           uint32 *l1, uint32 *l2, uint32 *l3,
                           uint32 *vec, uint32 *page, uint32 *cline) {
    *ncpus = 0; *family = 0; *model = 0; *model_str = NULL;
    *fsb_hz = 0; *cpu_hz = 0; *tb_hz = 0;
    *l1 = 0; *l2 = 0; *l3 = 0; *vec = 0; *page = 0; *cline = 0;
    IExec->GetCPUInfoTags(
        GCIT_NumberOfCPUs,    (Tag)ncpus,
        GCIT_Family,          (Tag)family,
        GCIT_Model,           (Tag)model,
        GCIT_ModelString,     (Tag)model_str,
        GCIT_FrontsideSpeed,  (Tag)fsb_hz,
        GCIT_ProcessorSpeed,  (Tag)cpu_hz,
        GCIT_TimeBaseSpeed,   (Tag)tb_hz,
        GCIT_L1CacheSize,     (Tag)l1,
        GCIT_L2CacheSize,     (Tag)l2,
        GCIT_L3CacheSize,     (Tag)l3,
        GCIT_VectorUnit,      (Tag)vec,
        GCIT_CPUPageSize,     (Tag)page,
        GCIT_CacheLineSize,   (Tag)cline,
        TAG_END);
}


int sys_hardware(cJSON *params, cJSON **out_result, cJSON **out_err) {
    (void)params; (void)out_err;
    cJSON *r = cJSON_CreateObject();

    /* AttnFlags from ExecBase. NB on AOS4 only AFF_OTHER + AFF_PRIVATE
     * are typically set for PowerPC; the AFF_603/604/750/7400/4XX bits
     * weren't carried forward. GetCPUInfo is the canonical source. */
    struct ExecBase *eb = (struct ExecBase *)SysBase;
    uint32_t af = eb->AttnFlags;
    cJSON_AddNumberToObject(r, "attn_flags", (double)af);
    cJSON *afj = cJSON_AddArrayToObject(r, "attn_flags_decoded");
    static const struct { unsigned bit; const char *name; } attn_table[] = {
        { 0,  "AFF_68010"   }, { 1,  "AFF_68020"   },
        { 2,  "AFF_68030"   }, { 3,  "AFF_68040"   },
        { 4,  "AFF_68881"   }, { 5,  "AFF_68882"   },
        { 6,  "AFF_FPU40"   }, { 7,  "AFF_68060"   },
        { 8,  "AFF_603"     }, { 9,  "AFF_604"     },
        { 10, "AFF_750"     }, { 11, "AFF_7400"    },
        { 12, "AFF_ALTIVEC" }, { 13, "AFF_4XX"     },
        { 14, "AFF_OTHER"   }, { 15, "AFF_PRIVATE" },
    };
    for (size_t i = 0; i < sizeof(attn_table)/sizeof(attn_table[0]); i++) {
        if (af & (1u << attn_table[i].bit)) {
            cJSON_AddItemToArray(afj, cJSON_CreateString(attn_table[i].name));
        }
    }

    /* CPU details via GetCPUInfo. */
    cJSON *cpu = cJSON_AddObjectToObject(r, "cpu");
    uint32 ncpus, family, model, l1, l2, l3, vec, page, cline;
    uint64 fsb_hz, cpu_hz, tb_hz;
    const char *model_str = NULL;
    _read_cpu_info(&ncpus, &family, &model, &model_str,
                   &fsb_hz, &cpu_hz, &tb_hz,
                   &l1, &l2, &l3, &vec, &page, &cline);

    cJSON_AddNumberToObject(cpu, "count", (double)ncpus);
    cJSON_AddNumberToObject(cpu, "family_id", (double)family);
    cJSON_AddStringToObject(cpu, "family", _cpu_family(family));
    cJSON_AddNumberToObject(cpu, "model_id", (double)model);
    cJSON_AddStringToObject(cpu, "model", _cpu_model(model));
    if (model_str) cJSON_AddStringToObject(cpu, "model_string", model_str);
    cJSON_AddNumberToObject(cpu, "speed_hz", (double)cpu_hz);
    cJSON_AddNumberToObject(cpu, "fsb_hz", (double)fsb_hz);
    cJSON_AddNumberToObject(cpu, "timebase_hz", (double)tb_hz);
    cJSON_AddNumberToObject(cpu, "l1_cache", (double)l1);
    cJSON_AddNumberToObject(cpu, "l2_cache", (double)l2);
    cJSON_AddNumberToObject(cpu, "l3_cache", (double)l3);
    cJSON_AddNumberToObject(cpu, "vector_unit", (double)vec);
    cJSON_AddNumberToObject(cpu, "page_size", (double)page);
    cJSON_AddNumberToObject(cpu, "cache_line", (double)cline);

    /* Resource probes - presence flags. xena.resource ships on
     * BOTH X5000 and A1222 (Cyrus Plus has Xena+Xorro slots), so it
     * isn't a discriminator on its own; combined with CPU family it
     * still shapes the board signature. */
    cJSON *res = cJSON_AddObjectToObject(r, "resources");
    cJSON_AddBoolToObject(res, "xena",
        IExec->OpenResource("xena.resource") != NULL);
    cJSON_AddBoolToObject(res, "i2c",
        IExec->OpenResource("i2c.resource") != NULL);
    cJSON_AddBoolToObject(res, "acpi",
        IExec->OpenResource("acpi.resource") != NULL);
    cJSON_AddBoolToObject(res, "performancemonitor",
        IExec->OpenResource("performancemonitor.resource") != NULL);
    cJSON_AddBoolToObject(res, "fsldma",
        IExec->OpenResource("fsldma.resource") != NULL);

    *out_result = r;
    return 0;
}


/* Best-effort board detection used by proto.capabilities.
 * Driven by GetCPUInfo Family/Model with resource probes as
 * tie-breakers. AOS4's AttnFlags no longer carry PowerPC bits
 * (AFF_OTHER is typically the only PPC indicator), so we treat
 * GetCPUInfo as authoritative. */
const char *sys_detect_board(void) {
    uint32 ncpus, family, model, l1, l2, l3, vec, page, cline;
    uint64 fsb_hz, cpu_hz, tb_hz;
    const char *model_str = NULL;
    _read_cpu_info(&ncpus, &family, &model, &model_str,
                   &fsb_hz, &cpu_hz, &tb_hz,
                   &l1, &l2, &l3, &vec, &page, &cline);

    int has_fsldma = (IExec->OpenResource("fsldma.resource") != NULL);

    /* PA6T-1682M (P.A. Semi) = X1000 (Nemo). */
    if (model  == 18 /* CPUTYPE_PA6T_1682M */) return "x1000";
    if (family == 5  /* CPUFAMILY_PA6T     */) return "x1000";

    /* E5500 covers both X5000 (Cyrus Plus, Freescale P5020) and
     * A1222 (Tabor, Freescale T1042). Discriminate by model:
     *   model=21 (CPUTYPE_P50XX = QorIQ P5xxx) -> X5000.
     *   model=22 (CPUTYPE_P10XX = QorIQ P10xx) -> A1222.
     * E500 (8) is a smaller derivative; reported by older T1xxx
     * configurations - treat as A1222 unless we learn otherwise. */
    if (family == 7 /* CPUFAMILY_E5500 */ ||
        family == 8 /* CPUFAMILY_E500  */) {
        if (model == 21 /* P50XX */) return "x5000";
        if (model == 22 /* P10XX */) return "a1222";
        /* Unknown E5500/E500 chip - fall through to model_string sniff. */
        if (model_str) {
            if (strstr(model_str, "P5020") || strstr(model_str, "P5040"))
                return "x5000";
            if (strstr(model_str, "T1042") || strstr(model_str, "T2080"))
                return "a1222";
        }
        return "e5500-unknown";
    }

    /* 4XX family = Pegasos2 (440EP) or SAM440/460 (440GX/460EX).
     * fsldma ships on SAM460 onward. */
    if (family == 4 /* CPUFAMILY_4XX */) {
        if (has_fsldma) return "sam460";
        if (model == 13 /* PPC440EP - Pegasos2 / SAM440EP */) return "pegasos2";
        return "sam460";
    }

    /* 74XX = AmigaOne XE / Micro (G4-class). */
    if (family == 3 /* CPUFAMILY_74XX */) return "amigaone";
    /* 7X0 = G3 - older AmigaOnes. */
    if (family == 2 /* CPUFAMILY_7X0 */)  return "amigaone";

    return "unknown";
}

/* ---- sys.cold_reboot ------------------------------------------ */
/*
 * Software cold-reboot via IExec->ColdReboot(). Called when SystemTags
 * NP_Reboot doesn't propagate (running game holding GPU resources,
 * GrimReaper failing to come up, etc.). Spawns a tiny task that
 * sleeps delay_ms then calls ColdReboot, so the JSON-RPC response
 * gets flushed before the machine resets.
 *
 * Params:
 *   confirm:  bool, REQUIRED, must be true (gate against accidents)
 *   delay_ms: int, optional, default 500. Time between RPC return
 *             and ColdReboot. Min 100 (response flush). Max 5000.
 *
 * Returns: { queued: true, delay_ms: N }
 *
 * Note: ColdReboot is not graceful - filesystems are not synced. The
 * caller is expected to have done an Inhibit/Diskchange dance first
 * if they care about disk consistency. For our use case (recovering
 * a wedged guest under autonomous test loops) that's acceptable.
 */

#include <exec/tasks.h>

static long _coldreboot_delay_ticks = 25; /* default 500ms = 25 ticks */

static void _coldreboot_task_entry(void) {
    /* Wait the configured delay, then trigger cold reboot. The
     * IExec interface pointer is global on AOS4, valid in any task. */
    IDOS->Delay(_coldreboot_delay_ticks);
    IExec->ColdReboot();
    /* Unreachable. */
}

int sys_cold_reboot(cJSON *params, cJSON **out_result, cJSON **out_err) {
    cJSON *confirm = cJSON_GetObjectItemCaseSensitive(params, "confirm");
    if (!cJSON_IsBool(confirm) || !cJSON_IsTrue(confirm)) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "sys.cold_reboot requires confirm:true", NULL);
        return 0;
    }

    long long delay_ms = p_int(params, "delay_ms", 500);
    if (delay_ms < 100)  delay_ms = 100;
    if (delay_ms > 5000) delay_ms = 5000;
    /* Convert ms to AmigaDOS ticks (50/sec). Round up so we never
     * fire the reboot before the response has flushed. */
    _coldreboot_delay_ticks = (long)((delay_ms * 50 + 999) / 1000);

    struct Process *p = IDOS->CreateNewProcTags(
        NP_Entry,     (Tag)_coldreboot_task_entry,
        NP_Name,      (Tag)"MCPd ColdReboot",
        NP_StackSize, 16384,
        NP_Priority,  0,
        NP_Output,    (Tag)ZERO,
        NP_Input,     (Tag)ZERO,
        NP_Error,     (Tag)ZERO,
        NP_CloseOutput, FALSE,
        NP_CloseInput,  FALSE,
        NP_CloseError,  FALSE,
        TAG_DONE);
    if (!p) {
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
            "CreateNewProcTags(MCPd ColdReboot) failed", NULL);
        return 0;
    }

    cJSON *r = cJSON_CreateObject();
    cJSON_AddTrueToObject(r, "queued");
    cJSON_AddNumberToObject(r, "delay_ms", (double)delay_ms);
    *out_result = r;
    return 0;
}


/* ---- generic supervisor-mode read of CPU PA --------------------- */

/* Shared static state for the supervisor callbacks below. Multiple
 * call sites use the same Forbid/Permit + supervisor-callback pattern;
 * sharing the args structure keeps each handler concise and avoids
 * scattered globals. Serialised via Forbid so concurrent connections
 * don't clobber each other's args. */
static volatile uint32 _super_pa;
static volatile uint32 _super_len;
static uint8 *_super_dst;

static ULONG _super_memcpy_pa(struct ExecBase *unused) {
    (void)unused;
    memcpy(_super_dst, (const void *)(_super_pa), _super_len);
    return 0;
}

static void super_read_pa(uint32 pa, uint8 *dst, uint32 len) {
    IExec->Forbid();
    _super_pa = pa;
    _super_len = len;
    _super_dst = dst;
    IExec->Supervisor((ULONG (APICALL *)(struct ExecBase *))
                      _super_memcpy_pa);
    IExec->Permit();
}


/* ---- sys.read_ccsr ---------------------------------------------- */

/* Read a 32-bit register from the Freescale QorIQ Configuration,
 * Control and Status Register (CCSR) memory map.
 *
 * The CCSR is a 16 MiB region of MMIO control registers for the
 * SoC -- DDR controller, PCIe root complexes, FMAN, QMan, BMan,
 * I2C buses, eSDHC, etc. On X5000 (P5020) it lives at CPU PA
 * 0xFE000000; on A1222 (P1022) also at 0xFE000000.
 *
 * Reads must be 4-byte aligned (P5020/P1022 CCSR registers are all
 * 32-bit). Run in supervisor mode because user-space tasks don't
 * have the kernel MMU page mapping for the CCSR region.
 *
 * Used to inspect e.g. PCIe outbound translation windows
 * (PEXOWAR0/PEXOTAR0) for QEMU machine-model bring-up.
 */
#define CCSR_BASE 0xFE000000UL
#define CCSR_SIZE 0x01000000UL  /* 16 MiB */

/* Args + result for the supervisor callback. exec.library
 * IExec->Supervisor() takes a niladic ULONG-returning function -- no
 * way to pass user data through, so we use static storage and
 * serialise concurrent reads via Forbid/Permit. */
static volatile uint32 _ccsr_super_offset;
static volatile uint32 _ccsr_super_value;

static ULONG _ccsr_super_read32(struct ExecBase *unused) {
    (void)unused;
    /* CCSR registers are big-endian on the P50x0/P10x2 PowerPC SoC.
     * Direct uint32 read returns native-endian, which on this CPU IS
     * big-endian, so the value is in the right form for return. */
    volatile uint32 *p = (volatile uint32 *)(CCSR_BASE + _ccsr_super_offset);
    _ccsr_super_value = *p;
    return 0;
}

int sys_read_ccsr(cJSON *params, cJSON **out_result, cJSON **out_err) {
    long long offset = p_int(params, "offset", -1);
    if (offset < 0 || offset > (long long)(CCSR_SIZE - 4)) {
        *out_err = rpc_make_error(
            MCPD_ERR_INVPARAMS,
            "offset out of range [0..0x00FFFFFC]", NULL);
        return 0;
    }
    if (offset & 3) {
        *out_err = rpc_make_error(
            MCPD_ERR_INVPARAMS,
            "offset must be 4-byte aligned", NULL);
        return 0;
    }

    /* Optional `count` for reading a contiguous block of registers
     * (each 4 bytes, max 256 = 1 KiB). Default 1 register. */
    long long count = p_int(params, "count", 1);
    if (count < 1 || count > 256) {
        *out_err = rpc_make_error(
            MCPD_ERR_INVPARAMS,
            "count out of range [1..256]", NULL);
        return 0;
    }
    if (offset + count * 4 > (long long)CCSR_SIZE) {
        *out_err = rpc_make_error(
            MCPD_ERR_INVPARAMS,
            "offset + count*4 exceeds CCSR size", NULL);
        return 0;
    }

    cJSON *r = cJSON_CreateObject();
    cJSON_AddNumberToObject(r, "ccsr_base", (double)CCSR_BASE);
    cJSON_AddNumberToObject(r, "offset", (double)offset);
    cJSON_AddNumberToObject(r, "count", (double)count);

    cJSON *values = cJSON_AddArrayToObject(r, "values");
    cJSON *values_hex = cJSON_AddArrayToObject(r, "values_hex");

    for (long long i = 0; i < count; i++) {
        IExec->Forbid();
        _ccsr_super_offset = (uint32)(offset + i * 4);
        _ccsr_super_value = 0;
        IExec->Supervisor((ULONG (APICALL *)(struct ExecBase *))
                          _ccsr_super_read32);
        uint32 value = _ccsr_super_value;
        IExec->Permit();

        cJSON_AddItemToArray(values, cJSON_CreateNumber((double)value));
        char hex[16];
        snprintf(hex, sizeof(hex), "0x%08lx", (unsigned long)value);
        cJSON_AddItemToArray(values_hex, cJSON_CreateString(hex));
    }

    *out_result = r;
    return 0;
}


/* ---- sys.tlb_dump ---------------------------------------------- *
 *
 * Read the entire 64-entry TLB1 (variable-sized pages, software-managed,
 * the one the kernel uses for all kernel + driver mappings -- including
 * CCSR + PCI windows). Read-only; uses the `tlbre` instruction to copy
 * each entry into the MAS registers, then reads MAS1/MAS2/MAS3/MAS7.
 *
 * Why: lets us answer "what PA does VA X map to?" -- specifically
 * useful when bringing up QEMU machine models, where the LAW table
 * tells us what PA targets which peripheral, but you still need to
 * know what AOS programs into the TLB1 to convert kernel VA -> PA.
 *
 * SPRs:
 *   624 = MAS0  (TLBSEL [28-29] = 1 for TLB1; ESEL [16-19] = entry)
 *   625 = MAS1  (V [31] valid, IPROT [30], TID [16-23], TS [12], TSIZE [8-11])
 *   626 = MAS2  (EPN [12-31], WIMGE [0-4])
 *   627 = MAS3  (RPNL [12-31], U0-U3 [8-11], UX/SX/UW/SW/UR/SR [0-5])
 *   944 = MAS7  (RPNH [28-31] -- top 4 bits of 36-bit PA)
 *
 * Both e5500 (X5000 P5020) and e500v2 (A1222 P1022) use the same SPR
 * numbers. TSIZE encoding differs between the two; we emit raw TSIZE
 * and let the host decode.
 *
 * Safe at runtime: tlbre is a read instruction; no TLB modification.
 */
static volatile uint32 _tlb_entry_idx;
static volatile uint32 _tlb_mas1, _tlb_mas2, _tlb_mas3, _tlb_mas7;

static ULONG _tlb_super_read1(struct ExecBase *unused) {
    (void)unused;
    uint32 mas0 = (1u << 28) | ((_tlb_entry_idx & 0x3F) << 16);
    uint32 mas1 = 0, mas2 = 0, mas3 = 0, mas7 = 0;
    asm volatile (
        "mtspr 624,%4 \n\t"
        "isync         \n\t"
        "tlbre         \n\t"
        "isync         \n\t"
        "mfspr %0,625  \n\t"
        "mfspr %1,626  \n\t"
        "mfspr %2,627  \n\t"
        "mfspr %3,944  \n\t"
        : "=&r"(mas1), "=&r"(mas2), "=&r"(mas3), "=&r"(mas7)
        : "r"(mas0)
        : "memory"
    );
    _tlb_mas1 = mas1;
    _tlb_mas2 = mas2;
    _tlb_mas3 = mas3;
    _tlb_mas7 = mas7;
    return 0;
}

int sys_tlb_dump(cJSON *params, cJSON **out_result, cJSON **out_err) {
    (void)params; (void)out_err;

    cJSON *r = cJSON_CreateObject();
    cJSON_AddNumberToObject(r, "tlb", 1);
    cJSON_AddNumberToObject(r, "entries", 64);
    cJSON *arr = cJSON_AddArrayToObject(r, "table");

    for (uint32 i = 0; i < 64; i++) {
        IExec->Forbid();
        _tlb_entry_idx = i;
        _tlb_mas1 = _tlb_mas2 = _tlb_mas3 = _tlb_mas7 = 0;
        IExec->Supervisor((ULONG (APICALL *)(struct ExecBase *))
                          _tlb_super_read1);
        uint32 mas1 = _tlb_mas1;
        uint32 mas2 = _tlb_mas2;
        uint32 mas3 = _tlb_mas3;
        uint32 mas7 = _tlb_mas7;
        IExec->Permit();

        uint32 valid  = (mas1 >> 31) & 1;
        uint32 iprot  = (mas1 >> 30) & 1;
        uint32 tid    = (mas1 >> 16) & 0xFF;
        uint32 ts     = (mas1 >> 12) & 1;
        uint32 tsize  = (mas1 >>  7) & 0x1F; /* 5-bit on e5500 / 4-bit on e500v2 */
        uint32 epn    = mas2 & 0xFFFFF000U;
        uint32 wimge  = mas2 & 0x7F;
        uint32 rpnl   = mas3 & 0xFFFFF000U;
        uint32 perms  = mas3 & 0x3FF;
        uint32 rpnh   = mas7 & 0xF;

        cJSON *o = cJSON_CreateObject();
        cJSON_AddNumberToObject(o, "entry", (double)i);
        cJSON_AddNumberToObject(o, "v",     (double)valid);
        cJSON_AddNumberToObject(o, "iprot", (double)iprot);
        cJSON_AddNumberToObject(o, "tid",   (double)tid);
        cJSON_AddNumberToObject(o, "ts",    (double)ts);
        cJSON_AddNumberToObject(o, "tsize_raw", (double)tsize);
        cJSON_AddNumberToObject(o, "epn",   (double)epn);
        cJSON_AddNumberToObject(o, "wimge", (double)wimge);
        cJSON_AddNumberToObject(o, "rpnh",  (double)rpnh);
        cJSON_AddNumberToObject(o, "rpnl",  (double)rpnl);
        cJSON_AddNumberToObject(o, "perms", (double)perms);
        cJSON_AddNumberToObject(o, "mas1",  (double)mas1);
        cJSON_AddNumberToObject(o, "mas2",  (double)mas2);
        cJSON_AddNumberToObject(o, "mas3",  (double)mas3);
        cJSON_AddNumberToObject(o, "mas7",  (double)mas7);

        char hex[24];
        snprintf(hex, sizeof(hex), "0x%01lx%08lx",
                 (unsigned long)rpnh, (unsigned long)rpnl);
        cJSON_AddStringToObject(o, "rpn_36bit_hex", hex);

        cJSON_AddItemToArray(arr, o);
    }

    *out_result = r;
    return 0;
}


/* ---- sys.read_pa --------------------------------------------------- *
 *
 * Generic supervisor-mode read at any 36-bit physical address. Width 1
 * (byte), 2 (halfword), or 4 (word). Returns an array of integer
 * values, hex strings, AND a base64 blob of the raw bytes (handy for
 * 256-byte structure dumps where you want to feed it straight into a
 * disassembler / hex viewer host-side).
 *
 * Why it exists:
 *   - sys.read_ccsr only addresses the 16 MiB CCSR aperture and only
 *     does word-aligned reads. We can't reach low-RAM PAs (e.g. SATA
 *     CHBA at 0x0204_c000), and we can't read unaligned-byte registers
 *     (e.g. P5020 DUART LCR at +0x03 within its window).
 *   - This method exposes the existing super_read_pa() helper as
 *     JSON-RPC so the host can pull anything mapped in TLB1.
 *
 * Caveat: the PA must be in the kernel's TLB-mapped space. Hitting a
 * page that AOS hasn't mapped triggers a DSI in supervisor mode, which
 * will kill MCPd's connection-handler task (recoverable - daemon main
 * survives). The earlier `sys.fdt_read` attempt failed exactly because
 * AOS reclaims FDT pages.
 *
 * Cap: count*width <= 4096 bytes per call. Enough for a 1 KiB struct
 * dump or 4096 byte registers; chunk if you need more.
 */
int sys_read_pa(cJSON *params, cJSON **out_result, cJSON **out_err) {
    long long pa     = p_int(params, "pa", -1);
    long long count  = p_int(params, "count", 1);
    long long width  = p_int(params, "width", 4);

    if (pa < 0) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "pa required (>=0)", NULL);
        return 0;
    }
    if (width != 1 && width != 2 && width != 4) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "width must be 1, 2, or 4", NULL);
        return 0;
    }
    if (pa % width != 0) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "pa must be aligned to width", NULL);
        return 0;
    }
    if (count < 1 || count * width > 4096) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "count*width out of range [1..4096]", NULL);
        return 0;
    }

    /* DANGER: pa is NOT range-checked against TLB1 / kernel traps.
     * The empirical "safely-readable" set is small and unstable:
     * even VAs nominally covered by TLB1 entry 62 (0..1GiB) can
     * trap on supervisor read (e.g. 0x00001000, 0x40000000 in our
     * tests). A wrong PA -> DSI in supervisor mode -> the
     * connection-handler task dies. The daemon main listener
     * survives by design (spawn-per-connection isolation, see
     * mcpd/src/main.c::client_task_entry comment), but the caller
     * loses its connection. Run sys.tlb_dump first if unsure;
     * known-safe regions are CCSR (0xFE000000+) and addresses
     * inside the explicit IPROT'd TLB1 entries (kernel DMA buffers
     * around 0x01000000-0x02470000). Read at your own risk. */
    size_t total_bytes = (size_t)(count * width);
    uint8 *buf = (uint8 *)IExec->AllocVecTags(total_bytes,
        AVT_Type, MEMF_SHARED, AVT_ClearWithValue, 0, TAG_END);
    if (!buf) {
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
            "AllocVecTags failed", NULL);
        return 0;
    }

    /* Use the existing supervisor-mode memcpy helper. */
    super_read_pa((uint32)pa, buf, (uint32)total_bytes);

    cJSON *r = cJSON_CreateObject();
    cJSON_AddNumberToObject(r, "pa", (double)pa);
    cJSON_AddNumberToObject(r, "count", (double)count);
    cJSON_AddNumberToObject(r, "width", (double)width);
    cJSON_AddNumberToObject(r, "bytes", (double)total_bytes);

    cJSON *values = cJSON_AddArrayToObject(r, "values");
    cJSON *values_hex = cJSON_AddArrayToObject(r, "values_hex");

    for (long long i = 0; i < count; i++) {
        uint32 val = 0;
        if (width == 1) {
            val = buf[i];
        } else if (width == 2) {
            /* PowerPC native big-endian halfword. */
            val = ((uint32)buf[i*2]     << 8)
                |  (uint32)buf[i*2 + 1];
        } else {
            /* PowerPC native big-endian word. */
            val = ((uint32)buf[i*4]     << 24)
                | ((uint32)buf[i*4 + 1] << 16)
                | ((uint32)buf[i*4 + 2] <<  8)
                |  (uint32)buf[i*4 + 3];
        }
        cJSON_AddItemToArray(values, cJSON_CreateNumber((double)val));
        char hex[16];
        int width_chars = (int)(width * 2);
        snprintf(hex, sizeof(hex), "0x%0*lx", width_chars,
                 (unsigned long)val);
        cJSON_AddItemToArray(values_hex, cJSON_CreateString(hex));
    }

    /* Always include base64 of the raw bytes - costs little and saves
     * the host from reassembling words for blob dumps. */
    char *b64 = b64_encode(buf, total_bytes);
    if (b64) {
        cJSON_AddStringToObject(r, "bytes_b64", b64);
        b64_free(b64);
    }

    IExec->FreeVec(buf);
    *out_result = r;
    return 0;
}
