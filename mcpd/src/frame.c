/* frame.c - see frame.h. */

#include "frame.h"

#include <stdlib.h>
#include <string.h>

#include <proto/bsdsocket.h>
#include <proto/exec.h>
#include <exec/memory.h>
#include <exec/exectags.h>
#include <exec/tasks.h>

#include "conn_ctx.h"

/* Parent-task fallback. Multi-client (§19.3 P1 #7) child tasks open
 * their own bsdsocket interface and stash it in tc_UserData via a
 * conn_ctx struct - bsdsocket maintains task-local state, so the
 * parent's interface pointer can't be reused from a child task.
 * mcpd_isocket() returns the per-task interface when set, else this
 * global (which the parent's accept loop uses for the listen
 * socket). */
extern struct SocketIFace *ISocket;

static struct SocketIFace *_isk(void) {
    struct conn_ctx *c = mcpd_conn_ctx();
    return (c && c->isocket) ? c->isocket : ISocket;
}

/* The frame body can be up to MCPD_FRAME_MAX_PAYLOAD (32 MiB). Routing
 * that allocation through clib4's malloc fragments the C heap under
 * repeated multi-MiB churn (chunked-upload tests reproduce this on the
 * 3rd consecutive 4-10 MiB chunk). IExec->AllocVecTags hits the OS
 * allocator instead - much better at handling large blocks and
 * doesn't share heap state with the small-allocation traffic that
 * clib4 does well. */

static int read_n(int sock, void *buf, size_t n) {
    struct SocketIFace *isk = _isk();
    char *p = (char *)buf;
    while (n > 0) {
        long got = isk->recv(sock, p, n, 0);
        if (got <= 0) return -1;
        p += got;
        n -= (size_t)got;
    }
    return 0;
}

static int write_n(int sock, const void *buf, size_t n) {
    struct SocketIFace *isk = _isk();
    const char *p = (const char *)buf;
    while (n > 0) {
        /* AmigaOS bsdsocket send() takes APTR (non-const); cast away
         * const since we don't write to the buffer. */
        long sent = isk->send(sock, (char *)(uintptr_t)p, n, 0);
        if (sent <= 0) return -1;
        p += sent;
        n -= (size_t)sent;
    }
    return 0;
}

int frame_read(int sock, char **out, size_t *out_len) {
    uint8_t hdr[4];
    if (read_n(sock, hdr, 4) != 0) return -1;

    uint32_t len = ((uint32_t)hdr[0] << 24)
                 | ((uint32_t)hdr[1] << 16)
                 | ((uint32_t)hdr[2] << 8)
                 |  (uint32_t)hdr[3];
    if (len == 0 || len > MCPD_FRAME_MAX_PAYLOAD) return -1;

    char *buf = (char *)IExec->AllocVecTags((uint32)len + 1,
                                            AVT_Clear, FALSE,
                                            TAG_END);
    if (!buf) return -1;
    if (read_n(sock, buf, len) != 0) {
        IExec->FreeVec(buf);
        return -1;
    }
    buf[len] = '\0';
    *out = buf;
    *out_len = len;
    return 0;
}

/* Free a buffer returned by frame_read. Callers historically used
 * free() since the buffer was malloc'd; route via FreeVec now that
 * frame_read uses AllocVecTags. */
void frame_free(char *buf) {
    if (buf) IExec->FreeVec(buf);
}

int frame_write(int sock, const char *buf, size_t len) {
    if (len > MCPD_FRAME_MAX_PAYLOAD) return -1;
    uint8_t hdr[4] = {
        (uint8_t)((len >> 24) & 0xff),
        (uint8_t)((len >> 16) & 0xff),
        (uint8_t)((len >>  8) & 0xff),
        (uint8_t)( len        & 0xff),
    };
    if (write_n(sock, hdr, 4) != 0) return -1;
    if (write_n(sock, buf, len) != 0) return -1;
    return 0;
}
