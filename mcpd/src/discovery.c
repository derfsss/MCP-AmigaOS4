/* discovery.c - see discovery.h.
 *
 * One-shot peer task that opens its own bsdsocket.library interface
 * (per-task requirement on AOS4) + UDP socket on port 4323, then
 * loops responding to discovery probes. The reply carries:
 *
 *   {"mcp_discovery":1, "v":1,
 *    "server":"mcpd/1.4", "protocol":"1.0",
 *    "tcp_port":NNNN, "host":"<hostname>",
 *    "methods":NN, "tag":"<echoed>"}
 *
 * tcp_port is the port the RPC listener actually bound, not a
 * constant - --port would otherwise make the announcement a lie.
 *
 * The daemon never reports its own IP - the host learns it from
 * recvfrom's source address, which is authoritative.
 */

#include "discovery.h"
#include "rpc.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <proto/exec.h>
#include <proto/dos.h>
#include <proto/bsdsocket.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <dos/dostags.h>


#define DISCOVERY_BUF 2048

/* Smallest datagram we will answer. Chosen to sit below the host's
 * own probe (~77 bytes, unchanged since 1.0, so older hosts keep
 * working) and above the ~16 bytes that would otherwise buy a ~120
 * byte reply. See the comment at the acceptance check. */
#define DISCOVERY_MIN_PROBE 64


/* ---- reply budgets -------------------------------------------------
 *
 * The size floor at the acceptance check stops a sender extracting
 * more bytes than they spend. It does not stop them spending a lot: a
 * flood of full-size probes carrying a forged source address still
 * points this daemon at that address, and a sender willing to forge
 * one address is willing to forge a different one per datagram - so a
 * per-source budget on its own buys nothing against the case that
 * actually matters.
 *
 * Hence two budgets. The global one bounds what this daemon can
 * contribute to anybody's reflected flood, whatever the sources look
 * like. The per-source one stops a single real, unspoofed host from
 * spending that global budget and leaving none for the rest of the
 * LAN.
 *
 * Both are sized against what discovery actually is: one
 * fleet.discover() sends one probe per broadcast address - typically
 * two or three - and repeats only when a person asks again. A burst of
 * four replies per source refilling at one a second covers that with
 * room to spare, and the global ceiling still lets a couple of dozen
 * machines find this daemon in the same second.
 */
#define DISCOVERY_TICKS_PER_SEC   50   /* DateStamp ds_Tick units */
#define DISCOVERY_SRC_SLOTS       16
#define DISCOVERY_SRC_BURST        4
#define DISCOVERY_SRC_RATE         1   /* replies/sec, sustained */
#define DISCOVERY_GLOBAL_BURST    24
#define DISCOVERY_GLOBAL_RATE      8   /* replies/sec, sustained */

/* Tokens are scaled so a refill smaller than one token per tick still
 * accumulates instead of rounding away to nothing. */
#define RL_SCALE 1000

struct rl_bucket {
    int32_t  tokens;      /* scaled by RL_SCALE */
    uint32_t last_tick;
};

struct rl_src {
    uint32_t ip;          /* network order; 0 means the slot is free */
    uint32_t last_seen;
    struct rl_bucket b;
};

static struct rl_src    g_src[DISCOVERY_SRC_SLOTS];
static struct rl_bucket g_global;

/* Drop accounting. The AmigaOS debug ring does not wrap - once full,
 * the kernel stops accepting entries rather than overwriting the
 * oldest - so a line per dropped probe would let a flood push
 * everything else out of the one place an operator can look. Report
 * the first drop, then at most one summary a minute. */
static uint32_t g_dropped          = 0;
static uint32_t g_drop_reported    = 0;
static uint32_t g_drop_report_tick = 0;


/* Monotonic-enough tick counter from DateStamp, which needs no device
 * open in this peer task. Wraps about every 2.7 years; differences
 * stay correct across the wrap because they are computed in uint32
 * arithmetic. A backwards clock change reads as an enormous elapsed
 * time, which refills the buckets - self-correcting, and in the
 * permissive direction. */
static uint32_t _now_ticks(void) {
    struct DateStamp ds;
    IDOS->DateStamp(&ds);
    uint64_t t = ((uint64_t)ds.ds_Days * 1440u + (uint64_t)ds.ds_Minute)
                 * (uint64_t)(60 * DISCOVERY_TICKS_PER_SEC)
                 + (uint64_t)ds.ds_Tick;
    return (uint32_t)t;
}


static void _rl_init(struct rl_bucket *b, int32_t burst, uint32_t now) {
    b->tokens = burst * RL_SCALE;
    b->last_tick = now;
}


static void _rl_refill(struct rl_bucket *b, uint32_t now,
                       int32_t rate_per_sec, int32_t burst) {
    uint32_t elapsed = now - b->last_tick;
    if (elapsed == 0) return;
    b->last_tick = now;
    int64_t tok = (int64_t)b->tokens
                  + ((int64_t)elapsed * rate_per_sec * RL_SCALE)
                    / DISCOVERY_TICKS_PER_SEC;
    int64_t cap = (int64_t)burst * RL_SCALE;
    b->tokens = (int32_t)(tok > cap ? cap : tok);
}


