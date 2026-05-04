/* b64.h - tiny base64 encode/decode for fs.read / fs.write payloads. */
#ifndef MCPD_B64_H
#define MCPD_B64_H

#include <stddef.h>

/* Encode `n` bytes from `in` to a NUL-terminated base64 string.
 * Allocated via IExec->AllocVecTags MEMF_SHARED (NOT clib4 malloc) -
 * caller MUST release via b64_free, not free(). NULL on OOM. */
char *b64_encode(const unsigned char *in, size_t n);

/* Decode a NUL-terminated base64 string into a buffer allocated
 * via IExec->AllocVecTags MEMF_SHARED. Sets *out_len. Caller MUST
 * release via b64_free, not free(). Returns NULL on bad input. */
unsigned char *b64_decode(const char *in, size_t *out_len);

/* Release a buffer returned by b64_encode/b64_decode. Routes to
 * IExec->FreeVec since the buffers are AllocVecTags-allocated to
 * avoid clib4 heap fragmentation under multi-MiB churn. */
void b64_free(void *buf);

#endif
