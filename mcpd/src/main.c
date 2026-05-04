/* MCPd entry point.
 *
 * Listens on TCP :4322 and dispatches JSON-RPC 2.0 calls
 * (`fs.*`, `exec.*`, `sys.*`, `proto.*`, ...) over 4-byte
 * big-endian length-prefix framing. Each accepted connection
 * is handled by a fresh sub-task (CreateNewProcTags +
 * ReleaseSocket) for fault isolation.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <proto/exec.h>
#include <proto/dos.h>
#include <proto/bsdsocket.h>
#include <proto/z.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <errno.h>

#include "frame.h"
#include "rpc.h"
#include "discovery.h"
#include "conn_ctx.h"

extern void applib_register(void);
extern void applib_shutdown(void);

#define MCPD_VERSION "1.0"
#define DEFAULT_PORT 4322
#define BACKLOG 4

#ifndef MCPD_DATE
#define MCPD_DATE "00.00.0000"
#endif

/* AmigaOS 4 SDK identifier this build was compiled against. Set
 * from the Makefile (-DMCPD_SDK_VERSION=...) so the daemon can
 * report exactly which SDK + cross-compile toolchain produced it.
 * Falls back to a generic placeholder if the build system didn't
 * supply one. */
#ifndef MCPD_SDK_VERSION
#define MCPD_SDK_VERSION "AmigaOS 4 SDK (unknown revision)"
#endif

/* Visible to AmigaDOS `Version` / `version` commands. The leading
 * NUL keeps the tag out of regular strings(1) output, the
 * __attribute__((used)) defeats dead-stripping. Same idiom as
 * SerialShell (qemu-runner/amiga/serialshell.c:78-79). */
static const char __attribute__((used)) verstag[] =
    "\0$VER: MCPd " MCPD_VERSION " (" MCPD_DATE ")";

/* Companion build-environment cookie. Not parsed by `Version`,
 * but visible in a hex dump and surfaced via proto.version. */
static const char __attribute__((used)) sdktag[] =
    "\0$BUILT-WITH: " MCPD_SDK_VERSION;

/* Globals - bsdsocket interface shared with frame.c via extern. */
struct Library     *SocketBase = NULL;
struct SocketIFace *ISocket    = NULL;

/* z.library - AmigaOS 4 shared zlib (Hyperion port). Used by
 * fs.c's _decompress_zlib path. Routing zlib's internal allocator
 * through the OS shared library avoids fragmenting clib4's heap
 * with the multi-MiB inflate buffers (option 2 from the size-cap
 * report). Drop -lz from LDFLAGS once this lands. */
struct Library  *ZBase = NULL;
struct ZIFace   *IZ    = NULL;

static int open_z_lib(void) {
    /* z.library v53 is shipped with AOS4.1 FE Update 2+. */
    ZBase = IExec->OpenLibrary("z.library", 53);
    if (!ZBase) {
        IDOS->Printf("MCPd: z.library v53+ unavailable - "
                     "compression='zlib' uploads will fail\n");
        return -1;
    }
    IZ = (struct ZIFace *)IExec->GetInterface(ZBase, "main", 1, NULL);
    if (!IZ) {
        IExec->CloseLibrary(ZBase);
        ZBase = NULL;
        IDOS->Printf("MCPd: z.library main interface unavailable\n");
        return -1;
    }
    return 0;
}

static void close_z_lib(void) {
    if (IZ) IExec->DropInterface((struct Interface *)IZ);
    if (ZBase) IExec->CloseLibrary(ZBase);
    IZ = NULL;
    ZBase = NULL;
}

static int open_socket_lib(void) {
    SocketBase = IExec->OpenLibrary("bsdsocket.library", 4);
    if (!SocketBase) {
        IDOS->Printf("MCPd: bsdsocket.library v4+ unavailable\n");
        return -1;
    }
    ISocket = (struct SocketIFace *)IExec->GetInterface(
        SocketBase, "main", 1, NULL);
    if (!ISocket) {
        IExec->CloseLibrary(SocketBase);
        SocketBase = NULL;
        IDOS->Printf("MCPd: bsdsocket main interface unavailable\n");
        return -1;
    }
    return 0;
}

static void close_socket_lib(void) {
    if (ISocket) IExec->DropInterface((struct Interface *)ISocket);
    if (SocketBase) IExec->CloseLibrary(SocketBase);
    ISocket = NULL;
    SocketBase = NULL;
}

static int make_listen_socket(uint16_t port) {
    int s = ISocket->socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) {
        IDOS->Printf("MCPd: socket() failed\n");
        return -1;
    }

    int one = 1;
    if (ISocket->setsockopt(s, SOL_SOCKET, SO_REUSEADDR,
                            &one, sizeof(one)) != 0) {
        IDOS->Printf("MCPd: setsockopt(SO_REUSEADDR) failed errno=%ld\n",
                     (long)ISocket->Errno());
    }
