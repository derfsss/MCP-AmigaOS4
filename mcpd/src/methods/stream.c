/* fs.stream_get / fs.stream_put - raw bulk transfer on the connection.
 *
 * fs.read / fs.write_chunk carry a file in base64 inside JSON frames,
 * one round trip per chunk (at most 24-32 MiB each). For files of
 * hundreds of GiB that costs a third more bytes on the wire, an
 * encode and a decode pass, and an idle round trip per chunk. These
 * two methods answer with an ordinary JSON-RPC response and then move
 * the bytes raw on the same connection:
 *
 *   fs.stream_get {path, offset=0, length=-1}
 *     -> {path, offset, length, total_size}, then exactly `length`
 *        raw bytes from the file, starting at `offset`.
 *   fs.stream_put {path, offset=0, length}
 *     -> {path, offset, length, ready:true}; the client then sends
 *        exactly `length` raw bytes, written at `offset`. offset 0
 *        creates (or replaces) the file; a larger offset resumes and
 *        must equal the file's current size.
 *
 * Offsets and lengths are 64-bit (JSON numbers are exact to 2^53).
 * Everything that can be checked before the bytes flow (path, offset,
 * open) comes back as a normal JSON-RPC error. A failure while they
 * flow (read or write error, the peer stalling past SO_RCVTIMEO)
 * drops the connection: nothing else can tell the peer where the
 * stream stopped. The client then resumes from fs.stat's size (put)
 * or its own file's size (get); a put is confirmed by fs.stat on the
 * same connection once the bytes are sent (the file is closed before
 * that request is read).
 */

#include "../conn_ctx.h"
#include "../rpc.h"
#include "methods.h"

#include <stdint.h>
#include <string.h>

#include <proto/bsdsocket.h>
#include <proto/dos.h>
#include <proto/exec.h>
#include <dos/dos.h>
#include <exec/exectags.h>
#include <exec/memory.h>

/* One DOS call and one buffer per MiB: disk calls of 1 MiB run at
 * hundreds of MB/s on the QEMU guest, far above the network. */
#define STREAM_BUF (1024u * 1024u)

static cJSON *stream_dos_error(const char *msg, const char *path) {
    LONG e = IDOS->IoErr();
    cJSON *data = cJSON_CreateObject();
    if (data) {
        cJSON_AddStringToObject(data, "path", path);
        cJSON_AddNumberToObject(data, "ioerr", (double)e);
    }
    return rpc_make_error(MCPD_ERR_TARGET, msg, data);
}

static int64_t file_size(BPTR fh) {
    int64_t size = -1;
    struct ExamineData *ed = IDOS->ExamineObjectTags(EX_FileHandleInput, fh,
                                                     TAG_END);
    if (ed) {
        size = (int64_t)ed->FileSize;
        IDOS->FreeDosObject(DOS_EXAMINEDATA, ed);
    }
    return size;
}

static struct conn_ctx *stream_ctx(cJSON **out_err) {
    struct conn_ctx *ctx = mcpd_conn_ctx();
    if (!ctx || !ctx->isocket) {
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
            "streams run only on a connection worker", NULL);
        return NULL;
    }
    if (ctx->stream.fh != ZERO) {
        *out_err = rpc_make_error(MCPD_ERR_BUSY,
            "a stream is already set up on this connection", NULL);
        return NULL;
    }
    return ctx;
}

/* ---- fs.stream_get --------------------------------------------- */

int fs_stream_get(cJSON *params, cJSON **out_result, cJSON **out_err) {
    const char *path = p_str(params, "path", out_err);
    if (!path) return 0;
    long long offset = p_int(params, "offset", 0);
    long long length = p_int(params, "length", -1);
    if (offset < 0) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS, "offset must be >= 0", NULL);
        return 0;
    }
    struct conn_ctx *ctx = stream_ctx(out_err);
    if (!ctx) return 0;

    BPTR fh = IDOS->Open(path, MODE_OLDFILE);
    if (fh == ZERO) {
        *out_err = stream_dos_error("cannot open the file", path);
        return 0;
    }
    int64_t size = file_size(fh);
    if (size < 0) {
        *out_err = stream_dos_error("cannot examine the file", path);
        IDOS->Close(fh);
        return 0;
    }
    if (offset > size) {
        IDOS->Close(fh);
        cJSON *data = cJSON_CreateObject();
        if (data) {
            cJSON_AddStringToObject(data, "path", path);
            cJSON_AddNumberToObject(data, "offset", (double)offset);
            cJSON_AddNumberToObject(data, "total_size", (double)size);
        }
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS, "offset is past the end of the file", data);
        return 0;
    }
    int64_t n = size - offset;
    if (length >= 0 && length < n) n = length;
    if (offset > 0 && n > 0 &&
        !IDOS->ChangeFilePosition(fh, (int64_t)offset, OFFSET_BEGINNING)) {
        *out_err = stream_dos_error("cannot seek", path);
        IDOS->Close(fh);
        return 0;
    }

    cJSON *r = cJSON_CreateObject();
    if (!r) {
        IDOS->Close(fh);
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL, "out of memory", NULL);
        return 0;
    }
    cJSON_AddStringToObject(r, "path", path);
    cJSON_AddNumberToObject(r, "offset", (double)offset);
    cJSON_AddNumberToObject(r, "length", (double)n);
    cJSON_AddNumberToObject(r, "total_size", (double)size);
    ctx->stream.fh = fh;
    ctx->stream.dir = MCPD_STREAM_GET;
    ctx->stream.remaining = n;
    *out_result = r;
    return 0;
}

