/* events.* method handlers.
 *
 * Two delivery modes:
 *   1. Long-poll (`events.wait`) - the original design. Caller blocks
 *      for up to `timeout_ms` waiting for a change in any subscribed
 *      topic. Works without any per-connection state.
 *   2. Server-push (`events.subscribe` + `events.unsubscribe`) -
 *      caller registers topics once; the daemon sends JSON-RPC
 *      notifications (no `id`) whenever a baseline value changes.
 *      Backed by a global subscription struct + baseline values; the
 *      connection-handler idle-poll path drains it.
 *
 * Topics:
 *   sys.lastalert    - ExecBase->LastAlert[0] changed (raw)
 *   sys.task         - task added or removed (by name)
 *   debug.exception  - new dead-end / crash alert (decoded)
 *
 * Single-client-at-a-time daemon (limits.max_concurrent_clients=1),
 * so global subscription state is fine - no per-connection slot
 * needed.
 */

#include "../rpc.h"
#include "methods.h"

#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#include <proto/dos.h>
#include <proto/exec.h>
#include <exec/execbase.h>
#include <exec/lists.h>
#include <exec/tasks.h>


#define POLL_PERIOD_MS  200
#define MAX_TIMEOUT_MS  60000
#define MAX_TASK_NAMES  128
#define TASK_NAME_LEN   80