#ifdef SO_REUSEPORT
    /* Belt + braces: SO_REUSEPORT allows multiple listeners. We don't
     * need that, but it can short-circuit Roadshow's TIME_WAIT block
     * after a previous crash. */
    ISocket->setsockopt(s, SOL_SOCKET, SO_REUSEPORT, &one, sizeof(one));
#endif

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    addr.sin_addr.s_addr = htonl(INADDR_ANY);

    if (ISocket->bind(s, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
        IDOS->Printf("MCPd: bind(:%lu) failed errno=%ld\n",
                     (unsigned long)port, (long)ISocket->Errno());
        ISocket->CloseSocket(s);
        return -1;
    }
    if (ISocket->listen(s, BACKLOG) != 0) {
        IDOS->Printf("MCPd: listen() failed\n");
        ISocket->CloseSocket(s);
        return -1;
    }
    return s;
}

extern int events_emit_pending(int sock);
extern void events_client_reset(void);

extern int crashhook_init(void);
extern void crashhook_shutdown(void);
extern uint32_t crashhook_signal_mask(void);
extern int crashhook_drain(int sock);

/* Handle one connection. WaitSelect with a 200ms timeout drives an
 * idle-poll loop: when the socket is readable, dispatch the request
 * normally; on timeout, emit any pending server-push notifications
 * (events.subscribe topics that changed) + drain the crash hook.
 * Returns when the client disconnects or a frame error occurs.
 *
 * Multi-client note (§19.3 P1 #7): this runs in a child Process
 * spawned per accepted connection. The crashhook signal mask is
 * NOT used here because the AllocSignal'd bit lives in the parent
 * task; children would need their own bit. Polling the crash flag
 * at every 200 ms timeout is sufficient (latency tradeoff
 * documented in §19). The function uses _isk() inside frame.c via
 * tc_UserData so each child uses its own per-task ISocket. */
static void handle_connection(int sock) {
    /* Pull our task-local ISocket out of conn_ctx for the WaitSelect
     * call. (frame_read/frame_write inside the loop already use
     * the task-local interface via _isk().) */
    struct conn_ctx *ctx = mcpd_conn_ctx();
    struct SocketIFace *isk = ctx && ctx->isocket ? ctx->isocket : ISocket;

    for (;;) {
        fd_set rfds;
        FD_ZERO(&rfds);
        FD_SET(sock, &rfds);
        struct timeval tv;
        tv.tv_sec = 0;
        tv.tv_usec = 200000;  /* 200 ms */

        int sel = isk->WaitSelect(sock + 1, &rfds, NULL, NULL,
                                  &tv, NULL);
        if (sel < 0) {
            return;  /* socket error */
        }
        if (sel == 0) {
            /* Timeout. Poll the global crash flag - any client may
             * drain it (first-come wins; subsequent clients see it
             * already drained). Then any subscribed-topic deltas. */
            (void)crashhook_drain(sock);
            (void)events_emit_pending(sock);
            continue;
        }

        /* Socket has data: read+dispatch+respond. */
        char *req = NULL;
        size_t req_len = 0;
        if (frame_read(sock, &req, &req_len) != 0) return;

        char *resp = NULL;
        size_t resp_len = 0;
        if (rpc_dispatch(req, req_len, &resp, &resp_len) != 0) {
            frame_free(req);
            return;
        }
        frame_free(req);

        /* Drain pending event notifications BEFORE the response so
         * they lead the response on the wire. This makes single-frame
         * clients (a `request()` that reads one frame and returns)
         * still see notifications: the response is the LAST frame
         * for the round, so notifications ride in front of it. */
        (void)crashhook_drain(sock);
        (void)events_emit_pending(sock);

        int wrote = frame_write(sock, resp, resp_len);
        free(resp);
        if (wrote != 0) return;
    }
}

/* ---- multi-client (§19.3 P1 #7): spawn-per-connection worker -- */

/* Entry point for a per-connection child Process spawned via
 * CreateNewProcTags. The inherited socket id is passed through
 * NP_Child / tc_UserData (we set it to the int32 id pre-spawn).
 * The child opens its own bsdsocket interface (bsdsocket maintains
 * task-local state, so it cannot reuse the parent's), claims the
 * inherited fd, runs handle_connection(), and exits.
 *
 * On AOS4 a Process exits when this function returns; the kernel
 * reclaims its stack + Process struct.
 *
 * --- Fault isolation note ---
 * The spawn-per-connection design IS the daemon's safety net:
 * if a request handler hits an exception (DSI / alignment / etc.)
 * the kernel kills only this Process. The main listener (which
 * never executes user-handler code) survives, and other in-flight
 * connections continue. Examples that intentionally land on this
 * path: sys.read_pa with an unmapped PA, supervisor-mode reads
 * that race with TLB1 expunge. Diagnose via sys.uptime (alive),
 * sys.crashhook_status (exception_count), sys.lastalert, then
 * exec.cmd C:DumpDebugBuffer for the full register/stack dump.
 * See memory entry reference_mcpd_per_connection_isolation.md. */
