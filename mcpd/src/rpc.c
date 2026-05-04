/* rpc.c - cJSON-backed JSON-RPC 2.0 dispatch.
 *
 * Replaces the phase-0 hand-rolled scanner. Each method module in
 * methods/ exports its method_entry array via the static
 * mcpd_methods table at the bottom of this file.
 */

#include "rpc.h"

#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "methods/methods.h"

/* ---- error helpers ------------------------------------------------ */

cJSON *rpc_make_error(int code, const char *message, cJSON *data) {
    cJSON *e = cJSON_CreateObject();
    if (!e) {
        if (data) cJSON_Delete(data);
        return NULL;
    }
    cJSON_AddNumberToObject(e, "code", (double)code);
    cJSON_AddStringToObject(e, "message", message ? message : "");
    if (data) cJSON_AddItemToObject(e, "data", data);
    return e;
}

/* ---- dispatch ----------------------------------------------------- */

static const method_entry *find_method(const char *name) {
    for (const method_entry *m = mcpd_methods; m->name != NULL; m++) {
        if (strcmp(m->name, name) == 0) return m;
    }
    return NULL;
}

int rpc_dispatch(const char *req, size_t req_len,
                 char **out, size_t *out_len) {
    cJSON *root = cJSON_ParseWithLength(req, req_len);
    cJSON *id = NULL;
    cJSON *result = NULL;
    cJSON *err = NULL;

    if (!root || !cJSON_IsObject(root)) {
        err = rpc_make_error(-32700, "Parse error", NULL);
    } else {
        cJSON *id_node = cJSON_GetObjectItemCaseSensitive(root, "id");
        if (id_node) id = cJSON_Duplicate(id_node, 1);

        cJSON *method = cJSON_GetObjectItemCaseSensitive(root, "method");
        if (!cJSON_IsString(method) || method->valuestring == NULL) {
            err = rpc_make_error(-32600, "Invalid Request", NULL);
        } else {
            const method_entry *m = find_method(method->valuestring);
            if (!m) {
                cJSON *data = cJSON_CreateObject();
                if (data) cJSON_AddStringToObject(data, "method", method->valuestring);
                err = rpc_make_error(-32601, "Method not found", data);
            } else {
                cJSON *params = cJSON_GetObjectItemCaseSensitive(root, "params");
                int rc = m->handler(params, &result, &err);
                (void)rc;
            }
        }
    }

    /* Build response envelope. cJSON_CreateObject can return NULL on
     * OOM; bail with a non-zero return so the caller drops the
     * connection rather than producing a malformed response. */
    cJSON *envelope = cJSON_CreateObject();
    if (!envelope) {
        if (id) cJSON_Delete(id);
        if (result) cJSON_Delete(result);
        if (err) cJSON_Delete(err);
        cJSON_Delete(root);
        return -1;
    }
    cJSON_AddStringToObject(envelope, "jsonrpc", "2.0");
    if (id) cJSON_AddItemToObject(envelope, "id", id);
    else    cJSON_AddNullToObject(envelope, "id");
    if (err) {
        cJSON_AddItemToObject(envelope, "error", err);
        if (result) cJSON_Delete(result);
    } else if (result) {
        cJSON_AddItemToObject(envelope, "result", result);
    } else {
        /* Should not happen - handler returned neither. Synthesise. */
        cJSON_AddItemToObject(envelope, "error",
            rpc_make_error(-32603, "Internal error: handler returned neither result nor error", NULL));
    }

    char *printed = cJSON_PrintUnformatted(envelope);
    cJSON_Delete(envelope);
    cJSON_Delete(root);

    if (!printed) return -1;
    *out = printed;
    *out_len = strlen(printed);
    return 0;
}

/* ---- method registry ---------------------------------------------
 *
 * Method handler prototypes live in methods/methods.h (already
 * included at the top of this file). Adding a new method requires
 * adding its prototype there too - the compiler will refuse to link
 * a typo rather than discovering it at runtime.
 */

