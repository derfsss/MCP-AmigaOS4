/* conn_ctx.h - per-connection task-local state for multi-client MCPd.
 *
 * Each accepted TCP connection runs in its own AOS4
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
 * The parent task's tc_UserData is NULL, so frame.c falls back to
 * the global ISocket (used by the listen socket).
 */
#ifndef MCPD_CONN_CTX_H
#define MCPD_CONN_CTX_H

#include <proto/exec.h>
#include <proto/bsdsocket.h>
#include <exec/tasks.h>
#include <dos/dos.h>

/* Per-task events.* subscription state. Was a single `_evs`
 * global in events.c, which meant only one
 * (random) connected client received each notification. Lives here
 * so each spawned worker has its own subscription mask, baseline,
 * and synthetic-notification slot. */
struct ev_state {
    unsigned topics_mask;          /* EV_TOPIC_* bits */
    int      baseline_initialized;
    uint32_t baseline_alert_code;  /* last seen ExecBase->LastAlert[0] */
    void    *pending_synthetic;    /* opaque cJSON*; events.test_emit slot */
};

/* A raw byte stream that follows the current response on the wire
 * (fs.stream_get / fs.stream_put, methods/stream.c). Set by the
 * handler, run by handle_connection straight after the response
 * frame is written, before the loop can write anything else (event
 * frames) into the stream. fh == 0: no stream. */
#define MCPD_STREAM_GET 1  /* file -> socket */
#define MCPD_STREAM_PUT 2  /* socket -> file */
struct mcpd_stream {
    BPTR      fh;
    int       dir;          /* MCPD_STREAM_GET / MCPD_STREAM_PUT */
    long long remaining;    /* bytes still to move */
};

struct conn_ctx {
    struct Library     *socket_base; /* OpenLibrary("bsdsocket.library") */
    struct SocketIFace *isocket;     /* GetInterface(socket_base, "main") */
    int                 fd;          /* connection fd in this task's table */
    struct ev_state     ev;          /* per-task events subscription state */
    struct mcpd_stream  stream;      /* raw bytes after this response */
};

/* Run the stream a handler set up, if any, and clear it (the file is
 * closed on every path). 0: done or none; -1: the connection must be
 * dropped (the peer cannot tell where the stream stopped). */
int mcpd_stream_run(struct conn_ctx *ctx);

/* Close and clear a stream that will not run (the response failed). */
void mcpd_stream_cancel(struct conn_ctx *ctx);

/* Returns the conn_ctx for the current task, or NULL when called
 * from the parent (or any task that hasn't set tc_UserData to a
 * conn_ctx). */
static inline struct conn_ctx *mcpd_conn_ctx(void) {
    struct Task *t = IExec->FindTask(NULL);
    return t ? (struct conn_ctx *)t->tc_UserData : NULL;
}

#endif /* MCPD_CONN_CTX_H */
