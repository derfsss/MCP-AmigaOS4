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

/* Maximum payload size accepted by frame_read() and sent by
 * frame_write(). 32 MiB is enough to cover most shipped AmigaOS 4
 * binaries plus base64 expansion overhead with comfortable headroom; the X5000's 2 GiB makes the transient
 * allocation cost trivial. For arbitrarily-large transfers use
 * fs.stream_get / fs.stream_put (raw bytes after the response,
 * methods/stream.c) rather than raising this further. */
#define MCPD_FRAME_MAX_PAYLOAD (32u * 1024u * 1024u)  /* 32 MiB */

/* Read one framed message from `sock`. Allocates *out via the OS
 * allocator (IExec->AllocVecTags); caller MUST release
 * via frame_free, NOT C free(). *out_len receives payload length on
 * success.
 *
 * Returns 0 on success, -1 on socket error / EOF / bad length.
 */
int frame_read(int sock, char **out, size_t *out_len);

/* Release a buffer returned by frame_read. Routes to IExec->FreeVec
 * since frame_read uses AllocVecTags (better behaviour than newlib
 * malloc for the multi-MiB transient allocations). */
void frame_free(char *buf);

/* Write one framed message of `len` bytes to `sock`.
 * Returns 0 on success, -1 on error. */
int frame_write(int sock, const char *buf, size_t len);

/* How long a connection may make no progress, reading or writing,
 * before it is dropped. Kept by frame_recv_some / frame_send_all with
 * WaitSelect, so it holds on every stack (SO_RCVTIMEO / SO_SNDTIMEO
 * are also set, but Roadshow 1.15 ignores them). */
#define MCPD_IO_TIMEOUT_S 120

/* recv() of up to `n` bytes once `sock` is readable: the count, 0 at
 * the end of the stream, -1 on error, a break, or MCPD_IO_TIMEOUT_S
 * of silence. Uses the calling task's bsdsocket interface. */
long frame_recv_some(int sock, void *buf, size_t n);

/* Send all `n` bytes: 0, or -1 on error, a break, or
 * MCPD_IO_TIMEOUT_S without progress (the peer stopped reading). */
int frame_send_all(int sock, const void *buf, size_t n);

#endif /* MCPD_FRAME_H */