/* ---- fs.stream_put --------------------------------------------- */

int fs_stream_put(cJSON *params, cJSON **out_result, cJSON **out_err) {
    const char *path = p_str(params, "path", out_err);
    if (!path) return 0;
    long long offset = p_int(params, "offset", 0);
    long long length = p_int(params, "length", -1);
    if (offset < 0 || length < 0) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "offset must be >= 0 and length (required) >= 0", NULL);
        return 0;
    }
    struct conn_ctx *ctx = stream_ctx(out_err);
    if (!ctx) return 0;

    BPTR fh;
    if (offset == 0) {
        fh = IDOS->Open(path, MODE_NEWFILE);
        if (fh == ZERO) {
            *out_err = stream_dos_error("cannot create the file", path);
            return 0;
        }
    } else {
        fh = IDOS->Open(path, MODE_READWRITE);
        if (fh == ZERO) {
            *out_err = stream_dos_error("cannot open the file to resume", path);
            return 0;
        }
        /* Resume only at the end: nothing is truncated (ChangeFileSize
         * is avoided, see fs.write_chunk) and no gap can be left. */
        int64_t size = file_size(fh);
        if (size != offset) {
            IDOS->Close(fh);
            cJSON *data = cJSON_CreateObject();
            if (data) {
                cJSON_AddStringToObject(data, "path", path);
                cJSON_AddNumberToObject(data, "offset", (double)offset);
                cJSON_AddNumberToObject(data, "size", (double)size);
            }
            *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
                "a resumed put must start at the file's current size", data);
            return 0;
        }
        if (!IDOS->ChangeFilePosition(fh, (int64_t)offset, OFFSET_BEGINNING)) {
            *out_err = stream_dos_error("cannot seek", path);
            IDOS->Close(fh);
            return 0;
        }
    }

    cJSON *r = cJSON_CreateObject();
    if (!r) {
        IDOS->Close(fh);
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL, "out of memory", NULL);
        return 0;
    }
    cJSON_AddStringToObject(r, "path", path);
    cJSON_AddNumberToObject(r, "offset", (double)offset);
    cJSON_AddNumberToObject(r, "length", (double)length);
    cJSON_AddBoolToObject(r, "ready", 1);
    ctx->stream.fh = fh;
    ctx->stream.dir = MCPD_STREAM_PUT;
    ctx->stream.remaining = length;
    *out_result = r;
    return 0;
}

/* ---- running a stream (from handle_connection) ------------------ */

void mcpd_stream_cancel(struct conn_ctx *ctx) {
    if (ctx->stream.fh != ZERO) {
        IDOS->Close(ctx->stream.fh);
    }
    ctx->stream.fh = ZERO;
    ctx->stream.dir = 0;
    ctx->stream.remaining = 0;
}

static int send_all(struct conn_ctx *ctx, const char *p, uint32 n) {
    while (n > 0) {
        long sent = ctx->isocket->send(ctx->fd, (APTR)p, n, 0);
        if (sent <= 0) return -1;
        p += sent;
        n -= (uint32)sent;
    }
    return 0;
}

int mcpd_stream_run(struct conn_ctx *ctx) {
    if (ctx->stream.fh == ZERO) return 0;
    int dir = ctx->stream.dir;
    long long rem = ctx->stream.remaining;
    int rc = 0;

    char *buf = NULL;
    if (rem > 0) {
        buf = (char *)IExec->AllocVecTags(STREAM_BUF,
                                          AVT_Type, MEMF_SHARED,
                                          TAG_END);
        if (!buf) rc = -1;
    }

    while (rc == 0 && rem > 0) {
        uint32 want = rem < (long long)STREAM_BUF ? (uint32)rem : STREAM_BUF;
        if (dir == MCPD_STREAM_GET) {
            LONG got = IDOS->Read(ctx->stream.fh, buf, (LONG)want);
            /* A short read before the promised length (the file
             * shrank, or a read error) cannot be told to the peer. */
            if (got <= 0 || send_all(ctx, buf, (uint32)got) != 0) {
                rc = -1;
                break;
            }
            rem -= got;
        } else {
            /* Fill the buffer, then write it in one DOS call. */
            uint32 have = 0;
            while (have < want) {
                long got = ctx->isocket->recv(ctx->fd, buf + have, want - have, 0);
                if (got <= 0) {
                    rc = -1;
                    break;
                }
                have += (uint32)got;
            }
            if (have > 0 && IDOS->Write(ctx->stream.fh, buf, (LONG)have) != (LONG)have) {
                rc = -1;
            }
            rem -= have;
        }
    }

    if (buf) IExec->FreeVec(buf);
    /* Close before the next request is read, so the client's fs.stat
     * on this connection sees the whole file. */
    mcpd_stream_cancel(ctx);
    return rc;
}
