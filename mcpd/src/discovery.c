/* discovery.c - see discovery.h.
 *
 * One-shot peer task that opens its own bsdsocket.library interface
 * (per-task requirement on AOS4) + UDP socket on port 4323, then
 * loops responding to discovery probes. The reply carries:
 *
 *   {"mcp_discovery":1, "v":1,
 *    "server":"mcpd/0.1", "protocol":"1.0",
 *    "tcp_port":4322, "host":"<hostname>",
 *    "methods":NN, "tag":"<echoed>"}
 *
 * The daemon never reports its own IP - the host learns it from
 * recvfrom's source address, which is authoritative.
 */

#include "discovery.h"
#include "rpc.h"

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


/* Bsdsocket interface owned by the discovery task only. */
static struct Library     *DiscoverySocketBase = NULL;
static struct SocketIFace *DiscoveryISocket    = NULL;

/* Snapshot of method count, captured at task spawn (so we don't
 * have to take a lock on the dispatch table). */
static int g_methods_advertised = 0;


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
        if (!strstr(buf, "\"mcp_discovery\"")) continue;

        char tag[64] = "";
        _extract_str_field(buf, "tag", tag, sizeof(tag));
        _sanitize_inplace(tag, sizeof(tag));

        char resp[512];
        int rlen = snprintf(resp, sizeof(resp),
            "{\"mcp_discovery\":1,\"v\":1,"
            "\"server\":\"" MCPD_SERVER_VERSION "\","
            "\"protocol\":\"" MCPD_PROTOCOL_VERSION "\","
            "\"tcp_port\":4322,"
            "\"host\":\"%s\","
            "\"methods\":%d,"
            "\"tag\":\"%s\"}",
            host, g_methods_advertised, tag);
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


int discovery_start(int methods_advertised) {
    g_methods_advertised = methods_advertised;

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
