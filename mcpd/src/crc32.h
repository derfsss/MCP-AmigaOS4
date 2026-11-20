/* crc32.h - CRC-32 (IEEE 802.3, the zlib / PNG / Python zlib.crc32 one).
 *
 * Used to check raw streams end to end (fs.stream_status): TCP's own
 * checksum is computed by the network card when a driver offloads it,
 * so it no longer covers the bytes between the stack and the card.
 * Self-contained (no z.library): streams must be checkable on every
 * machine.
 */
#ifndef MCPD_CRC32_H
#define MCPD_CRC32_H

#include <stddef.h>
#include <stdint.h>

/* Continue `crc` over `n` bytes at `p`. Start with 0; the result of
 * one call is the `crc` of the next (as zlib's crc32()). */
uint32_t mcpd_crc32(uint32_t crc, const void *p, size_t n);

#endif /* MCPD_CRC32_H */
