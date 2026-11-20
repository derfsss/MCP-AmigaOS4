/* rpc.h - JSON-RPC 2.0 dispatch (cJSON-backed).
 *
 * Method handlers receive parsed `params` (may be NULL) and produce
 * either a `*out_result` or a `*out_error` cJSON node. Caller owns
 * both nodes after return and is responsible for cJSON_Delete().
 */
#ifndef MCPD_RPC_H
#define MCPD_RPC_H

#include <stddef.h>
#include "cJSON.h"

/* THE DAEMON VERSION comes from the repository's VERSION file, the one
 * declaration the host package and the release script read as well.
 * mcpd/Makefile passes it as -DMCPD_VERSION_STR; nothing here repeats
 * the number. Every daemon-side spelling (`MCPd --version`, the $VER
 * cookie, `proto.version`, discovery) derives from this one define.
 *
 * The wire protocol version is deliberately separate: it tracks
 * framing / error-code / notification semantics, not the daemon
 * build, and only changes when those become incompatible. Adding
 * methods is backward-compatible and does NOT bump it. */
#ifndef MCPD_VERSION_STR
#error "MCPD_VERSION_STR is not set: build through mcpd/Makefile, which reads ../VERSION"
#endif
#define MCPD_PROTOCOL_VERSION "1.0"
#define MCPD_SERVER_VERSION   "mcpd/" MCPD_VERSION_STR

/* Ceiling on live connection workers. Each is a Process with a 64 KiB
 * stack that may be holding a multi-megabyte frame, so an unbounded
 * count is an unbounded commitment -- and connections that are never
 * closed (a killed client, a NAT that dropped the flow) accumulate.
 * Normal use is one or two per client; 16 leaves room for a fan-out
 * plus a long-poll and still bounds the damage. Advertised in
 * proto.capabilities limits. */
#define MCPD_MAX_CLIENTS 16

typedef int (*method_handler_fn)(cJSON *params,
                                 cJSON **out_result,
                                 cJSON **out_error);

typedef struct {
    const char       *name;
    method_handler_fn handler;
    const char       *description; /* for proto.capabilities */
} method_entry;

/* Registered methods. NULL-terminated. Defined in rpc.c. */
extern const method_entry mcpd_methods[];

/* Build a JSON-RPC error object: {code, message, data?}. Steals data. */
cJSON *rpc_make_error(int code, const char *message, cJSON *data);

/* Build the full JSON-RPC response wire bytes for a parsed request.
 * Allocates *out via malloc; caller frees. Returns 0 on success.
 *
 * On bad input the response is a well-formed error envelope (id may
 * be null), still 0.
 */
int rpc_dispatch(const char *req, size_t req_len,
                 char **out, size_t *out_len);

#endif /* MCPD_RPC_H */
