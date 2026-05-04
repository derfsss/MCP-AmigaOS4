/* methods.h - common helpers for MCPd method handlers. */
#ifndef MCPD_METHODS_H
#define MCPD_METHODS_H

#include "cJSON.h"

/* Standard application-level JSON-RPC error codes. */
#define MCPD_ERR_TARGET     (-32001)
#define MCPD_ERR_CANCELLED  (-32002)
#define MCPD_ERR_NOTCAPABLE (-32003)
#define MCPD_ERR_AUTH       (-32004)
#define MCPD_ERR_BUSY       (-32005)
#define MCPD_ERR_INVPARAMS  (-32602)
#define MCPD_ERR_INTERNAL   (-32603)

/* Pull a required string param. Returns NULL on missing/wrong type
 * and writes a -32602 error to *out_err (caller must check it). */
const char *p_str(cJSON *params, const char *name, cJSON **out_err);

/* Optional integer param with default. */
long long p_int(cJSON *params, const char *name, long long fallback);

/* Build {code:-32001, message:"...", data:{path:"..."}}. */
cJSON *target_error(const char *message, const char *path);


/* ---------------------------------------------------------------- *
 * RPC method handlers. Every entry in mcpd_methods[] (rpc.c) refers *
 * to one of these. Centralising the prototypes here lets the        *
 * compiler catch signature drift; rpc.c's table consumes them by    *
 * name.                                                             *
 * ---------------------------------------------------------------- */

#define MCPD_HANDLER(name) \
    int name(cJSON *params, cJSON **out_result, cJSON **out_err)

MCPD_HANDLER(proto_capabilities);
MCPD_HANDLER(proto_version);

MCPD_HANDLER(sys_version);
MCPD_HANDLER(sys_tasks);
MCPD_HANDLER(sys_libraries);
MCPD_HANDLER(sys_devices);
MCPD_HANDLER(sys_modules);
MCPD_HANDLER(sys_crashhook_status);
MCPD_HANDLER(sys_ports);
MCPD_HANDLER(sys_lastalert);
MCPD_HANDLER(sys_uptime);
MCPD_HANDLER(sys_memory);
MCPD_HANDLER(sys_volumes);
MCPD_HANDLER(sys_assigns);
MCPD_HANDLER(sys_hardware);
MCPD_HANDLER(sys_hardware_i2c);
MCPD_HANDLER(sys_hardware_perfcounters);
MCPD_HANDLER(sys_executable_symbols);
MCPD_HANDLER(sys_applications);
MCPD_HANDLER(sys_alert_decode);
MCPD_HANDLER(sys_cold_reboot);
MCPD_HANDLER(sys_read_ccsr);
MCPD_HANDLER(sys_tlb_dump);
MCPD_HANDLER(sys_read_pa);
MCPD_HANDLER(sys_mcu_cmd);

MCPD_HANDLER(fs_list);
MCPD_HANDLER(fs_stat);
MCPD_HANDLER(fs_read);
MCPD_HANDLER(fs_write);
MCPD_HANDLER(fs_write_chunk);
MCPD_HANDLER(fs_delete);
MCPD_HANDLER(fs_makedir);
MCPD_HANDLER(fs_rename);
MCPD_HANDLER(fs_protect);
MCPD_HANDLER(fs_copy);
MCPD_HANDLER(fs_hash);

MCPD_HANDLER(exec_cmd);

MCPD_HANDLER(wb_screens);
MCPD_HANDLER(wb_windows);
MCPD_HANDLER(wb_publicscreens);
MCPD_HANDLER(wb_frontmost);

MCPD_HANDLER(debug_task_snapshot);
MCPD_HANDLER(debug_symbol);
MCPD_HANDLER(debug_stacktrace);
MCPD_HANDLER(debug_write_memory);
MCPD_HANDLER(debug_write_register);

MCPD_HANDLER(events_wait);
MCPD_HANDLER(events_subscribe);
MCPD_HANDLER(events_unsubscribe);
MCPD_HANDLER(events_test_emit);

MCPD_HANDLER(app_notify);

#undef MCPD_HANDLER

#endif
