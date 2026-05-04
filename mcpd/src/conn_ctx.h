/* conn_ctx.h - per-connection task-local state for multi-client MCPd.
 *
 * §19.3 P1 #7. Each accepted TCP connection runs in its own AOS4
 * Process spawned via CreateNewProcTags. Because bsdsocket.library
 * maintains task-local socket tables, every child Process must open
 * its own SocketBase + ISocket interface; reusing the parent's
 * pointer doesn't work. We stash the per-task interface (and the
 * connection fd) in a conn_ctx struct, addressed via the task's
 * tc_UserData slot, so that frame.c can find the right interface
 * without threading parameters through every call site.
 *
 * Lifetime: the struct lives on the child task's stack inside its
 * entry function, so it's valid for the entire connection lifetime.
 * The parent task's tc_UserData is NULL → frame.c falls back to
 * the global ISocket (used by the listen socket).
 */
#ifndef MCPD_CONN_CTX_H
#define MCPD_CONN_CTX_H

#include <proto/exec.h>
#include <proto/bsdsocket.h>
#include <exec/tasks.h>

/* Per-task events.* subscription state (§19.3 P1 #7 v2 / API tidy
 * #8). Was a single `_evs` global in events.c which meant only one
 * (random) connected client received each notification. Lives here
 * so each spawned worker has its own subscription mask, baseline,
 * and synthetic-notification slot. */
struct ev_state {
    unsigned topics_mask;          /* EV_TOPIC_* bits */
    int      baseline_initialized;
    uint32_t baseline_alert_code;  /* last seen ExecBase->LastAlert[0] */
    void    *pending_synthetic;    /* opaque cJSON*; events.test_emit slot */
};

struct conn_ctx {
    struct Library     *socket_base; /* OpenLibrary("bsdsocket.library") */
    struct SocketIFace *isocket;     /* GetInterface(socket_base, "main") */
    int                 fd;          /* connection fd in this task's table */
    struct ev_state     ev;          /* per-task events subscription state */
};

/* Returns the conn_ctx for the current task, or NULL when called
 * from the parent (or any task that hasn't set tc_UserData to a
 * conn_ctx). */
static inline struct conn_ctx *mcpd_conn_ctx(void) {
    struct Task *t = IExec->FindTask(NULL);
    return t ? (struct conn_ctx *)t->tc_UserData : NULL;
}

#endif /* MCPD_CONN_CTX_H */
