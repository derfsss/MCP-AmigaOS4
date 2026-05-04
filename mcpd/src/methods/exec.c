/* exec.cmd method handler. */

#include "../rpc.h"
#include "../conn_ctx.h"
#include "amigautil.h"
#include "methods.h"  /* pulls in cJSON.h */

#include <stdlib.h>
#include <string.h>

#include <proto/dos.h>
#include <dos/dos.h>


/* Streaming-mode on_chunk callback: each non-empty chunk gets sent
 * as a `proc.stdout` JSON-RPC notification on the conn_ctx fd. The
 * final NULL/0 invocation just signals "child done" - we send no
 * frame (the exec.cmd response itself signals completion). */
extern int frame_write(int sock, const char *buf, size_t len);

static void _stream_chunk_cb(const char *bytes, size_t n, void *user) {
    int sock = (int)(intptr_t)user;
    if (sock < 0 || bytes == NULL || n == 0) return;
    cJSON *env = cJSON_CreateObject();
    if (!env) return;
    cJSON_AddStringToObject(env, "jsonrpc", "2.0");
    cJSON_AddStringToObject(env, "method", "events.notify");
    cJSON *params = cJSON_AddObjectToObject(env, "params");
    cJSON_AddStringToObject(params, "topic", "proc.stdout");
    cJSON *data = cJSON_AddObjectToObject(params, "data");
    /* Bytes are already UTF-8 by the time we're called. */
    cJSON_AddStringToObject(data, "chunk", bytes);
    cJSON_AddNumberToObject(data, "size", (double)n);
    char *json = cJSON_PrintUnformatted(env);
    cJSON_Delete(env);
    if (json) {
        frame_write(sock, json, strlen(json));
        free(json);
    }
}


/* Conservative AmigaDOS-style quoting. Any arg that contains a space,
 * tab, double-quote, or asterisk gets wrapped in double quotes, with
 * embedded asterisks doubled (** -> *) and embedded quotes prefixed
 * with asterisk (*" -> "). Empty args become "". Returns malloc'd
 * string; caller frees. */
static char *_quote_arg(const char *a) {
    if (!a) a = "";
    int needs_quote = (*a == '\0');
    int extra = 0;
    for (const char *p = a; *p; p++) {
        if (*p == ' ' || *p == '\t' || *p == '"' || *p == '*' || *p == '\n') {
            needs_quote = 1;
        }
        if (*p == '"' || *p == '*') extra++;
    }
    size_t inlen = strlen(a);
    size_t outlen = inlen + (size_t)extra + (needs_quote ? 2 : 0) + 1;
    char *q = (char *)malloc(outlen);
    if (!q) return NULL;
    char *o = q;
    if (needs_quote) *o++ = '"';
    for (const char *p = a; *p; p++) {
        if (*p == '"' || *p == '*') *o++ = '*';
        *o++ = *p;
    }
    if (needs_quote) *o++ = '"';
    *o = '\0';
    return q;
}


int exec_cmd(cJSON *params, cJSON **out_result, cJSON **out_err) {
    const char *cmd = p_str(params, "command", out_err);
    if (!cmd) return 0;

    long long timeout_ms = p_int(params, "timeout_ms", 0);
    if (timeout_ms <= 0) {
        timeout_ms = p_int(params, "timeout_s", 30) * 1000;
    }
    if (timeout_ms <= 0) timeout_ms = 30000;

    /* Optional args array. */
    cJSON *args = cJSON_GetObjectItemCaseSensitive(params, "args");

    /* Build full command line. */
    char *full = NULL;
    size_t full_len = strlen(cmd);
    full = (char *)malloc(full_len + 1);
    if (!full) {
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL, "exec.cmd: oom", NULL);
        return 0;
    }
    memcpy(full, cmd, full_len + 1);

    if (cJSON_IsArray(args)) {
        int n = cJSON_GetArraySize(args);
        for (int i = 0; i < n; i++) {
            cJSON *e = cJSON_GetArrayItem(args, i);
            if (!cJSON_IsString(e) || !e->valuestring) {
                free(full);
                *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
                                          "args[] entries must be strings",
                                          NULL);
                return 0;
            }
            char *q = _quote_arg(e->valuestring);
            if (!q) {
                free(full);
                *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
                                          "exec.cmd: oom", NULL);
                return 0;
            }
            size_t qlen = strlen(q);
            char *grown = (char *)realloc(full, full_len + 1 + qlen + 1);
            if (!grown) {
                free(q); free(full);
                *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
                                          "exec.cmd: oom", NULL);
                return 0;
            }
            full = grown;
            full[full_len] = ' ';
            memcpy(full + full_len + 1, q, qlen + 1);
            full_len += 1 + qlen;
            free(q);
        }
    }

    /* Optional cwd: lock + swap CurrentDir for the duration of the call. */
    const char *cwd = NULL;
    cJSON *cwd_v = cJSON_GetObjectItemCaseSensitive(params, "cwd");
    if (cJSON_IsString(cwd_v) && cwd_v->valuestring && cwd_v->valuestring[0]) {
        cwd = cwd_v->valuestring;
    }

    BPTR cwd_lock = ZERO;
    BPTR old_dir = ZERO;
    int dir_swapped = 0;
    if (cwd) {
        cwd_lock = IDOS->Lock(cwd, SHARED_LOCK);
        if (cwd_lock == ZERO) {
            free(full);
            *out_err = target_error("exec.cmd: cwd lock failed", cwd);
            return 0;
        }
        old_dir = IDOS->SetCurrentDir(cwd_lock);
        dir_swapped = 1;
    }

    /* Streaming mode (#9 / §19.3 P1 #8): when stream=true, run the
     * command async + emit proc.stdout notifications as bytes arrive.
     * Non-streaming path (default) keeps the existing
     * one-shot-capture behaviour for backward compat. */
    cJSON *stream_v = cJSON_GetObjectItemCaseSensitive(params, "stream");
    int stream = cJSON_IsBool(stream_v) && cJSON_IsTrue(stream_v);

    int rc = 0;
    char *out = NULL;
    if (stream) {
        struct conn_ctx *c = mcpd_conn_ctx();
        int sock = c ? c->fd : -1;
        out = amiga_run_command_streaming(
            full, &rc, (double)timeout_ms / 1000.0,
            200,  /* poll every 200ms */
            _stream_chunk_cb,
            (void *)(intptr_t)sock);
    } else {
        out = amiga_run_command(full, &rc, (double)timeout_ms / 1000.0);
    }

    if (dir_swapped) {
        IDOS->SetCurrentDir(old_dir);
        IDOS->UnLock(cwd_lock);
    }

    free(full);

    if (!out) {
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
                                  "exec.cmd: capture failed", NULL);
        return 0;
    }

    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "output", out);
    cJSON_AddNumberToObject(r, "exit_code", (double)rc);
    cJSON_AddBoolToObject(r, "truncated", cJSON_False);
    cJSON_AddBoolToObject(r, "streamed", stream ? cJSON_True : cJSON_False);
    free(out);
    *out_result = r;
    return 0;
}
