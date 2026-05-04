/* frame.h - 4-byte big-endian length-prefix framing (LSP-style).
 *
 * Wire format:
 *   uint32_be length | <length> bytes UTF-8 JSON
 *
 * No trailing newline, no Content-Type header. Length excludes itself.
 */
#ifndef MCPD_FRAME_H
#define MCPD_FRAME_H

#include <stddef.h>
#include <stdint.h>

/* Maximum payload size accepted by read_frame(). 32 MiB is enough to
 * cover most shipped AmigaOS 4 binaries plus base64 expansion overhead
 * with comfortable headroom; the X5000's 2 GiB makes the transient
 * allocation cost trivial. For arbitrarily-large transfers prefer
 * chunked fs.read / fs.write rather than raising this further. */
#define MCPD_FRAME_MAX_PAYLOAD (32u * 1024u * 1024u)  /* 32 MiB */

/* Read one framed message from `sock`. Allocates *out via the OS
 * allocator (IExec->AllocVecTags MEMF_SHARED); caller MUST release
 * via frame_free, NOT C free(). *out_len receives payload length on
 * success.
 *
 * Returns 0 on success, -1 on socket error / EOF / bad length.
 */
int frame_read(int sock, char **out, size_t *out_len);

/* Release a buffer returned by frame_read. Routes to IExec->FreeVec
 * since frame_read uses AllocVecTags (better behaviour than clib4
 * malloc for the multi-MiB transient allocations). */
void frame_free(char *buf);

/* Write one framed message of `len` bytes to `sock`.
 * Returns 0 on success, -1 on error. */
int frame_write(int sock, const char *buf, size_t len);

#endif /* MCPD_FRAME_H */