/* Find (or make) the bucket for a source address.
 *
 * A new source starts with a full burst, so a sender forging a fresh
 * address per datagram always finds tokens here. That is deliberate:
 * forged sources are the global budget's problem, and starting new
 * arrivals empty would instead punish the real machine that just
 * booted. */
static struct rl_src *_rl_src_for(uint32_t ip, uint32_t now) {
    struct rl_src *free_slot = NULL;
    struct rl_src *oldest = &g_src[0];

    for (int i = 0; i < DISCOVERY_SRC_SLOTS; i++) {
        struct rl_src *s = &g_src[i];
        if (s->ip != 0 && s->ip == ip) {
            s->last_seen = now;
            return s;
        }
        if (s->ip == 0 && !free_slot) free_slot = s;
        if ((uint32_t)(now - s->last_seen) >
            (uint32_t)(now - oldest->last_seen)) {
            oldest = s;
        }
    }

    struct rl_src *s = free_slot ? free_slot : oldest;
    s->ip = ip;
    s->last_seen = now;
    _rl_init(&s->b, DISCOVERY_SRC_BURST, now);
    return s;
}


/* Both budgets have to allow the reply, and only then is either
 * spent, so a probe one budget refuses does not quietly drain the
 * other. */
static int _rl_allow(uint32_t ip, uint32_t now) {
    struct rl_src *s = _rl_src_for(ip, now);
    _rl_refill(&s->b, now, DISCOVERY_SRC_RATE, DISCOVERY_SRC_BURST);
    _rl_refill(&g_global, now, DISCOVERY_GLOBAL_RATE,
               DISCOVERY_GLOBAL_BURST);

    if (s->b.tokens < RL_SCALE || g_global.tokens < RL_SCALE) return 0;
    s->b.tokens    -= RL_SCALE;
    g_global.tokens -= RL_SCALE;
    return 1;
}


static void _rl_note_drop(uint32_t now) {
    g_dropped++;
    if (!g_drop_reported) {
        g_drop_reported = 1;
        g_drop_report_tick = now;
        IExec->DebugPrintF("[MCPd] discovery rate limit engaged\n");
        return;
    }
    if ((uint32_t)(now - g_drop_report_tick)
        < 60u * DISCOVERY_TICKS_PER_SEC) {
        return;
    }
    g_drop_report_tick = now;
    IExec->DebugPrintF("[MCPd] discovery dropped=%lu (rate limit)\n",
                       (unsigned long)g_dropped);
}


/* Bsdsocket interface owned by the discovery task only. */
static struct Library     *DiscoverySocketBase = NULL;
static struct SocketIFace *DiscoveryISocket    = NULL;

/* Snapshot of method count, captured at task spawn (so we don't
 * have to take a lock on the dispatch table). */
static int g_methods_advertised = 0;

/* The port the RPC listener bound. Announced as-is; see discovery.h
 * for why this is not the compile-time constant it used to be. */
static uint16_t g_tcp_port = 4322;


static int _open_socket_lib(void) {
    DiscoverySocketBase = IExec->OpenLibrary("bsdsocket.library", 4);
    if (!DiscoverySocketBase) return -1;
    DiscoveryISocket = (struct SocketIFace *)IExec->GetInterface(
        DiscoverySocketBase, "main", 1, NULL);
    if (!DiscoveryISocket) {
        IExec->CloseLibrary(DiscoverySocketBase);
        DiscoverySocketBase = NULL;
        return -1;
    }
    return 0;
}


static void _close_socket_lib(void) {
    if (DiscoveryISocket) {
        IExec->DropInterface((struct Interface *)DiscoveryISocket);
        DiscoveryISocket = NULL;
    }
    if (DiscoverySocketBase) {
        IExec->CloseLibrary(DiscoverySocketBase);
        DiscoverySocketBase = NULL;
    }
}


/* Find the JSON value for `key` (no nested escapes) and copy up to
 * `cap-1` chars into `out`. Returns 1 if found, 0 otherwise. The
 * value must be a string ("...") - integer values are skipped. */
static int _extract_str_field(const char *buf, const char *key,
                              char *out, size_t cap) {
    char needle[64];
    int n = snprintf(needle, sizeof(needle), "\"%s\"", key);
    if (n < 0 || n >= (int)sizeof(needle)) return 0;
    const char *p = strstr(buf, needle);
    if (!p) return 0;
    p += n;
    /* skip whitespace + colon */
    while (*p == ' ' || *p == '\t' || *p == ':') p++;
    if (*p != '"') return 0;
    p++;
    const char *end = strchr(p, '"');
    if (!end) return 0;
    size_t len = (size_t)(end - p);
    if (len >= cap) len = cap - 1;
    memcpy(out, p, len);
    out[len] = '\0';
    return 1;
}


