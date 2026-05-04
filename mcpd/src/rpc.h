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

#define MCPD_PROTOCOL_VERSION "1.0"
#define MCPD_SERVER_VERSION   "mcpd/1.0"

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
