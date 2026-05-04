/* sha256.h - tiny SHA-256 implementation (FIPS 180-4).
 *
 * Public-domain reference implementation - small, self-contained,
 * no external deps. Used for fs.hash with algo="sha256".
 *
 * Streaming API:
 *   sha256_init(&ctx);
 *   sha256_update(&ctx, data, len);   // call any number of times
 *   sha256_final(&ctx, digest);        // 32 bytes out
 */
#ifndef MCPD_SHA256_H
#define MCPD_SHA256_H

#include <stddef.h>
#include <stdint.h>

#define SHA256_DIGEST_BYTES 32

typedef struct {
    uint32_t state[8];
    uint64_t bitlen;
    uint8_t  buf[64];
    size_t   buflen;
} sha256_ctx;

void sha256_init(sha256_ctx *ctx);
void sha256_update(sha256_ctx *ctx, const void *data, size_t len);
void sha256_final(sha256_ctx *ctx, uint8_t out[32]);

#endif
