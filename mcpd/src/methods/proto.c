/* proto.* method handlers. */

#include "../rpc.h"
#include "../frame.h"
#include "methods.h"

#include <string.h>

#include <proto/exec.h>
#include <exec/execbase.h>

#ifndef MCPD_DATE
#define MCPD_DATE "00.00.0000"
#endif

#ifndef MCPD_SDK_VERSION
#define MCPD_SDK_VERSION "AmigaOS 4 SDK (unknown revision)"
#endif


/* Pull a feature label from each registered method's namespace
 * prefix. "fs.list" -> "fs", etc. Returns 1 if the feature was new
 * (added) or 0 if already present. */
static int _add_unique_feature(cJSON *features, const char *name) {
    int n = cJSON_GetArraySize(features);
    for (int i = 0; i < n; i++) {
        cJSON *e = cJSON_GetArrayItem(features, i);
        if (cJSON_IsString(e) && e->valuestring
            && strcmp(e->valuestring, name) == 0) return 0;
    }
    cJSON_AddItemToArray(features, cJSON_CreateString(name));
    return 1;
}


/* In sys.c. AttnFlags + GetCPUInfo + xena/fsldma resource probes. */
extern const char *sys_detect_board(void);


int proto_capabilities(cJSON *params, cJSON **out_result, cJSON **out_err) {
    (void)params; (void)out_err;

    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "server", MCPD_SERVER_VERSION);
    cJSON_AddStringToObject(r, "protocol", MCPD_PROTOCOL_VERSION);
    cJSON_AddStringToObject(r, "protocol_version", MCPD_PROTOCOL_VERSION);

    /* Build info. */
    cJSON *build = cJSON_AddObjectToObject(r, "build");
    cJSON_AddStringToObject(build, "date", MCPD_DATE);
    cJSON_AddStringToObject(build, "compiler", "ppc-amigaos-gcc");
    cJSON_AddStringToObject(build, "sdk", MCPD_SDK_VERSION);

    /* Method list - both as objects (with descriptions) for human
     * readers, and as a flat list of names for tooling that just
     * wants method-name introspection. Plus features = unique
     * namespace prefixes. */
    cJSON *methods = cJSON_AddArrayToObject(r, "methods");
    cJSON *method_names = cJSON_AddArrayToObject(r, "method_names");
    cJSON *features = cJSON_AddArrayToObject(r, "features");

    for (const method_entry *m = mcpd_methods; m->name != NULL; m++) {
        cJSON *e = cJSON_CreateObject();
        cJSON_AddStringToObject(e, "name", m->name);
        if (m->description) {
            cJSON_AddStringToObject(e, "description", m->description);
        }
        cJSON_AddItemToArray(methods, e);

        cJSON_AddItemToArray(method_names, cJSON_CreateString(m->name));

        const char *dot = strchr(m->name, '.');
        if (dot) {
            char ns[32];
            size_t n = (size_t)(dot - m->name);
            if (n >= sizeof(ns)) n = sizeof(ns) - 1;
            memcpy(ns, m->name, n);
            ns[n] = '\0';
            _add_unique_feature(features, ns);
        }
    }

    /* Static limits. The frame cap is the single source of truth in
     * frame.h; we surface it here so clients can budget chunked I/O
     * without hardcoding the value. */
    cJSON *limits = cJSON_AddObjectToObject(r, "limits");
    cJSON_AddNumberToObject(limits, "max_payload_bytes",
                            (double)MCPD_FRAME_MAX_PAYLOAD);
    cJSON_AddNumberToObject(limits, "max_concurrent_clients", 1);
    cJSON_AddNumberToObject(limits, "max_in_flight_per_client", 1);

    /* Best-effort board detection. */
    cJSON *board = cJSON_AddObjectToObject(r, "board");
    cJSON_AddStringToObject(board, "detected", sys_detect_board());
    cJSON_AddNullToObject(board, "cpld_hwrev");
    cJSON_AddNullToObject(board, "cpld_build");

    *out_result = r;
    return 0;
}


int proto_version(cJSON *params, cJSON **out_result, cJSON **out_err) {
    (void)params; (void)out_err;
    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "server", MCPD_SERVER_VERSION);
    cJSON_AddStringToObject(r, "protocol", MCPD_PROTOCOL_VERSION);
    cJSON_AddStringToObject(r, "build_date", MCPD_DATE);
    cJSON_AddStringToObject(r, "sdk", MCPD_SDK_VERSION);
    *out_result = r;
    return 0;
}