static void client_task_entry(void) {
    struct Task *self = IExec->FindTask(NULL);

    /* Suppress AmigaDOS requesters for this process. A bare DOS
     * Lock() / Examine() on a mounted-but-empty volume (e.g. a
     * CDFS handler with no media) otherwise pops a "Please insert
     * volume X" system requester that blocks the daemon until a
     * human dismisses it. Setting pr_WindowPtr = -1 makes those
     * calls fail with ERROR_OBJECT_NOT_FOUND instead, which our
     * fs_error_for() surfaces to the client cleanly.
     *
     * Per-process, so this is the right place to set it (each
     * connection runs in its own spawned Process). */
    struct Process *me = (struct Process *)self;
    me->pr_WindowPtr = (APTR)-1;

    /* tc_UserData was set to the inherited socket id by the parent
     * before CreateNewProcTags returned. Pull it out, then overwrite
     * with our conn_ctx so frame.c can find our ISocket. */
    int32 socket_id = (int32)(intptr_t)self->tc_UserData;

    struct conn_ctx ctx = { 0 };
    ctx.socket_base = IExec->OpenLibrary("bsdsocket.library", 4);
    if (!ctx.socket_base) return;
    ctx.isocket = (struct SocketIFace *)IExec->GetInterface(
        ctx.socket_base, "main", 1, NULL);
    if (!ctx.isocket) {
        IExec->CloseLibrary(ctx.socket_base);
        return;
    }

    /* Claim the parent's released fd into our task's socket table. */
    ctx.fd = ctx.isocket->ObtainSocket(socket_id, AF_INET, SOCK_STREAM, 0);
    if (ctx.fd < 0) {
        IExec->DropInterface((struct Interface *)ctx.isocket);
        IExec->CloseLibrary(ctx.socket_base);
        return;
    }

    /* SO_LINGER linger=0 - send RST instead of FIN on close so a
     * crash here doesn't leave port 4322 in TIME_WAIT for minutes. */
    struct linger lng = { 1, 0 };
    ctx.isocket->setsockopt(ctx.fd, SOL_SOCKET, SO_LINGER,
                            &lng, sizeof(lng));

    /* Wire the conn_ctx into tc_UserData so frame.c's _isk() finds
     * us. (Replaces the inherited socket_id we read above.) */
    self->tc_UserData = &ctx;

    handle_connection(ctx.fd);

    /* Cleanup. Per-task interface ALWAYS gets its own ISocket close. */
    self->tc_UserData = NULL;
    ctx.isocket->CloseSocket(ctx.fd);
    IExec->DropInterface((struct Interface *)ctx.isocket);
    IExec->CloseLibrary(ctx.socket_base);
}

/* Spawn a child Process to handle `client_fd`. Releases the fd from
 * the parent's task into the global socket namespace, then passes
 * the released id to the child via NP_Child. Returns 0 on success,
 * -1 if the spawn failed (caller should close the fd as a fallback).
 *
 * Stack: 65536 - matches discovery's worker, comfortable headroom for
 * the deepest method handler call chain. */
static int spawn_client_worker(int client_fd) {
    int32 id = ISocket->ReleaseSocket(client_fd, -1);
    if (id < 0) {
        IDOS->Printf("MCPd: ReleaseSocket failed errno=%ld\n",
                     (long)ISocket->Errno());
        return -1;
    }

    /* CreateNewProcTags accepts NP_UserData which sets pr_UserData on
     * the spawned Process. Our entry function reads tc_UserData
     * (Task struct field), and AOS4 sets tc_UserData == pr_UserData
     * for new Processes per the autodoc. */
    struct Process *p = IDOS->CreateNewProcTags(
        NP_Entry,        (Tag)client_task_entry,
        NP_Name,         (Tag)"MCPd Client",
        NP_StackSize,    65536,
        /* -1 = below Workbench (priority 0) so heavy daemon work --
         * chunked uploads, recursive copies, exec.cmd subprocesses --
         * never competes with the user dragging windows. Idle clients
         * sleep on socket Wait() so this only matters during active
         * RPC work. */
        NP_Priority,     -1,
        NP_UserData,     (Tag)(intptr_t)id,
        NP_Output,       (Tag)ZERO,
        NP_Input,        (Tag)ZERO,
        NP_Error,        (Tag)ZERO,
        NP_CloseOutput,  FALSE,
        NP_CloseInput,   FALSE,
        NP_CloseError,   FALSE,
        TAG_DONE);
    if (!p) {
        /* Spawn failed - try to take the fd back so we can close it. */
        int back = ISocket->ObtainSocket(id, AF_INET, SOCK_STREAM, 0);
        if (back >= 0) ISocket->CloseSocket(back);
        return -1;
    }
    return 0;
}