const method_entry mcpd_methods[] = {
    { "proto.capabilities", proto_capabilities, "Server capability advertisement" },
    { "proto.version",      proto_version,      "Server / protocol version" },
    { "sys.version",        sys_version,        "AmigaOS Kickstart / Workbench version" },
    { "sys.tasks",          sys_tasks,          "List ready + waiting tasks" },
    { "sys.libraries",      sys_libraries,      "List opened libraries with version + open count" },
    { "sys.devices",        sys_devices,        "List opened devices" },
    { "sys.modules",        sys_modules,        "Loaded libraries/devices/resources with load base + size (for crash address mapping)" },
    { "sys.crashhook_status", sys_crashhook_status, "IDebug crash-hook diagnostics: registered, fire_count, exception_count" },
    { "sys.ports",          sys_ports,          "List public message ports" },
    { "sys.lastalert",      sys_lastalert,      "ExecBase->LastAlert (most recent system alert code)" },
    { "sys.uptime",         sys_uptime,         "Monotonic uptime via IExec->ReadEClock" },
    { "sys.memory",         sys_memory,         "Free / largest / total memory across MEMF_ANY / SHARED / VIRTUAL" },
    { "sys.volumes",        sys_volumes,        "Mounted volumes from DOS list (LDF_VOLUMES)" },
    { "sys.assigns",        sys_assigns,        "Active AmigaDOS assigns (LDF_ASSIGNS)" },
    { "sys.hardware",       sys_hardware,       "CPU + AttnFlags + resource probes for board identification" },
    { "sys.hardware.i2c",   sys_hardware_i2c,   "Enumerate I2C buses + probe devices via i2c.resource" },
    { "sys.hardware.perfcounters", sys_hardware_perfcounters,
      "Live CPU perf counters via performancemonitor.resource" },
    { "sys.executable.symbols", sys_executable_symbols,
      "Read ELF symbol table from a binary on the target via elf.library" },
    { "sys.applications",   sys_applications,   "Registered applications via application.library GetApplicationList" },
    { "sys.alert_decode",   sys_alert_decode,   "Decode an arbitrary alert code (subsystem/general/specific)" },
    { "sys.cold_reboot",    sys_cold_reboot,    "Software cold reboot via IExec->ColdReboot (requires confirm:true; spawns delayed-reboot task so the RPC response flushes first)" },
    { "sys.read_ccsr",      sys_read_ccsr,      "Read 32-bit register(s) from Freescale QorIQ CCSR (CPU PA 0xFE000000+offset). Supervisor-mode read; serialised via Forbid/Permit. Used for SoC introspection (PCIe outbound windows, LAW registers, etc) for QEMU machine bring-up." },
    { "sys.tlb_dump",       sys_tlb_dump,       "Dump all 64 TLB1 entries (kernel-managed variable-size pages) via tlbre/MAS regs. Returns valid + TID + TS + TSIZE + EPN + RPN (36-bit) + WIMG + perms per entry. Used to verify VA->PA mappings (CCSR, PCI mem/IO windows) for QEMU machine bring-up. Read-only; safe at runtime." },
    { "sys.read_pa",        sys_read_pa,        "DANGEROUS: supervisor-mode read at any 36-bit PA. params: pa (REQUIRED), count (default 1), width (1/2/4, default 4). Returns values, values_hex, bytes_b64. *** Wrong pa -> DSI in supervisor mode -> connection-handler TASK CRASHES *** (daemon main + other connections survive by design; reconnect to recover). The 'safe' regions (verified): CCSR aperture (0xFE000000..0xFF000000) and explicit-IPROT'd kernel DMA at 0x01000000..0x02470000. TLB1 entries 62/63 nominally cover 0..2GiB but kernel traps protect chunks of it (0x00000000, 0x00001000, 0x40000000 all DSI in tests). Run sys.tlb_dump first. Cap: count*width <= 4096." },
    { "sys.mcu_cmd",        sys_mcu_cmd,        "X5000 Cyrus MCU serial supervisor protocol (TRM 8.1). params: cmd (e.g. 't', 'v', 'f', 's'), unit (default 1). Opens serial.device unit at 38400 8N1, sends '#cmd\\n', reads '$reply\\n' with 5s timeout. The canonical sensor path on X5000." },
    { "app.notify",         app_notify,         "Post a Ringhio notification via application.library" },
    { "fs.list",            fs_list,            "List a directory (returns name+type per entry)" },
    { "fs.stat",            fs_stat,            "Stat one file or directory" },
    { "fs.read",            fs_read,            "Read a file (returns base64 + size)" },
    { "fs.write",           fs_write,           "Write a file (base64 input; optional compression='zlib' + raw_size hint)" },
    { "fs.write_chunk",     fs_write_chunk,     "Chunked / resumable write at offset (option B); supports compression='zlib'" },
    { "fs.delete",          fs_delete,          "Delete a file or directory" },
    { "fs.makedir",         fs_makedir,         "Create a directory" },
    { "fs.rename",          fs_rename,          "Rename / move a file or directory" },
    { "fs.protect",         fs_protect,         "Set protection bits on a path" },
    { "fs.copy",            fs_copy,            "Copy a file (preserves protection + date via CLONE)" },
    { "fs.hash",            fs_hash,            "Streaming SHA-256 of a file (algo=sha256 only for now)" },
    { "exec.cmd",           exec_cmd,           "Run an AmigaDOS command, capture stdout" },
    { "wb.screens",         wb_screens,         "List public screens (LockIBase walk)" },
    { "wb.windows",         wb_windows,         "List windows across all screens" },
    { "wb.publicscreens",   wb_publicscreens,   "Public screens registry (LockPubScreenList)" },
    { "wb.frontmost",       wb_frontmost,       "Frontmost screen + active screen + active window" },
    { "debug.task_snapshot", debug_task_snapshot,
      "Snapshot a named task: registers + backtrace via IDebug->ReadTaskContext" },
    { "debug.symbol",       debug_symbol,
      "Resolve an address to module/function/source via IDebug->ObtainDebugSymbol" },
    { "debug.stacktrace",   debug_stacktrace,
      "Symbolicated backtrace of a named task via IDebug->StackTrace" },
    { "debug.write_memory", debug_write_memory,
      "Write base64 bytes to an arbitrary memory address (requires confirm: true)" },
    { "debug.write_register", debug_write_register,
      "Modify one register in a task's saved ExceptionContext (requires confirm: true)" },
    { "events.wait",        events_wait,
      "Long-poll for events on subscribed topics; returns first deltas or [] on timeout" },
    { "events.subscribe",   events_subscribe,
      "Server-push: register topics; daemon emits JSON-RPC notifications on change" },
    { "events.unsubscribe", events_unsubscribe,
      "Server-push: clear subscription; stop receiving notifications" },
    { "events.test_emit",   events_test_emit,
      "Synthesize a JSON-RPC notification (test path for server-push wiring)" },
    { NULL, NULL, NULL }
};
