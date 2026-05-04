/* b64.c - minimal base64 (RFC 4648) encode/decode.
 *
 * Whitespace tolerated on input. Encoder always emits canonical
 * padding. Used for fs.read / fs.write payloads on the wire.
 *
 * Memory: encoded/decoded buffers are allocated via IExec->AllocVecTags
 * MEMF_SHARED, NOT clib4 malloc. The buffers can be 32 MiB for big
 * fs.read/fs.write payloads, and clib4's heap fragments under repeated
 * multi-MiB churn (chunked-upload tests reproduce this on the third
 * sequential 4-10 MiB chunk). The OS allocator handles those large
 * blocks much better. Callers MUST release via b64_free, not free().
 */

#include "b64.h"

#include <stdlib.h>
#include <string.h>

#include <proto/exec.h>
#include <exec/memory.h>
#include <exec/exectags.h>

static const char enc_tbl[] =
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

static const signed char dec_tbl[256] = {
    /* fill at runtime via init? no - initialise here. */
    [0 ... 255] = -1,
    ['A']=0,['B']=1,['C']=2,['D']=3,['E']=4,['F']=5,['G']=6,['H']=7,
    ['I']=8,['J']=9,['K']=10,['L']=11,['M']=12,['N']=13,['O']=14,['P']=15,
    ['Q']=16,['R']=17,['S']=18,['T']=19,['U']=20,['V']=21,['W']=22,['X']=23,
    ['Y']=24,['Z']=25,
    ['a']=26,['b']=27,['c']=28,['d']=29,['e']=30,['f']=31,['g']=32,['h']=33,
    ['i']=34,['j']=35,['k']=36,['l']=37,['m']=38,['n']=39,['o']=40,['p']=41,
    ['q']=42,['r']=43,['s']=44,['t']=45,['u']=46,['v']=47,['w']=48,['x']=49,
    ['y']=50,['z']=51,
    ['0']=52,['1']=53,['2']=54,['3']=55,['4']=56,['5']=57,['6']=58,['7']=59,
    ['8']=60,['9']=61,
    ['+']=62,['/']=63,
};

char *b64_encode(const unsigned char *in, size_t n) {
    size_t out_n = ((n + 2) / 3) * 4;
    char *out = (char *)IExec->AllocVecTags((uint32)(out_n + 1),
                                            AVT_Clear, FALSE,
                                            TAG_END);
    if (!out) return NULL;
    char *p = out;
    size_t i = 0;
    while (i + 3 <= n) {
        unsigned int v = ((unsigned int)in[i] << 16)
                       | ((unsigned int)in[i+1] << 8)
                       |  (unsigned int)in[i+2];
        *p++ = enc_tbl[(v >> 18) & 0x3f];
        *p++ = enc_tbl[(v >> 12) & 0x3f];
        *p++ = enc_tbl[(v >>  6) & 0x3f];
        *p++ = enc_tbl[v & 0x3f];
        i += 3;
    }
    if (i < n) {
        unsigned int v = (unsigned int)in[i] << 16;
        if (i + 1 < n) v |= (unsigned int)in[i+1] << 8;
        *p++ = enc_tbl[(v >> 18) & 0x3f];
        *p++ = enc_tbl[(v >> 12) & 0x3f];
        if (i + 1 < n) {
            *p++ = enc_tbl[(v >> 6) & 0x3f];
            *p++ = '=';
        } else {
            *p++ = '=';
            *p++ = '=';
        }
    }
    *p = '\0';
    return out;
}

unsigned char *b64_decode(const char *in, size_t *out_len) {
    size_t n = strlen(in);
    /* upper bound */
    unsigned char *out = (unsigned char *)IExec->AllocVecTags((uint32)(n + 1),
                                                              AVT_Clear, FALSE,
                                                              TAG_END);
    if (!out) return NULL;
    unsigned char *p = out;

    int buf = 0;
    int bits = 0;
    for (size_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)in[i];
        if (c == '=' || c == ' ' || c == '\n' || c == '\r' || c == '\t') {
            if (c == '=') break;
            continue;
        }
        signed char d = dec_tbl[c];
        if (d < 0) {
            IExec->FreeVec(out);
            return NULL;
        }
        buf = (buf << 6) | d;
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            *p++ = (unsigned char)((buf >> bits) & 0xff);
        }
    }
    *out_len = (size_t)(p - out);
    return out;
}

void b64_free(void *buf) {
    if (buf) IExec->FreeVec(buf);
}