static void usage(void) {
    IDOS->Printf(
        "Usage: MCPd [--version] [--port N]\n"
        "  --version    print version and exit\n"
        "  --port N     listen on TCP port N (default %lu)\n",
        (unsigned long)DEFAULT_PORT);
}

int main(int argc, char **argv) {
    uint16_t port = DEFAULT_PORT;

    /* Suppress system requesters in the parent process too. The
     * listener loop itself doesn't usually touch DOS, but Discovery
     * helpers / future config-file loaders might, and a popup in
     * the parent would block the listen+accept loop entirely. */
    struct Process *me = (struct Process *)IExec->FindTask(NULL);
    me->pr_WindowPtr = (APTR)-1;

    for (int i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--version") == 0) {
            IDOS->Printf("MCPd %s (%s)\n", MCPD_VERSION, MCPD_DATE);
            IDOS->Printf("Built with: %s\n", MCPD_SDK_VERSION);
            return 0;
        }
        if (strcmp(argv[i], "--help") == 0 || strcmp(argv[i], "-h") == 0) {
            usage();
            return 0;
        }
        if (strcmp(argv[i], "--port") == 0 && i + 1 < argc) {
            long p = strtol(argv[++i], NULL, 10);
            if (p < 1 || p > 65535) {
                IDOS->Printf("MCPd: invalid --port value\n");
                return 64;
            }
            port = (uint16_t)p;
            continue;
        }
        IDOS->Printf("MCPd: unknown argument: %s\n", argv[i]);
        usage();
        return 64;
    }

    if (open_socket_lib() != 0) return 1;

    /* z.library is best-effort - if the system doesn't have it, the
     * non-compressed paths still work and compression='zlib' returns
     * an explicit InvalidParams. */
    (void)open_z_lib();

    int listen_sock = make_listen_socket(port);
    if (listen_sock < 0) {
        close_z_lib();
        close_socket_lib();
        return 1;
    }

    /* Count advertised methods (the registry is NULL-terminated). */
    int method_count = 0;
    for (const method_entry *m = mcpd_methods; m->name != NULL; m++) {
        method_count++;
    }

    /* Spawn the LAN-discovery responder peer task. Best-effort:
     * a discovery failure doesn't take MCPd down. */
    if (discovery_start(method_count) != 0) {
        IDOS->Printf("MCPd: discovery responder failed to start\n");
    }

    /* Register with application.library so MCPd shows up in AmiDock
     * and other AOS-side tools. Best-effort; degrades silently if
     * application.library v53.11+ isn't available. */
    applib_register();

    /* Install the global IDebug crash hook so a kernel exception in
     * any task surfaces as a debug.exception notification. Best-
     * effort: a failure here just means clients fall back to the
     * LastAlert-polling path. */
    if (crashhook_init() != 0) {
        IDOS->Printf("MCPd: crash hook registration failed "
                     "(non-fatal)\n");
    }

    IDOS->Printf("MCPd %s listening on :%lu (Ctrl-C to stop)\n",
                 MCPD_VERSION, (unsigned long)port);

    /* Multi-client (§19.3 P1 #7): the parent task does nothing but
     * accept + spawn. Each child Process owns its own connection
     * lifecycle. The single-client wedge that bit us when an old
     * client's socket was still open while a new one tried to
     * connect is gone. */
    for (;;) {
        struct sockaddr_in peer;
        socklen_t peer_len = sizeof(peer);
        int client = ISocket->accept(listen_sock,
                                     (struct sockaddr *)&peer,
                                     &peer_len);
        if (client < 0) {
            if (IExec->SetSignal(0, SIGBREAKF_CTRL_C) & SIGBREAKF_CTRL_C) break;
            continue;
        }
        if (spawn_client_worker(client) != 0) {
            /* Spawn failed - close the fd so we don't leak it; loop
             * back and accept the next connection. */
            IDOS->Printf("MCPd: spawn_client_worker failed; "
                         "dropping connection\n");
            ISocket->CloseSocket(client);
        }
        /* spawn_client_worker has either handed the fd to the child
         * (via ReleaseSocket) or recovered + closed it on failure -
         * either way the parent owns nothing. */

        if (IExec->SetSignal(0, SIGBREAKF_CTRL_C) & SIGBREAKF_CTRL_C) break;
    }

    ISocket->CloseSocket(listen_sock);
    applib_shutdown();
    close_z_lib();
    close_socket_lib();
    IDOS->Printf("MCPd: shutdown\n");
    return 0;
}