static char *_sanitize_str(const char *src, char *dst, size_t cap) {
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


/* Snapshot up to MAX_TASK_NAMES task names from both Ready + Wait
 * lists into `out`. Caller must hold Forbid. Returns the count. */
static int _snapshot_task_names(char (*out)[TASK_NAME_LEN]) {
    struct ExecBase *eb = (struct ExecBase *)SysBase;
    int n = 0;
    char tmp[TASK_NAME_LEN];
    /* Ready list. */
    for (struct Node *node = eb->TaskReady.lh_Head;
         node->ln_Succ != NULL && n < MAX_TASK_NAMES;
         node = node->ln_Succ) {
        _sanitize_str(node->ln_Name, tmp, sizeof(tmp));
        memcpy(out[n++], tmp, TASK_NAME_LEN);
    }
    /* Wait list. */
    for (struct Node *node = eb->TaskWait.lh_Head;
         node->ln_Succ != NULL && n < MAX_TASK_NAMES;
         node = node->ln_Succ) {
        _sanitize_str(node->ln_Name, tmp, sizeof(tmp));
        memcpy(out[n++], tmp, TASK_NAME_LEN);
    }
    return n;
}


/* Returns true if `name` appears in the first `count` entries of
 * `names`. */
static int _name_in_set(const char (*names)[TASK_NAME_LEN], int count,
                       const char *name) {
    for (int i = 0; i < count; i++) {
        if (strncmp(names[i], name, TASK_NAME_LEN) == 0) return 1;
    }
    return 0;
}


int events_wait(cJSON *params, cJSON **out_result, cJSON **out_err) {
    (void)out_err;

    long long timeout_ms = p_int(params, "timeout_ms", 5000);
    if (timeout_ms < 0) timeout_ms = 0;
    if (timeout_ms > MAX_TIMEOUT_MS) timeout_ms = MAX_TIMEOUT_MS;

    /* Parse topics (default = all). */
    int want_alert = 0, want_task = 0, want_excpt = 0;
    cJSON *topics = cJSON_GetObjectItemCaseSensitive(params, "topics");
    if (cJSON_IsArray(topics)) {
        int present = 0;
        cJSON *t;
        cJSON_ArrayForEach(t, topics) {
            if (cJSON_IsString(t) && t->valuestring) {
                if (strcmp(t->valuestring, "sys.lastalert") == 0) {
                    want_alert = 1; present = 1;
                } else if (strcmp(t->valuestring, "sys.task") == 0) {
                    want_task = 1; present = 1;
                } else if (strcmp(t->valuestring, "debug.exception") == 0) {
                    want_excpt = 1; present = 1;
                }
            }
        }
        if (!present) {
            want_alert = 1; want_task = 1; want_excpt = 1;
        }
    } else {
        want_alert = 1; want_task = 1; want_excpt = 1;
    }

    /* Baseline snapshot. */
    struct ExecBase *eb = (struct ExecBase *)SysBase;
    uint32_t prev_alert = (uint32_t)eb->LastAlert[0];

    char (*prev_tasks)[TASK_NAME_LEN] = NULL;
    int prev_count = 0;
    char (*cur_tasks)[TASK_NAME_LEN] = NULL;
    if (want_task) {
        prev_tasks = (char (*)[TASK_NAME_LEN])
            calloc(MAX_TASK_NAMES, TASK_NAME_LEN);
        cur_tasks = (char (*)[TASK_NAME_LEN])
            calloc(MAX_TASK_NAMES, TASK_NAME_LEN);
        if (!prev_tasks || !cur_tasks) {
            free(prev_tasks); free(cur_tasks);
            *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
                                      "events.wait: out of memory", NULL);
            return 0;
        }
        IExec->Forbid();
        prev_count = _snapshot_task_names(prev_tasks);
        IExec->Permit();
    }

    cJSON *events = cJSON_CreateArray();

    /* Poll loop. */
    long long elapsed = 0;
    int got_change = 0;
    while (elapsed < timeout_ms) {
        /* Sleep poll period. AmigaDOS Delay() is in ticks (50/sec). */
        long ticks = (long)((POLL_PERIOD_MS * 50 + 999) / 1000);
        if (ticks < 1) ticks = 1;
        IDOS->Delay(ticks);
        elapsed += POLL_PERIOD_MS;

        /* Bail early on Ctrl-C. */
        if (IExec->SetSignal(0, SIGBREAKF_CTRL_C) & SIGBREAKF_CTRL_C) break;

        if (want_alert || want_excpt) {
            uint32_t cur_alert = (uint32_t)eb->LastAlert[0];
            if (cur_alert != prev_alert) {
                if (want_alert) {
                    cJSON *ev = cJSON_CreateObject();
                    cJSON_AddStringToObject(ev, "topic", "sys.lastalert");
                    cJSON *data = cJSON_AddObjectToObject(ev, "data");
                    cJSON_AddNumberToObject(data, "alert_code",
                                            (double)cur_alert);
                    cJSON_AddNumberToObject(data, "previous",
                                            (double)prev_alert);
                    cJSON_AddItemToArray(events, ev);
                }
                /* `debug.exception` only fires for dead-end alerts
                 * (the high bit signals a fatal trap); recoverable
                 * alerts stay on `sys.lastalert`. Decoded subsystem /
                 * specific name included so listeners can route. */
                if (want_excpt && (cur_alert & 0x80000000UL)) {
                    cJSON *ev = cJSON_CreateObject();
                    cJSON_AddStringToObject(ev, "topic", "debug.exception");
                    cJSON *data = cJSON_AddObjectToObject(ev, "data");
                    cJSON_AddNumberToObject(data, "alert_code",
                                            (double)cur_alert);
                    /* Re-use the alert decoder from sys.c. */
                    extern void _add_decoded_alert(cJSON *parent,
                                                   uint32_t code);
                    _add_decoded_alert(data, cur_alert);
                    /* Carry the rest of the LastAlert[1..3] payload. */
                    cJSON *payload = cJSON_AddArrayToObject(data, "payload");
                    for (int i = 1; i < 4; i++) {
                        unsigned int v = (unsigned int)
                            (uint32_t)eb->LastAlert[i];
                        cJSON_AddItemToArray(payload,
                            cJSON_CreateNumber((double)v));
                    }
                    cJSON_AddItemToArray(events, ev);
                }
                prev_alert = cur_alert;
                got_change = 1;
            }
        }

        if (want_task) {
            int cur_count = 0;
            IExec->Forbid();
            cur_count = _snapshot_task_names(cur_tasks);
            IExec->Permit();

            /* Added: in cur but not in prev. */
            for (int i = 0; i < cur_count; i++) {
                if (!_name_in_set(prev_tasks, prev_count, cur_tasks[i])) {
                    cJSON *ev = cJSON_CreateObject();
                    cJSON_AddStringToObject(ev, "topic", "sys.task");
                    cJSON *data = cJSON_AddObjectToObject(ev, "data");
                    cJSON_AddStringToObject(data, "action", "added");
                    cJSON_AddStringToObject(data, "name", cur_tasks[i]);
                    cJSON_AddItemToArray(events, ev);
                    got_change = 1;
                }
            }
            /* Removed: in prev but not in cur. */
            for (int i = 0; i < prev_count; i++) {
                if (!_name_in_set(cur_tasks, cur_count, prev_tasks[i])) {
                    cJSON *ev = cJSON_CreateObject();
                    cJSON_AddStringToObject(ev, "topic", "sys.task");
                    cJSON *data = cJSON_AddObjectToObject(ev, "data");
                    cJSON_AddStringToObject(data, "action", "removed");
                    cJSON_AddStringToObject(data, "name", prev_tasks[i]);
                    cJSON_AddItemToArray(events, ev);
                    got_change = 1;
                }
            }

            /* Roll prev to current state. */
            memcpy(prev_tasks, cur_tasks,
                   (size_t)MAX_TASK_NAMES * TASK_NAME_LEN);
            prev_count = cur_count;
        }

        if (got_change) break;
    }

    free(prev_tasks);
    free(cur_tasks);

    cJSON *r = cJSON_CreateObject();
    cJSON_AddItemToObject(r, "events", events);
    cJSON_AddNumberToObject(r, "elapsed_ms", (double)elapsed);
    *out_result = r;
    return 0;
}


