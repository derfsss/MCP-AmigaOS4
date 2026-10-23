/* exec.* method handlers: run AmigaDOS commands as tracked jobs. */

#include "../rpc.h"
#include "../conn_ctx.h"
#include "../jobs.h"
#include "amigautil.h"
#include "methods.h"  /* pulls in cJSON.h */

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include <proto/dos.h>
#include <dos/dos.h>


/* Maximum output exec.cmd returns inline; the rest stays readable
 * through exec.output while the job record lasts. 1 MiB. */
#define EXEC_CAP_MAX (1u << 20)

/* Default and ceiling for one exec.output read. */
#define EXEC_OUTPUT_DEFAULT (64u * 1024u)
#define EXEC_OUTPUT_MAX     EXEC_CAP_MAX

/* How often a waiting exec.cmd looks at its job: 2 ticks = 40 ms. */
#define EXEC_POLL_TICKS 2

extern int frame_write(int sock, const char *buf, size_t len);

/* Streaming: each chunk of new output goes out as a `proc.stdout`
 * notification on this connection while exec.cmd waits. */
static void _send_chunk(int sock, uint32_t job_id, const char *text,
                        size_t raw_n) {
    if (sock < 0 || raw_n == 0) return;
    cJSON *env = cJSON_CreateObject();
    if (!env) return;
    cJSON_AddStringToObject(env, "jsonrpc", "2.0");
    cJSON_AddStringToObject(env, "method", "events.notify");
    cJSON *params = cJSON_AddObjectToObject(env, "params");
    cJSON_AddStringToObject(params, "topic", "proc.stdout");
    cJSON *data = cJSON_AddObjectToObject(params, "data");
    cJSON_AddStringToObject(data, "chunk", text);
    cJSON_AddNumberToObject(data, "size", (double)raw_n);
    cJSON_AddNumberToObject(data, "job_id", (double)job_id);
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


static int _p_bool(cJSON *params, const char *name) {
    cJSON *v = cJSON_GetObjectItemCaseSensitive(params, name);
    return cJSON_IsBool(v) && cJSON_IsTrue(v);
}

/* Required positive integer job_id. */
static int _p_job_id(cJSON *params, uint32_t *out, cJSON **out_err) {
    cJSON *v = cJSON_GetObjectItemCaseSensitive(params, "job_id");
    if (!cJSON_IsNumber(v) || v->valuedouble < 1) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
                                  "job_id (positive integer) is required",
                                  NULL);
        return -1;
    }
    *out = (uint32_t)v->valuedouble;
    return 0;
}

static cJSON *_unknown_job(uint32_t id) {
    cJSON *d = cJSON_CreateObject();
    cJSON_AddNumberToObject(d, "job_id", (double)id);
    return rpc_make_error(MCPD_ERR_INVPARAMS,
        "unknown job_id (never started, or its record was evicted)", d);
}

static const char *_state_name(int state) {
    return state == JOB_RUNNING ? "running" : "exited";
}

/* Fields every exec.* result shares. */
static void _add_job_fields(cJSON *r, const struct job_info *ji) {
    cJSON_AddNumberToObject(r, "job_id", (double)ji->id);
    cJSON_AddStringToObject(r, "state", _state_name(ji->state));
    if (ji->state == JOB_EXITED) {
        cJSON_AddNumberToObject(r, "exit_code", (double)ji->rc);
        cJSON_AddNumberToObject(r, "result2", (double)ji->result2);
    } else {
        cJSON_AddNullToObject(r, "exit_code");
    }
    cJSON_AddNumberToObject(r, "pid", (double)ji->pid);
    cJSON_AddNumberToObject(r, "priority", (double)ji->priority);
    cJSON_AddNumberToObject(r, "elapsed_ms", (double)ji->elapsed_ms);
}


/* exec.cmd: start a job and wait for it, up to the timeout.
 *
 * Finished in time: the full output (capped at EXEC_CAP_MAX) and exit
 * code, and the output file is deleted. Still running at the deadline:
 * with return_running=true a normal result {state:"running", job_id,
 * output so far}; otherwise a -32001 error whose data carries the
 * job_id. The error is the default because the daemon's older callers
 * expect exec.cmd to mean "ran to completion". */
