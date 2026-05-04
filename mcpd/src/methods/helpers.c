/* helpers.c - small convenience helpers used by every method module. */

#include "methods.h"
#include "../rpc.h"

#include <stdlib.h>

const char *p_str(cJSON *params, const char *name, cJSON **out_err) {
    if (!cJSON_IsObject(params)) {
        if (out_err && !*out_err) {
            *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
                                      "params must be an object", NULL);
        }
        return NULL;
    }
    cJSON *v = cJSON_GetObjectItemCaseSensitive(params, name);
    if (!cJSON_IsString(v) || v->valuestring == NULL) {
        if (out_err && !*out_err) {
            cJSON *data = cJSON_CreateObject();
            if (data) cJSON_AddStringToObject(data, "missing", name);
            *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
                                      "missing required string param",
                                      data);
        }
        return NULL;
    }
    return v->valuestring;
}

long long p_int(cJSON *params, const char *name, long long fallback) {
    if (!cJSON_IsObject(params)) return fallback;
    cJSON *v = cJSON_GetObjectItemCaseSensitive(params, name);
    if (!cJSON_IsNumber(v)) return fallback;
    return (long long)v->valuedouble;
}

cJSON *target_error(const char *message, const char *path) {
    cJSON *data = NULL;
    if (path) {
        data = cJSON_CreateObject();
        if (data) cJSON_AddStringToObject(data, "path", path);
    }
    return rpc_make_error(MCPD_ERR_TARGET, message, data);
}