/* ---- server-push subscription state ----------------------------- */

#define EV_TOPIC_LASTALERT 0x01u
#define EV_TOPIC_DEBUG_EXC 0x02u
/* sys.task is intentionally excluded from server-push: walking the
 * task lists every 200ms costs too much. Use events.wait for it. */

/* Per-task events state lives on each child task's conn_ctx (set up
 * in main.c::client_task_entry). Looking it up gives each connected
 * client its own subscription mask + baseline + synthetic slot, so
 * subscribe/wait semantics stop colliding when 2+ clients are
 * connected. (§19.3 P1 #7 v2 / API tidy #8.) */

#include "../conn_ctx.h"

/* Process-global fallback for the parent task (which doesn't have a
 * conn_ctx). Practically unreachable - the parent doesn't dispatch
 * RPCs - but keeping it preserves the invariant that ev_state() never
 * returns NULL. */
static struct ev_state _parent_evs = { 0, 0, 0, NULL };

static struct ev_state *ev_state(void) {
    struct conn_ctx *c = mcpd_conn_ctx();
    return c ? &c->ev : &_parent_evs;
}

/* Forward declaration; definition lives further down in this file. */
int events_drain_synthetic(int sock);


/* Public: called by the connection idle loop in main.c. Returns 1
 * if any notifications were emitted, 0 if not. Each emitted
 * notification is a fully-framed JSON-RPC notification written via
 * frame_write(); the caller passes the live socket. */
int events_emit_pending(int sock) {
    int emitted_any = 0;

    /* Synthetic test notifications first - always drained, regardless
     * of subscription state (events.test_emit is explicit). */
    if (events_drain_synthetic(sock)) emitted_any = 1;

    if (ev_state()->topics_mask == 0) return emitted_any;

    struct ExecBase *eb = (struct ExecBase *)SysBase;

    if (!ev_state()->baseline_initialized) {
        ev_state()->baseline_alert_code = (uint32_t)eb->LastAlert[0];
        ev_state()->baseline_initialized = 1;
        return emitted_any;
    }

    uint32_t cur_alert = (uint32_t)eb->LastAlert[0];
    if (cur_alert == ev_state()->baseline_alert_code) return emitted_any;

    /* sys.lastalert (raw delta) */
    if (ev_state()->topics_mask & EV_TOPIC_LASTALERT) {
        cJSON *env = cJSON_CreateObject();
        cJSON_AddStringToObject(env, "jsonrpc", "2.0");
        cJSON_AddStringToObject(env, "method", "events.notify");
        cJSON *params = cJSON_AddObjectToObject(env, "params");
        cJSON_AddStringToObject(params, "topic", "sys.lastalert");
        cJSON *data = cJSON_AddObjectToObject(params, "data");
        cJSON_AddNumberToObject(data, "alert_code", (double)cur_alert);
        cJSON_AddNumberToObject(data, "previous",
                                (double)ev_state()->baseline_alert_code);
        char *json = cJSON_PrintUnformatted(env);
        cJSON_Delete(env);
        if (json) {
            extern int frame_write(int sock, const char *buf, size_t len);
            frame_write(sock, json, strlen(json));
            free(json);
            emitted_any = 1;
        }
    }

    /* debug.exception (decoded, dead-end only) */
    if ((ev_state()->topics_mask & EV_TOPIC_DEBUG_EXC) &&
        (cur_alert & 0x80000000UL)) {
        cJSON *env = cJSON_CreateObject();
        cJSON_AddStringToObject(env, "jsonrpc", "2.0");
        cJSON_AddStringToObject(env, "method", "events.notify");
        cJSON *params = cJSON_AddObjectToObject(env, "params");
        cJSON_AddStringToObject(params, "topic", "debug.exception");
        cJSON *data = cJSON_AddObjectToObject(params, "data");
        cJSON_AddNumberToObject(data, "alert_code", (double)cur_alert);
        extern void _add_decoded_alert(cJSON *parent, uint32_t code);
        _add_decoded_alert(data, cur_alert);
        cJSON *payload = cJSON_AddArrayToObject(data, "payload");
        for (int i = 1; i < 4; i++) {
            unsigned int v = (unsigned int)(uint32_t)eb->LastAlert[i];
            cJSON_AddItemToArray(payload,
                cJSON_CreateNumber((double)v));
        }
        char *json = cJSON_PrintUnformatted(env);
        cJSON_Delete(env);
        if (json) {
            extern int frame_write(int sock, const char *buf, size_t len);
            frame_write(sock, json, strlen(json));
            free(json);
            emitted_any = 1;
        }
    }

    ev_state()->baseline_alert_code = cur_alert;
    return emitted_any;
}