int exec_cmd(cJSON *params, cJSON **out_result, cJSON **out_err) {
    const char *cmd = p_str(params, "command", out_err);
    if (!cmd) return 0;

    long long timeout_ms = p_int(params, "timeout_ms", 0);
    if (timeout_ms <= 0) {
        timeout_ms = p_int(params, "timeout_s", 30) * 1000;
    }
    if (timeout_ms <= 0) timeout_ms = 30000;

    int priority = (int)p_int(params, "priority",
                              (long long)jobs_default_priority());
    int return_running = _p_bool(params, "return_running");
    int stream = _p_bool(params, "stream");

    /* Optional args array. */
    cJSON *args = cJSON_GetObjectItemCaseSensitive(params, "args");

    /* Build full command line. */
    size_t full_len = strlen(cmd);
    char *full = (char *)malloc(full_len + 1);
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

    /* Optional cwd: the new shell takes a copy of this process's
     * current directory when it is created, so it only needs to be
     * swapped in around the start. */
    cJSON *cwd_v = cJSON_GetObjectItemCaseSensitive(params, "cwd");
    BPTR cwd_lock = ZERO;
    BPTR old_dir = ZERO;
    if (cJSON_IsString(cwd_v) && cwd_v->valuestring && cwd_v->valuestring[0]) {
        cwd_lock = IDOS->Lock(cwd_v->valuestring, SHARED_LOCK);
        if (cwd_lock == ZERO) {
            free(full);
            *out_err = target_error("exec.cmd: cwd lock failed",
                                    cwd_v->valuestring);
            return 0;
        }
        old_dir = IDOS->SetCurrentDir(cwd_lock);
    }

    uint32_t id = 0;
    int actual_pri = priority;
    int started = jobs_start(full, priority, &id, &actual_pri, out_err);

    if (cwd_lock != ZERO) {
        IDOS->SetCurrentDir(old_dir);
        IDOS->UnLock(cwd_lock);
    }
    free(full);
    if (started != 0) return 0;

    struct conn_ctx *c = mcpd_conn_ctx();
    int sock = (stream && c) ? c->fd : -1;
    size_t streamed_to = 0;
    long long waited_ms = 0;
    struct job_info ji;

    for (;;) {
        if (jobs_get(id, &ji) != 0) {
            *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
                                      "exec.cmd: job record lost", NULL);
            return 0;
        }
        if (sock >= 0) {
            size_t raw = 0, total = 0;
            char *t = amiga_read_output(ji.out_path, streamed_to, 8192,
                                        &raw, &total);
            if (t) {
                _send_chunk(sock, id, t, raw);
                free(t);
            }
            streamed_to += raw;
        }
        if (ji.state == JOB_EXITED) break;
        if (waited_ms >= timeout_ms) break;
        IDOS->Delay(EXEC_POLL_TICKS);
        waited_ms += EXEC_POLL_TICKS * 1000 / TICKS_PER_SECOND;
    }

    if (ji.state == JOB_RUNNING && !return_running) {
        cJSON *d = cJSON_CreateObject();
        cJSON_AddNumberToObject(d, "job_id", (double)id);
        cJSON_AddStringToObject(d, "state", "running");
        cJSON_AddNumberToObject(d, "timeout_ms", (double)timeout_ms);
        *out_err = rpc_make_error(MCPD_ERR_TARGET,
            "exec.cmd: timed out; the command is still running "
            "(see exec.status / exec.kill)", d);
        return 0;
    }

    size_t raw = 0, total = 0;
    char *out = amiga_read_output(ji.out_path, 0, EXEC_CAP_MAX, &raw, &total);
    if (!out) {
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
                                  "exec.cmd: capture failed", NULL);
        return 0;
    }
    if (ji.state == JOB_EXITED) jobs_drop_output(id);

    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "output", out);
    free(out);
    _add_job_fields(r, &ji);
    cJSON_AddBoolToObject(r, "truncated", raw < total);
    cJSON_AddNumberToObject(r, "output_bytes", (double)total);
    cJSON_AddBoolToObject(r, "streamed", sock >= 0);
    if (actual_pri != priority) {
        cJSON_AddNumberToObject(r, "priority_requested", (double)priority);
    }
    *out_result = r;
    return 0;
}


/* exec.status: one job's state without its output. */
int exec_status(cJSON *params, cJSON **out_result, cJSON **out_err) {
    uint32_t id;
    if (_p_job_id(params, &id, out_err) != 0) return 0;
    struct job_info ji;
    if (jobs_get(id, &ji) != 0) {
        *out_err = _unknown_job(id);
        return 0;
    }
    cJSON *r = cJSON_CreateObject();
    _add_job_fields(r, &ji);
    cJSON_AddStringToObject(r, "command", ji.command);
    cJSON_AddBoolToObject(r, "output_available", !ji.output_deleted);
    cJSON_AddNumberToObject(r, "mem_free_before",
                            (double)ji.mem_free_before);
    if (ji.state == JOB_EXITED) {
        cJSON_AddNumberToObject(r, "mem_free_after",
                                (double)ji.mem_free_after);
    }
    cJSON *sigs = cJSON_AddArrayToObject(r, "signals_sent");
    static const char letters[4] = { 'c', 'd', 'e', 'f' };
    for (int i = 0; i < 4; i++) {
        if (ji.signals_sent & (SIGBREAKF_CTRL_C << i)) {
            char s[2] = { letters[i], '\0' };
            cJSON_AddItemToArray(sigs, cJSON_CreateString(s));
        }
    }
    *out_result = r;
    return 0;
}