/* Sanitise a name to printable ASCII (same idiom as sys.c / wb.c). */
static void _sanitize_inplace(char *s, size_t cap) {
    size_t i = 0;
    while (i < cap && s[i]) {
        unsigned char c = (unsigned char)s[i];
        if (c < 0x20 || c >= 0x7f) s[i] = '?';
        i++;
    }
}


static void _discovery_loop(void) {
    if (_open_socket_lib() != 0) return;

    int sock = DiscoveryISocket->socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) goto done;

    int reuse = 1;
    DiscoveryISocket->setsockopt(sock, SOL_SOCKET, SO_REUSEADDR,
                                 &reuse, sizeof(reuse));

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(MCPD_DISCOVERY_PORT);
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    if (DiscoveryISocket->bind(sock, (struct sockaddr *)&addr,
                               sizeof(addr)) != 0) {
        goto cleanup;
    }

    /* Snapshot hostname once. */
    char host[64] = "amiga";
    if (DiscoveryISocket->gethostname(host, sizeof(host) - 1) != 0) {
        strncpy(host, "amiga", sizeof(host));
    }
    host[sizeof(host) - 1] = '\0';
    _sanitize_inplace(host, sizeof(host));

    _rl_init(&g_global, DISCOVERY_GLOBAL_BURST, _now_ticks());

    char buf[DISCOVERY_BUF];
    for (;;) {
        struct sockaddr_in peer;
        socklen_t peer_len = sizeof(peer);

        long got = DiscoveryISocket->recvfrom(
            sock, buf, sizeof(buf) - 1, 0,
            (struct sockaddr *)&peer, &peer_len);

        if (IExec->SetSignal(0, SIGBREAKF_CTRL_C) & SIGBREAKF_CTRL_C) break;

        if (got <= 0) continue;
        buf[got] = '\0';

        /* Probe? */
        /* A probe has to look like one, and has to cost the sender
         * at least as much as the answer costs us.
         *
         * The reply is ~120 bytes. Answering a 16-byte datagram --
         * which is all `strstr` alone required -- turns the daemon
         * into a 7x UDP amplifier pointed at whatever return address
         * the sender wrote, and UDP lets them write anything. A
         * minimum probe size removes the leverage: nobody gains by
         * spending more bytes than they extract. The host's own probe
         * has always been ~77 bytes, so this costs real clients
         * nothing, and requiring the "v" field rejects a datagram
         * that merely happens to contain the magic substring.
         *
         * This does not stop someone flooding the port; it stops them
         * flooding somebody ELSE through it. */
        if (got < DISCOVERY_MIN_PROBE) continue;
        if (!strstr(buf, "\"mcp_discovery\"")) continue;
        if (!strstr(buf, "\"v\"")) continue;

        /* Looks like a probe. Whether we answer it is now a
         * question of budget - see the reply-budget block above. */
        uint32_t now = _now_ticks();
        if (!_rl_allow((uint32_t)peer.sin_addr.s_addr, now)) {
            _rl_note_drop(now);
            continue;
        }

        char tag[64] = "";
        _extract_str_field(buf, "tag", tag, sizeof(tag));
        _sanitize_inplace(tag, sizeof(tag));

        char resp[512];
        int rlen = snprintf(resp, sizeof(resp),
            "{\"mcp_discovery\":1,\"v\":1,"
            "\"server\":\"" MCPD_SERVER_VERSION "\","
            "\"protocol\":\"" MCPD_PROTOCOL_VERSION "\","
            "\"tcp_port\":%u,"
            "\"host\":\"%s\","
            "\"methods\":%d,"
            "\"tag\":\"%s\"}",
            (unsigned)g_tcp_port, host, g_methods_advertised, tag);
        if (rlen <= 0 || rlen >= (int)sizeof(resp)) continue;

        DiscoveryISocket->sendto(sock, resp, (size_t)rlen, 0,
                                 (struct sockaddr *)&peer, peer_len);
    }

cleanup:
    DiscoveryISocket->CloseSocket(sock);
done:
    _close_socket_lib();
}


/* Task entry point. NP_Entry calls this. */
static void _discovery_task_entry(void) {
    _discovery_loop();
}


int discovery_start(int methods_advertised, uint16_t tcp_port) {
    g_methods_advertised = methods_advertised;
    g_tcp_port = tcp_port;

    struct Process *p = IDOS->CreateNewProcTags(
        NP_Entry,     (Tag)_discovery_task_entry,
        NP_Name,      (Tag)"MCPd Discovery",
        NP_StackSize, 65536,
        NP_Priority,  0,
        NP_Output,    (Tag)ZERO,
        NP_Input,     (Tag)ZERO,
        NP_Error,     (Tag)ZERO,
        NP_CloseOutput, FALSE,
        NP_CloseInput,  FALSE,
        NP_CloseError,  FALSE,
        TAG_DONE);
    return p ? 0 : -1;
}