/* events.subscribe { topics: ["sys.lastalert", "debug.exception"] }
 *
 * Topics not in the list are unsubscribed. Pass an empty list to
 * unsubscribe everything (equivalent to events.unsubscribe). */
int events_subscribe(cJSON *params, cJSON **out_result, cJSON **out_err) {
    (void)out_err;
    cJSON *topics = cJSON_GetObjectItemCaseSensitive(params, "topics");
    unsigned mask = 0;
    if (cJSON_IsArray(topics)) {
        cJSON *t;
        cJSON_ArrayForEach(t, topics) {
            if (cJSON_IsString(t) && t->valuestring) {
                if (strcmp(t->valuestring, "sys.lastalert") == 0)
                    mask |= EV_TOPIC_LASTALERT;
                else if (strcmp(t->valuestring, "debug.exception") == 0)
                    mask |= EV_TOPIC_DEBUG_EXC;
            }
        }
    }
    ev_state()->topics_mask = mask;
    /* Reset baseline so the first idle-poll captures current state
     * rather than emitting an immediate spurious change. */
    ev_state()->baseline_initialized = 0;

    cJSON *r = cJSON_CreateObject();
    cJSON_AddNumberToObject(r, "topics_mask", (double)mask);
    cJSON *cur = cJSON_AddArrayToObject(r, "subscribed");
    if (mask & EV_TOPIC_LASTALERT)
        cJSON_AddItemToArray(cur, cJSON_CreateString("sys.lastalert"));
    if (mask & EV_TOPIC_DEBUG_EXC)
        cJSON_AddItemToArray(cur, cJSON_CreateString("debug.exception"));
    *out_result = r;
    return 0;
}


int events_unsubscribe(cJSON *params, cJSON **out_result, cJSON **out_err) {
    (void)params; (void)out_err;
    ev_state()->topics_mask = 0;
    ev_state()->baseline_initialized = 0;
    cJSON *r = cJSON_CreateObject();
    cJSON_AddBoolToObject(r, "ok", 1);
    *out_result = r;
    return 0;
}


/* Public: called by main.c handle_connection() on disconnect. Single-
 * client model means subscription state is global; if a client closes
 * its connection without an explicit events.unsubscribe, the next
 * client must not inherit the previous client's topics or pending
 * synthetic notification. */
void events_client_reset(void) {
    ev_state()->topics_mask = 0;
    ev_state()->baseline_initialized = 0;
    ev_state()->baseline_alert_code = 0;
    if (ev_state()->pending_synthetic) {
        cJSON_Delete((cJSON *)ev_state()->pending_synthetic);
        ev_state()->pending_synthetic = NULL;
    }
}


/* Drain the synthetic-notification slot if populated. Called from
 * events_emit_pending() on every idle/response-edge tick. */
int events_drain_synthetic(int sock) {
    if (!ev_state()->pending_synthetic) return 0;
    char *json = cJSON_PrintUnformatted((cJSON *)ev_state()->pending_synthetic);
    cJSON_Delete((cJSON *)ev_state()->pending_synthetic);
    ev_state()->pending_synthetic = NULL;
    if (!json) return 0;
    extern int frame_write(int sock, const char *buf, size_t len);
    frame_write(sock, json, strlen(json));
    free(json);
    return 1;
}


/* events.test_emit { topic: string, data?: object }
 *
 * Synthesize a JSON-RPC notification for the test harness to verify
 * the server-push path works end-to-end. The notification is
 * delivered by the next idle-poll tick (within ~200ms). */
int events_test_emit(cJSON *params, cJSON **out_result, cJSON **out_err) {
    const char *topic = p_str(params, "topic", out_err);
    if (!topic) return 0;
    cJSON *data = cJSON_GetObjectItemCaseSensitive(params, "data");

    cJSON *env = cJSON_CreateObject();
    cJSON_AddStringToObject(env, "jsonrpc", "2.0");
    cJSON_AddStringToObject(env, "method", "events.notify");
    cJSON *p = cJSON_AddObjectToObject(env, "params");
    cJSON_AddStringToObject(p, "topic", topic);
    if (cJSON_IsObject(data)) {
        cJSON_AddItemToObject(p, "data", cJSON_Duplicate(data, 1));
    } else {
        cJSON_AddObjectToObject(p, "data");
    }

    /* Replace any previously pending synthetic. */
    if (ev_state()->pending_synthetic) cJSON_Delete((cJSON *)ev_state()->pending_synthetic);
    ev_state()->pending_synthetic = env;

    cJSON *r = cJSON_CreateObject();
    cJSON_AddBoolToObject(r, "queued", 1);
    cJSON_AddStringToObject(r, "topic", topic);
    *out_result = r;
    return 0;
}
