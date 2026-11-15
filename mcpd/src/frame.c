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

#include <netinet/in.h>
#include <netinet/tcp.h>

/* Parent-task fallback. Multi-client child tasks open
 * their own bsdsocket interface and stash it in tc_UserData via a
 * conn_ctx struct - bsdsocket maintains task-local state, so the
 * parent's interface pointer can't be reused from a child task.
 * mcpd_isocket() returns the per-task interface when set, else this
 * global (which the parent's accept loop uses for the listen
 * socket). */
extern struct SocketIFace *ISocket;

/* frame_write: frames up to this size go out in one send() (header
 * and body copied together); up to FRAME_COALESCE_STACK bytes the
 * copy is on the stack, larger ones in a short-lived allocation. */
#define FRAME_COALESCE_MAX   (256u * 1024u)
#define FRAME_COALESCE_STACK 2048u

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

/* bsdsocket.library's errno numbers are 4.4BSD's (netinclude
 * sys/errno.h), not clib4's: EAGAIN / EWOULDBLOCK is 35. */
#define BSD_EAGAIN 35

/* Wait until `sock` can be read (for_write 0) or written (1), for at
 * most MCPD_IO_TIMEOUT_S. 1: ready; 0: the time ran out; -1: error
 * or a break signal. Not every stack honours SO_RCVTIMEO and
 * SO_SNDTIMEO (Roadshow 1.15 ignores both), so the connection's
 * silence limit is kept here, the same on every stack. */
static int wait_io(struct SocketIFace *isk, int sock, int for_write) {
    fd_set fds;
    FD_ZERO(&fds);
    FD_SET(sock, &fds);
    struct timeval tv;
    tv.tv_sec = MCPD_IO_TIMEOUT_S;
    tv.tv_usec = 0;
    int r = isk->WaitSelect(sock + 1, for_write ? NULL : &fds,
                            for_write ? &fds : NULL, NULL, &tv, NULL);
    return r < 0 ? -1 : (r > 0 ? 1 : 0);
}

/* Whether `sock` is this worker's connection, set non-blocking when
 * the worker started (main.c). Then every call is tried first and
 * WaitSelect runs only when it would block, so the time limit costs
 * nothing while data flows. */
static int nonblocking(int sock) {
    struct conn_ctx *c = mcpd_conn_ctx();
    return c && c->nbio && c->fd == sock;
}

long frame_recv_some(int sock, void *buf, size_t n) {
    struct SocketIFace *isk = _isk();
    if (!nonblocking(sock)) {
        /* Blocking socket (the stack refused FIONBIO): wait first, then
         * recv returns what is there (or 0 at EOF). */
        if (wait_io(isk, sock, 0) != 1) return -1;
        long got = isk->recv(sock, buf, n, 0);
        return got < 0 ? -1 : got;
    }
    for (;;) {
        long got = isk->recv(sock, buf, n, 0);
        if (got >= 0) return got;
        if (isk->Errno() != BSD_EAGAIN || wait_io(isk, sock, 0) != 1)
            return -1;
    }
}

int frame_send_all(int sock, const void *buf, size_t n) {
    struct SocketIFace *isk = _isk();
    const char *p = (const char *)buf;
    int nb = nonblocking(sock);
    while (n > 0) {
        /* AmigaOS bsdsocket send() takes APTR (non-const); cast away
         * const since we don't write to the buffer. On a blocking
         * socket the stack's SO_SNDTIMEO (where honoured) is the only
         * limit. */
        long sent = isk->send(sock, (char *)(uintptr_t)p, n, 0);
        if (sent > 0) {
            p += sent;
            n -= (size_t)sent;
            continue;
        }
        if (sent < 0 && nb && isk->Errno() == BSD_EAGAIN &&
            wait_io(isk, sock, 1) == 1) {
            continue;
        }
        return -1;
    }
    return 0;
}

static int read_n(int sock, void *buf, size_t n) {
    char *p = (char *)buf;
    while (n > 0) {
        long got = frame_recv_some(sock, p, n);
        if (got <= 0) return -1;
        p += got;
        n -= (size_t)got;
    }
    return 0;
}

static int write_n(int sock, const void *buf, size_t n) {
    return frame_send_all(sock, buf, n);
}

int frame_read(int sock, char **out, size_t *out_len) {
    uint8_t hdr[4];
    if (read_n(sock, hdr, 4) != 0) return -1;

    uint32_t len = ((uint32_t)hdr[0] << 24)
                 | ((uint32_t)hdr[1] << 16)
                 | ((uint32_t)hdr[2] << 8)
                 |  (uint32_t)hdr[3];
    if (len == 0 || len > MCPD_FRAME_MAX_PAYLOAD) return -1;

    /* Read one byte of the body BEFORE reserving room for the rest.
     * A header is four bytes and may claim 32 MiB; allocating on that
     * promise alone means four bytes is all it costs a peer -- hostile
     * or merely wedged -- to tie up 32 MiB for as long as it stays
     * silent. Waiting for the first real byte costs nothing in the
     * normal case, where the body is already on its way, and bounds
     * the abnormal one to a worker Process plus the receive timeout
     * (MCPD_IO_TIMEOUT_S, kept by frame_recv_some). */
    char first;
    if (read_n(sock, &first, 1) != 0) return -1;

    char *buf = (char *)IExec->AllocVecTags((uint32)len + 1,
                                            AVT_Clear, FALSE,
                                            TAG_END);
    if (!buf) return -1;
    buf[0] = first;
    if (len > 1 && read_n(sock, buf + 1, len - 1) != 0) {
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
    /* Header and body in one send() where the frame is small: sent
     * apart, Nagle holds the body's tail until the peer ACKs the
     * 4-byte header, and a peer that delays its ACKs adds that delay
     * to every response. Large frames fill whole segments, which
     * Nagle does not hold, so they keep two sends and no copy. */
    if (len <= FRAME_COALESCE_MAX) {
        char small[FRAME_COALESCE_STACK];
        char *one = small;
        if (len + 4 > sizeof(small)) {
            one = (char *)IExec->AllocVecTags((uint32)len + 4,
                                              AVT_Type, MEMF_PRIVATE,
                                              TAG_END);
        }
        if (one) {
            memcpy(one, hdr, 4);
            memcpy(one + 4, buf, len);
            int rc = write_n(sock, one, len + 4);
            if (one != small) IExec->FreeVec(one);
            return rc;
        }
        /* No memory for the copy: fall through to two sends. */
    }
    /* A large frame (an fs.read answer of up to 32 MiB) is bulk data:
     * Nagle on while it goes, as for raw streams (stream.c). Without
     * it Roadshow 1.15 sent it in small segments: chunked downloads
     * fell from ~10 to 1 MiB/s on an X5000. Setting TCP_NODELAY again
     * afterwards also sends what Nagle held. */
    struct conn_ctx *c = mcpd_conn_ctx();
    int nagle = c && c->nodelay && c->fd == sock && len > FRAME_COALESCE_MAX;
    if (nagle) {
        int off = 0;
        nagle = _isk()->setsockopt(sock, IPPROTO_TCP, TCP_NODELAY,
                                   &off, sizeof(off)) == 0;
    }
    int rc = (write_n(sock, hdr, 4) != 0 || write_n(sock, buf, len) != 0) ? -1 : 0;
    if (nagle) {
        int on = 1;
        _isk()->setsockopt(sock, IPPROTO_TCP, TCP_NODELAY, &on, sizeof(on));
    }
    return rc;
}