/* exec.output: a slice of a job's output, running or finished. */
int exec_output(cJSON *params, cJSON **out_result, cJSON **out_err) {
    uint32_t id;
    if (_p_job_id(params, &id, out_err) != 0) return 0;
    long long offset = p_int(params, "offset", 0);
    long long max = p_int(params, "max_bytes", EXEC_OUTPUT_DEFAULT);
    if (offset < 0) offset = 0;
    if (max <= 0) max = EXEC_OUTPUT_DEFAULT;
    if (max > EXEC_OUTPUT_MAX) max = EXEC_OUTPUT_MAX;

    struct job_info ji;
    if (jobs_get(id, &ji) != 0) {
        *out_err = _unknown_job(id);
        return 0;
    }
    if (ji.output_deleted) {
        cJSON *d = cJSON_CreateObject();
        cJSON_AddNumberToObject(d, "job_id", (double)id);
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "output already returned by exec.cmd and deleted", d);
        return 0;
    }
    size_t raw = 0, total = 0;
    char *text = amiga_read_output(ji.out_path, (size_t)offset,
                                   (size_t)max, &raw, &total);
    if (!text) {
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
                                  "exec.output: oom", NULL);
        return 0;
    }
    cJSON *r = cJSON_CreateObject();
    _add_job_fields(r, &ji);
    cJSON_AddStringToObject(r, "output", text);
    free(text);
    cJSON_AddNumberToObject(r, "offset", (double)offset);
    cJSON_AddNumberToObject(r, "bytes", (double)raw);
    cJSON_AddNumberToObject(r, "total_bytes", (double)total);
    cJSON_AddBoolToObject(r, "eof", ji.state == JOB_EXITED &&
                          (size_t)offset + raw >= total);
    *out_result = r;
    return 0;
}


/* exec.kill: ask a job to stop with a break signal, then wait up to
 * wait_ms for it to exit. A program that ignores the signal keeps
 * running; that is reported, not forced. */
int exec_kill(cJSON *params, cJSON **out_result, cJSON **out_err) {
    uint32_t id;
    if (_p_job_id(params, &id, out_err) != 0) return 0;
    if (_p_bool(params, "force")) {
        *out_err = rpc_make_error(MCPD_ERR_NOTCAPABLE,
            "exec.kill: force is only available for sandboxed jobs", NULL);
        return 0;
    }
    uint32_t mask = SIGBREAKF_CTRL_C;
    const char *letter = "c";
    cJSON *sv = cJSON_GetObjectItemCaseSensitive(params, "signal");
    if (cJSON_IsString(sv) && sv->valuestring) {
        switch (sv->valuestring[0] | 0x20) {
        case 'c': mask = SIGBREAKF_CTRL_C; letter = "c"; break;
        case 'd': mask = SIGBREAKF_CTRL_D; letter = "d"; break;
        case 'e': mask = SIGBREAKF_CTRL_E; letter = "e"; break;
        case 'f': mask = SIGBREAKF_CTRL_F; letter = "f"; break;
        default:
            *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
                "signal must be one of c, d, e, f", NULL);
            return 0;
        }
    }
    long long wait_ms = p_int(params, "wait_ms", 2000);
    if (wait_ms < 0) wait_ms = 0;
    if (wait_ms > 60000) wait_ms = 60000;

    int sent = jobs_signal(id, mask);
    if (sent < 0) {
        *out_err = _unknown_job(id);
        return 0;
    }
    struct job_info ji;
    long long waited = 0;
    for (;;) {
        if (jobs_get(id, &ji) != 0) {
            *out_err = _unknown_job(id);
            return 0;
        }
        if (ji.state == JOB_EXITED || !sent || waited >= wait_ms) break;
        IDOS->Delay(EXEC_POLL_TICKS);
        waited += EXEC_POLL_TICKS * 1000 / TICKS_PER_SECOND;
    }
    cJSON *r = cJSON_CreateObject();
    _add_job_fields(r, &ji);
    cJSON_AddStringToObject(r, "signal", letter);
    cJSON_AddBoolToObject(r, "sent", sent);
    cJSON_AddBoolToObject(r, "exited", ji.state == JOB_EXITED);
    *out_result = r;
    return 0;
}


/* exec.list: every job record, newest first. */
int exec_list(cJSON *params, cJSON **out_result, cJSON **out_err) {
    (void)params; (void)out_err;
    cJSON *r = cJSON_CreateObject();
    cJSON *arr = cJSON_AddArrayToObject(r, "jobs");
    jobs_list(arr);
    cJSON_AddNumberToObject(r, "worker_priority",
                            (double)jobs_worker_priority());
    cJSON_AddNumberToObject(r, "default_priority",
                            (double)jobs_default_priority());
    cJSON_AddNumberToObject(r, "max_priority",
                            (double)jobs_max_priority());
    *out_result = r;
    return 0;
}
