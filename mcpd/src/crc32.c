/* crc32.c - see crc32.h. Slicing-by-8 over the reflected polynomial
 * 0xEDB88320, written with byte loads so it is endian-neutral (the
 * PPC is big-endian; the usual slicing code assumes little-endian
 * word loads). The tables are built on first use. Two workers may
 * build them at once: both write the same values, and `ready` is set
 * only after a builder has finished, so a reader never sees a
 * half-built table from its own call. */

#include "crc32.h"

static uint32_t tab[8][256];
static volatile int ready;

static void build(void) {
    for (uint32_t i = 0; i < 256; i++) {
        uint32_t c = i;
        for (int k = 0; k < 8; k++)
            c = (c & 1) ? (c >> 1) ^ 0xEDB88320u : c >> 1;
        tab[0][i] = c;
    }
    for (uint32_t i = 0; i < 256; i++) {
        uint32_t c = tab[0][i];
        for (int t = 1; t < 8; t++) {
            c = (c >> 8) ^ tab[0][c & 0xff];
            tab[t][i] = c;
        }
    }
    __sync_synchronize();
    ready = 1;
}

uint32_t mcpd_crc32(uint32_t crc, const void *buf, size_t n) {
    if (!ready) build();
    const uint8_t *p = (const uint8_t *)buf;
    uint32_t c = ~crc;
    while (n >= 8) {
        uint32_t lo = c ^ ((uint32_t)p[0] | ((uint32_t)p[1] << 8)
                         | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24));
        c = tab[7][lo & 0xff] ^ tab[6][(lo >> 8) & 0xff]
          ^ tab[5][(lo >> 16) & 0xff] ^ tab[4][lo >> 24]
          ^ tab[3][p[4]] ^ tab[2][p[5]] ^ tab[1][p[6]] ^ tab[0][p[7]];
        p += 8;
        n -= 8;
    }
    while (n--) c = (c >> 8) ^ tab[0][(c ^ *p++) & 0xff];
    return ~c;
}
