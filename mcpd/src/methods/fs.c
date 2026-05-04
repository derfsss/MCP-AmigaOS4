/* fs.* method handlers.
 *
 * fs.list      - dos.library ExamineDir loop (AOS4 v50+ helper).
 * fs.stat      - ExamineObjectTags on Lock.
 * fs.read      - Open + Read, base64-encoded result.
 * fs.write     - Decode base64, Open MODE_NEWFILE + Write.
 * fs.delete    - DeleteFile.
 * fs.makedir   - CreateDir.
 */

#include "../b64.h"
#include "../rpc.h"
#include "../sha256.h"
#include "methods.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <proto/dos.h>
#include <proto/exec.h>
#include <proto/z.h>
#include <dos/dos.h>
#include <exec/memory.h>
#include <exec/exectags.h>
#include <libraries/z.h>

/* z.library interface - opened in main.c. Routing zlib through the
 * shared OS library avoids fragmenting clib4's heap with the multi-MiB
 * inflate buffers. NULL if the library wasn't available at startup;
 * compression="zlib" requests then return -32602. */
extern struct ZIFace *IZ;

/* Per-call ceiling on a single fs.read slice. Tracks the framing
 * cap in frame.h - the response envelope wrapping this payload must
 * fit in MCPD_FRAME_MAX_PAYLOAD. */
#define FS_READ_MAX_BYTES (32u * 1024u * 1024u)

/* Per-call ceiling on a decompressed fs.write/write_chunk payload.
 * Higher than FS_READ_MAX_BYTES because zlib compression can multiply
 * effective payload size; capped to avoid arbitrary single-allocation
 * spikes when a client sends a maliciously crafted high-ratio blob. */
#define FS_WRITE_DECOMPRESSED_MAX (128u * 1024u * 1024u)

/* Decode a zlib-compressed base64 string into raw bytes. Returns
 * malloc'd buffer + size on success; NULL on failure with err_msg
 * pointing to a static string. Caller must free the return.
 * `expected_size` may be 0 - if non-zero, the result must match
 * exactly (otherwise -> "size mismatch"). */
static unsigned char *_decompress_zlib(
    const unsigned char *compressed, size_t compressed_len,
    size_t expected_size, size_t *out_len, const char **err_msg)
{
    *err_msg = NULL;
    *out_len = 0;
    if (compressed_len == 0) {
        *err_msg = "empty compressed payload";
        return NULL;
    }
    if (!IZ) {
        *err_msg = "z.library not available - daemon needs v53+";
        return NULL;
    }

    /* If caller told us the size, allocate exactly + do a one-shot
     * Inflate. Faster + simpler than streaming. */
    if (expected_size > 0) {
        if (expected_size > FS_WRITE_DECOMPRESSED_MAX) {
            *err_msg = "raw_size exceeds decompressed cap";
            return NULL;
        }
        unsigned char *out = (unsigned char *)IExec->AllocVecTags(
            (uint32)(expected_size + 1),
            AVT_Clear, FALSE, TAG_END);
        if (!out) { *err_msg = "out of memory"; return NULL; }

        z_stream zs;
        memset(&zs, 0, sizeof(zs));
        zs.next_in = (const uint8 *)compressed;
        zs.avail_in = (uint32)compressed_len;
        zs.next_out = out;
        zs.avail_out = (uint32)expected_size;
        if (IZ->InflateInit(&zs) != Z_OK) {
            IExec->FreeVec(out);
            *err_msg = "InflateInit failed";
            return NULL;
        }
        int rc = IZ->Inflate(&zs, Z_FINISH);
        IZ->InflateEnd(&zs);
        if (rc != Z_STREAM_END) {
            IExec->FreeVec(out);
            *err_msg = (rc == Z_BUF_ERROR) ? "raw_size too small for payload"
                     : (rc == Z_DATA_ERROR) ? "zlib data error"
                     : "zlib Inflate failed";
            return NULL;
        }
        if ((size_t)zs.total_out != expected_size) {
            IExec->FreeVec(out);
            *err_msg = "decompressed size != raw_size";
            return NULL;
        }
        *out_len = (size_t)zs.total_out;
        return out;
    }

    /* No expected_size: stream-inflate into a growing buffer. Start
     * at 4x the compressed size; double when it fills. AllocVecTags
     * MEMF_SHARED throughout (NOT clib4 malloc) to keep the multi-MiB
     * transient out of clib4's fragmenting heap. */
    z_stream zs;
    memset(&zs, 0, sizeof(zs));
    if (IZ->InflateInit(&zs) != Z_OK) {
        *err_msg = "InflateInit failed";
        return NULL;
    }
    zs.next_in = (const uint8 *)compressed;
    zs.avail_in = (uint32)compressed_len;

    size_t cap = compressed_len * 4;
    if (cap < 4096) cap = 4096;
    unsigned char *buf = (unsigned char *)IExec->AllocVecTags(
        (uint32)cap, AVT_Clear, FALSE, TAG_END);
    if (!buf) { IZ->InflateEnd(&zs); *err_msg = "out of memory"; return NULL; }
    size_t total = 0;
    int rc;
    do {
        if (cap - total < 4096) {
            size_t new_cap = cap * 2;
            if (new_cap > FS_WRITE_DECOMPRESSED_MAX) {
                new_cap = FS_WRITE_DECOMPRESSED_MAX;
            }
            if (new_cap == cap) {
                IExec->FreeVec(buf);
                IZ->InflateEnd(&zs);
                *err_msg = "decompressed payload exceeds cap";
                return NULL;
            }
            /* No FreeVec realloc: alloc new + copy + free old. */
            unsigned char *nb = (unsigned char *)IExec->AllocVecTags(
                (uint32)new_cap, AVT_Clear, FALSE, TAG_END);
            if (!nb) {
                IExec->FreeVec(buf);
                IZ->InflateEnd(&zs);
                *err_msg = "out of memory (grow)";
                return NULL;
            }
            memcpy(nb, buf, total);
            IExec->FreeVec(buf);
            buf = nb;
            cap = new_cap;
        }
        zs.next_out = buf + total;
        zs.avail_out = (uint32)(cap - total);
        rc = IZ->Inflate(&zs, Z_NO_FLUSH);
        total = cap - zs.avail_out;
        if (rc == Z_STREAM_END) break;
        if (rc != Z_OK) {
            IExec->FreeVec(buf);
            IZ->InflateEnd(&zs);
            *err_msg = "zlib Inflate failed";
            return NULL;
        }
    } while (zs.avail_in > 0 || zs.avail_out == 0);
    IZ->InflateEnd(&zs);
    *out_len = total;
    return buf;
}

/* Decode a write payload from params. Honours `compression` ("none"
 * or "zlib") and `raw_size` (optional hint for zlib path). Returns
 * malloc'd raw bytes + size on success; NULL with *out_err set on
 * failure. */
static unsigned char *_decode_write_payload(
    cJSON *params, size_t *out_n, cJSON **out_err)
{
    const char *content_b64 = p_str(params, "content_b64", out_err);
    if (!content_b64) return NULL;

    cJSON *comp = cJSON_GetObjectItemCaseSensitive(params, "compression");
    int is_zlib = comp && cJSON_IsString(comp) &&
                  comp->valuestring &&
                  strcmp(comp->valuestring, "zlib") == 0;

    size_t b64_n = 0;
    unsigned char *decoded = b64_decode(content_b64, &b64_n);
    if (!decoded) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
                                  "content_b64 is not valid base64", NULL);
        return NULL;
    }

    if (!is_zlib) {
        *out_n = b64_n;
        return decoded;
    }

    /* zlib path: take optional raw_size hint and inflate. */
    long long raw_size = p_int(params, "raw_size", 0);
    if (raw_size < 0) raw_size = 0;
    size_t inflated_n = 0;
    const char *err_msg = NULL;
    unsigned char *inflated = _decompress_zlib(
        decoded, b64_n, (size_t)raw_size, &inflated_n, &err_msg);
    b64_free(decoded);
    if (!inflated) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            err_msg ? err_msg : "zlib decompression failed", NULL);
        return NULL;
    }
    *out_n = inflated_n;
    return inflated;
}

/* ---- helpers ---------------------------------------------------- */

/* Map AmigaOS IoErr() to a friendly message. Returns a static
 * string; caller does not free. */
static const char *ioerr_msg(LONG err) {
    switch (err) {
        case ERROR_OBJECT_NOT_FOUND:    return "Object not found";
        case ERROR_OBJECT_EXISTS:       return "Object already exists";
        case ERROR_OBJECT_IN_USE:       return "Object in use";
        case ERROR_OBJECT_WRONG_TYPE:   return "Object wrong type";
        case ERROR_DIR_NOT_FOUND:       return "Directory not found";
        case ERROR_INVALID_LOCK:        return "Invalid lock";
        case ERROR_NO_FREE_STORE:       return "No free memory";
        case ERROR_DISK_FULL:           return "Disk full";
        case ERROR_DELETE_PROTECTED:    return "Object is protected from deletion";
        case ERROR_WRITE_PROTECTED:     return "Disk is write-protected";
        case ERROR_DIRECTORY_NOT_EMPTY: return "Directory not empty";
        case 0:                         return "OK";
        default:                        return "I/O error";
    }
}

static cJSON *fs_error_for(const char *path) {
    LONG e = IDOS->IoErr();
    return target_error(ioerr_msg(e), path);
}

/* ---- fs.list ---------------------------------------------------- */

/* Emit one entry. `prefix` is the relative path from the original
 * root (empty string for top-level). Returns 0 on success.
 *
 * If `recursive` is set and the entry is a directory, recurses
 * into it - up to a hard depth cap so a malicious / cyclic tree
 * doesn't blow the stack. */
static int _list_emit(cJSON *entries, const char *prefix,
                      struct ExamineData *ed, int recursive,
                      int depth, int max_depth, BPTR parent_lock) {
    cJSON *e = cJSON_CreateObject();
    /* Build display name = prefix + ed->Name. */
    if (prefix && prefix[0]) {
        char rel[512];
        snprintf(rel, sizeof(rel), "%s/%s",
                 prefix, (const char *)ed->Name);
        cJSON_AddStringToObject(e, "name", rel);
    } else {
        cJSON_AddStringToObject(e, "name", (const char *)ed->Name);
    }
    cJSON_AddStringToObject(e, "type",
        EXD_IS_DIRECTORY(ed) ? "dir" : "file");
    if (!EXD_IS_DIRECTORY(ed)) {
        cJSON_AddNumberToObject(e, "size", (double)ed->FileSize);
    }
    cJSON_AddItemToArray(entries, e);

    if (recursive && EXD_IS_DIRECTORY(ed) && depth < max_depth) {
        /* Lock this subdirectory and recurse. ed->Name is the basename
         * relative to parent_lock, so we have to swap CurrentDir to
         * parent_lock for the duration of the Lock(). */
        BPTR saved_cd = IDOS->SetCurrentDir(parent_lock);
        BPTR sub = IDOS->Lock((const char *)ed->Name, SHARED_LOCK);
        IDOS->SetCurrentDir(saved_cd);
        if (sub == ZERO) {
            return 0;
        }
        APTR sctx = IDOS->ObtainDirContextTags(
            EX_FileLockInput, sub,
            EX_DataFields, EXF_NAME | EXF_TYPE | EXF_SIZE,
            TAG_END);
        if (sctx) {
            char child_prefix[512];
            if (prefix && prefix[0]) {
                snprintf(child_prefix, sizeof(child_prefix), "%s/%s",
                         prefix, (const char *)ed->Name);
            } else {
                snprintf(child_prefix, sizeof(child_prefix), "%s",
                         (const char *)ed->Name);
            }
            struct ExamineData *child;
            while ((child = IDOS->ExamineDir(sctx)) != NULL) {
                _list_emit(entries, child_prefix, child,
                           recursive, depth + 1, max_depth, sub);
            }
            IDOS->ReleaseDirContext(sctx);
        }
        IDOS->UnLock(sub);
    }
    return 0;
}


int fs_list(cJSON *params, cJSON **out_result, cJSON **out_err) {
    const char *path = p_str(params, "path", out_err);
    if (!path) return 0;

    cJSON *rec_node = cJSON_GetObjectItemCaseSensitive(params, "recursive");
    int recursive = cJSON_IsBool(rec_node) && cJSON_IsTrue(rec_node);

    long long max_depth_ll = p_int(params, "max_depth", 8);
    int max_depth = (int)max_depth_ll;
    if (max_depth < 1) max_depth = 1;
    if (max_depth > 16) max_depth = 16;

    BPTR lock = IDOS->Lock(path, SHARED_LOCK);
    if (lock == ZERO) {
        *out_err = fs_error_for(path);
        return 0;
    }

    /* Recursive walk needs CWD set so child Lock() with bare names
     * resolves relative to the current dir. We CurrentDir(lock) the
     * starting point and restore afterward. */
    BPTR prev_cd = (BPTR)0;
    if (recursive) {
        prev_cd = IDOS->SetCurrentDir(lock);
    }

    cJSON *entries = cJSON_CreateArray();

    APTR ctx = IDOS->ObtainDirContextTags(
        EX_FileLockInput, lock,
        EX_DataFields, EXF_NAME | EXF_TYPE | EXF_SIZE,
        TAG_END);
    if (!ctx) {
        if (recursive) IDOS->SetCurrentDir(prev_cd);
        IDOS->UnLock(lock);
        cJSON_Delete(entries);
        *out_err = fs_error_for(path);
        return 0;
    }

    struct ExamineData *ed;
    while ((ed = IDOS->ExamineDir(ctx)) != NULL) {
        _list_emit(entries, "", ed, recursive, 0, max_depth, lock);
    }
    IDOS->ReleaseDirContext(ctx);

    if (recursive) IDOS->SetCurrentDir(prev_cd);
    IDOS->UnLock(lock);

    *out_result = entries;
    return 0;
}

/* ---- fs.stat --------------------------------------------------- */

int fs_stat(cJSON *params, cJSON **out_result, cJSON **out_err) {
    const char *path = p_str(params, "path", out_err);
    if (!path) return 0;

    struct ExamineData *ed = IDOS->ExamineObjectTags(EX_StringNameInput, path,
                                                     TAG_END);
    if (!ed) {
        *out_err = fs_error_for(path);
        return 0;
    }

    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "name", (const char *)ed->Name);
    cJSON_AddStringToObject(r, "type",
        EXD_IS_DIRECTORY(ed) ? "dir" : "file");
    cJSON_AddNumberToObject(r, "size",
        EXD_IS_DIRECTORY(ed) ? 0.0 : (double)ed->FileSize);
    /* Date as ISO-ish "YYYY-MM-DD HH:MM:SS" via DateToStr. */
    {
        struct DateTime dt;
        char date_buf[32], time_buf[32];
        memset(&dt, 0, sizeof(dt));
        dt.dat_Stamp.ds_Days   = ed->Date.ds_Days;
        dt.dat_Stamp.ds_Minute = ed->Date.ds_Minute;
        dt.dat_Stamp.ds_Tick   = ed->Date.ds_Tick;
        dt.dat_Format = FORMAT_INT;  /* ISO-ish: YYYY-MM-DD */
        dt.dat_StrDate = date_buf;
        dt.dat_StrTime = time_buf;
        if (IDOS->DateToStr(&dt)) {
            char both[80];
            snprintf(both, sizeof(both), "%s %s", date_buf, time_buf);
            cJSON_AddStringToObject(r, "modified", both);
        }
    }

    IDOS->FreeDosObject(DOS_EXAMINEDATA, ed);
    *out_result = r;
    return 0;
}

/* ---- fs.read --------------------------------------------------- */

int fs_read(cJSON *params, cJSON **out_result, cJSON **out_err) {
    const char *path = p_str(params, "path", out_err);
    if (!path) return 0;

    /* Optional partial-read params (sec 7.2):
     *   offset - byte offset to start at (default 0)
     *   length - max bytes to read (default = full file - offset)
     * If specified, the result.size is the bytes actually read,
     * and the result includes total_size + offset for the caller's
     * benefit.
     */
    long long req_offset = p_int(params, "offset", 0);
    long long req_length = p_int(params, "length", -1);
    if (req_offset < 0) req_offset = 0;

    BPTR fh = IDOS->Open(path, MODE_OLDFILE);
    if (fh == ZERO) {
        *out_err = fs_error_for(path);
        return 0;
    }

    /* Find total size. */
    int64_t fsize = -1;
    {
        struct ExamineData *ed = IDOS->ExamineObjectTags(EX_FileHandleInput, fh,
                                                         TAG_END);
        if (ed) {
            fsize = (int64_t)ed->FileSize;
            IDOS->FreeDosObject(DOS_EXAMINEDATA, ed);
        }
    }
    if (fsize < 0) fsize = 0;

    /* Compute the slice we want. */
    int64_t avail = fsize - req_offset;
    if (avail < 0) avail = 0;
    int64_t to_read = avail;
    if (req_length >= 0 && req_length < to_read) {
        to_read = req_length;
    }

    if ((uint64_t)to_read > FS_READ_MAX_BYTES) {
        IDOS->Close(fh);
        cJSON *data = cJSON_CreateObject();
        if (data) {
            cJSON_AddStringToObject(data, "path", path);
            cJSON_AddNumberToObject(data, "requested", (double)to_read);
            cJSON_AddNumberToObject(data, "max", (double)FS_READ_MAX_BYTES);
        }
        *out_err = rpc_make_error(MCPD_ERR_TARGET,
                                  "fs.read slice exceeds 32 MiB cap", data);
        return 0;
    }

    /* Only seek when there's actually data to read at the requested
     * offset. ChangeFilePosition can refuse a seek past EOF on some
     * filesystems; if to_read is 0 we'd just be returning an empty
     * slice anyway, so skip the seek and let the empty-buffer path
     * below produce the right answer. */
    if (req_offset > 0 && to_read > 0) {
        /* DOS 4.x BOOL convention: DOSFALSE (0) = failure. Don't test
         * `< 0` - DOSTRUE is -1, which would look like an error. */
        if (!IDOS->ChangeFilePosition(fh, (int64_t)req_offset,
                                      OFFSET_BEGINNING)) {
            IDOS->Close(fh);
            *out_err = fs_error_for(path);
            return 0;
        }
    }

    unsigned char *buf = (unsigned char *)IExec->AllocVecTags(
        (uint32)((size_t)to_read + 1),
        AVT_Clear, FALSE, TAG_END);
    if (!buf) {
        IDOS->Close(fh);
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL, "out of memory", NULL);
        return 0;
    }
    LONG got = (to_read > 0)
        ? IDOS->Read(fh, buf, (LONG)to_read)
        : 0;
    IDOS->Close(fh);
    if (got < 0) {
        IExec->FreeVec(buf);
        *out_err = fs_error_for(path);
        return 0;
    }

    char *b64 = b64_encode(buf, (size_t)got);
    IExec->FreeVec(buf);
    if (!b64) {
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL, "base64 encode failed", NULL);
        return 0;
    }

    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "path", path);
    cJSON_AddNumberToObject(r, "size", (double)got);
    cJSON_AddNumberToObject(r, "offset", (double)req_offset);
    cJSON_AddNumberToObject(r, "total_size", (double)fsize);
    cJSON_AddStringToObject(r, "content_b64", b64);
    b64_free(b64);
    *out_result = r;
    return 0;
}

/* ---- fs.write -------------------------------------------------- */

int fs_write(cJSON *params, cJSON **out_result, cJSON **out_err) {
    const char *path = p_str(params, "path", out_err);
    if (!path) return 0;

    size_t n = 0;
    unsigned char *data = _decode_write_payload(params, &n, out_err);
    if (!data) return 0;  /* *out_err already set */

    BPTR fh = IDOS->Open(path, MODE_NEWFILE);
    if (fh == ZERO) {
        b64_free(data);
        *out_err = fs_error_for(path);
        return 0;
    }
    LONG wrote = (n == 0) ? 0 : IDOS->Write(fh, data, (LONG)n);
    IDOS->Close(fh);
    b64_free(data);
    if (wrote < 0 || (size_t)wrote != n) {
        *out_err = fs_error_for(path);
        return 0;
    }

    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "path", path);
    cJSON_AddNumberToObject(r, "size", (double)wrote);
    *out_result = r;
    return 0;
}

/* ---- fs.write_chunk -------------------------------------------- */
/*
 * Chunked / resumable upload primitive (option B in the planning
 * report). The host slices a large local file into <=24 MiB pieces
 * (so each chunk's base64 fits the 32 MiB JSON-RPC frame) and calls
 * fs.write_chunk repeatedly with monotonically increasing offsets.
 * Compression is per-chunk - the host can zlib each chunk before
 * base64 to push more bytes through each frame (option F).
 *
 * Params:
 *   path:        REQUIRED string. Destination file.
 *   offset:      REQUIRED int >= 0. Byte offset for this chunk.
 *   content_b64: REQUIRED string. Base64 of the (optionally
 *                zlib-compressed) chunk bytes.
 *   compression: optional "none" (default) | "zlib".
 *   raw_size:    optional int. Decompressed size hint when
 *                compression="zlib"; lets the daemon allocate
 *                exactly + verify size match.
 *   truncate:    optional bool. If true, truncate the file to
 *                exactly (offset + chunk_len) after writing this
 *                chunk. Default: true when offset==0, false otherwise
 *                - i.e. the first chunk acts like fs.write
 *                (truncating any pre-existing file), subsequent
 *                chunks just seek+write. Pass truncate=false on
 *                the first chunk to opt into "patch existing file
 *                in place".
 *   total_size:  optional int. Hint that lets the daemon pre-extend
 *                the destination (single SetFileSize) so that many
 *                small chunks don't repeatedly grow metadata. Pure
 *                hint - safe to omit.
 *
 * Returns: { path, offset, written, total_so_far }
 *   - written = bytes actually written for THIS chunk
 *   - total_so_far = file size after this chunk (offset + written)
 *
 * Resume semantics: a partial file left behind by a crashed upload
 * is intentionally NOT cleaned up. The host can resume by sending
 * subsequent chunks with the appropriate offsets.
 */
int fs_write_chunk(cJSON *params, cJSON **out_result, cJSON **out_err) {
    const char *path = p_str(params, "path", out_err);
    if (!path) return 0;
    cJSON *offset_node = cJSON_GetObjectItemCaseSensitive(params, "offset");
    if (!cJSON_IsNumber(offset_node)) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "missing required number param: offset", NULL);
        return 0;
    }
    long long req_offset = (long long)offset_node->valuedouble;
    if (req_offset < 0) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "offset must be >= 0", NULL);
        return 0;
    }

    /* Truncate default: true when offset==0, else false. */
    cJSON *trunc_node = cJSON_GetObjectItemCaseSensitive(params, "truncate");
    int do_truncate;
    if (cJSON_IsBool(trunc_node)) {
        do_truncate = cJSON_IsTrue(trunc_node);
    } else {
        do_truncate = (req_offset == 0);
    }

    long long total_size_hint = p_int(params, "total_size", -1);

    size_t n = 0;
    unsigned char *data = _decode_write_payload(params, &n, out_err);
    if (!data) return 0;

    /* Open MODE_NEWFILE if first chunk + truncate; MODE_READWRITE
     * otherwise (preserves existing bytes outside the chunk). */
    BPTR fh;
    if (req_offset == 0 && do_truncate) {
        fh = IDOS->Open(path, MODE_NEWFILE);
    } else {
        fh = IDOS->Open(path, MODE_READWRITE);
    }
    if (fh == ZERO) {
        b64_free(data);
        *out_err = fs_error_for(path);
        return 0;
    }

    /* total_size_hint pre-extend disabled until ChangeFileSize on a
     * fresh MODE_NEWFILE handle is verified safe on the FFS that
     * RAM:/T: actually use (early experiments hung the daemon -
     * suspect interaction with sfsfs/ramlib's lazy allocation). */
    (void)total_size_hint;

    if (req_offset > 0) {
        if (!IDOS->ChangeFilePosition(fh, (int64_t)req_offset,
                                      OFFSET_BEGINNING)) {
            IDOS->Close(fh);
            b64_free(data);
            *out_err = fs_error_for(path);
            return 0;
        }
    }

    LONG wrote = (n == 0) ? 0 : IDOS->Write(fh, data, (LONG)n);
    IDOS->Close(fh);
    b64_free(data);

    if (wrote < 0 || (size_t)wrote != n) {
        *out_err = fs_error_for(path);
        return 0;
    }
    /* Compute total_after analytically - no Examine probe needed:
     *   - first chunk truncates, so file size = wrote
     *   - subsequent chunks extend if write past EOF, else file
     *     keeps its prior size (which we don't know without Examine).
     * For chunked-upload we can give the caller offset + wrote, which
     * is the largest byte position we just wrote to. The host helper
     * tracks the running offset itself and doesn't need this. */
    int64_t total_after = (int64_t)req_offset + (int64_t)wrote;

    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "path", path);
    cJSON_AddNumberToObject(r, "offset", (double)req_offset);
    cJSON_AddNumberToObject(r, "written", (double)wrote);
    cJSON_AddNumberToObject(r, "total_so_far", (double)total_after);
    *out_result = r;
    return 0;
}

/* ---- fs.delete ------------------------------------------------- */

int fs_delete(cJSON *params, cJSON **out_result, cJSON **out_err) {
    const char *path = p_str(params, "path", out_err);
    if (!path) return 0;

    /* Optional `recursive` flag (sec 7.2). When set, shell out to
     * AmigaDOS `Delete <path> ALL` so we don't need to write our
     * own dir-tree walker (which would have to handle MultiAssign,
     * symlinks, etc.). Slightly slower but matches user
     * expectations + leverages all the AmigaDOS edge-case logic. */
    cJSON *rec_node = cJSON_GetObjectItemCaseSensitive(params, "recursive");
    int recursive = cJSON_IsBool(rec_node) && cJSON_IsTrue(rec_node);

    if (recursive) {
        char cmd[1024];
        int n = snprintf(cmd, sizeof(cmd),
                         "Delete \"%s\" ALL QUIET", path);
        if (n <= 0 || n >= (int)sizeof(cmd)) {
            *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
                                      "fs.delete: path too long", NULL);
            return 0;
        }
        int rc = 0;
        extern char *amiga_run_command(const char *, int *, double);
        char *out = amiga_run_command(cmd, &rc, 60.0);
        if (!out) {
            *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
                                      "fs.delete: capture failed", NULL);
            return 0;
        }
        if (rc != 0) {
            cJSON *data = cJSON_CreateObject();
            if (data) {
                cJSON_AddStringToObject(data, "path", path);
                cJSON_AddNumberToObject(data, "rc", (double)rc);
                cJSON_AddStringToObject(data, "output", out);
            }
            *out_err = rpc_make_error(MCPD_ERR_TARGET,
                "fs.delete recursive failed", data);
            free(out);
            return 0;
        }
        free(out);
    } else if (!IDOS->Delete(path)) {
        *out_err = fs_error_for(path);
        return 0;
    }

    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "path", path);
    cJSON_AddBoolToObject(r, "ok", cJSON_True);
    cJSON_AddBoolToObject(r, "recursive",
        recursive ? cJSON_True : cJSON_False);
    *out_result = r;
    return 0;
}

/* ---- fs.hash --------------------------------------------------- */

int fs_hash(cJSON *params, cJSON **out_result, cJSON **out_err) {
    const char *path = p_str(params, "path", out_err);
    if (!path) return 0;
    /* algo defaults to "sha256". md5/sha1 not implemented yet -
     * SHA-256 covers the integrity-check use case. */
    cJSON *algo_node = cJSON_GetObjectItemCaseSensitive(params, "algo");
    const char *algo = "sha256";
    if (cJSON_IsString(algo_node) && algo_node->valuestring) {
        algo = algo_node->valuestring;
    }
    if (strcmp(algo, "sha256") != 0) {
        cJSON *data = cJSON_CreateObject();
        if (data) {
            cJSON_AddStringToObject(data, "requested", algo);
            cJSON_AddStringToObject(data, "supported", "sha256");
        }
        *out_err = rpc_make_error(MCPD_ERR_NOTCAPABLE,
            "fs.hash supports only algo=sha256 currently", data);
        return 0;
    }

    BPTR fh = IDOS->Open(path, MODE_OLDFILE);
    if (fh == ZERO) {
        *out_err = fs_error_for(path);
        return 0;
    }

    sha256_ctx ctx;
    sha256_init(&ctx);

    enum { CHUNK = 32768 };
    unsigned char *buf = (unsigned char *)malloc(CHUNK);
    if (!buf) {
        IDOS->Close(fh);
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL, "out of memory", NULL);
        return 0;
    }
    uint64_t total = 0;
    for (;;) {
        LONG got = IDOS->Read(fh, buf, CHUNK);
        if (got < 0) {
            free(buf);
            IDOS->Close(fh);
            *out_err = fs_error_for(path);
            return 0;
        }
        if (got == 0) break;
        sha256_update(&ctx, buf, (size_t)got);
        total += (uint64_t)got;
    }
    free(buf);
    IDOS->Close(fh);

    uint8_t digest[SHA256_DIGEST_BYTES];
    sha256_final(&ctx, digest);

    char hex[SHA256_DIGEST_BYTES * 2 + 1];
    static const char *hexd = "0123456789abcdef";
    for (int i = 0; i < SHA256_DIGEST_BYTES; i++) {
        hex[i*2]   = hexd[(digest[i] >> 4) & 0xf];
        hex[i*2+1] = hexd[digest[i] & 0xf];
    }
    hex[sizeof(hex) - 1] = '\0';

    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "path", path);
    cJSON_AddStringToObject(r, "algo", "sha256");
    cJSON_AddStringToObject(r, "hash", hex);
    cJSON_AddNumberToObject(r, "size", (double)total);
    *out_result = r;
    return 0;
}

/* ---- fs.makedir ------------------------------------------------ */

int fs_makedir(cJSON *params, cJSON **out_result, cJSON **out_err) {
    const char *path = p_str(params, "path", out_err);
    if (!path) return 0;
    BPTR lock = IDOS->CreateDir(path);
    if (lock == ZERO) {
        *out_err = fs_error_for(path);
        return 0;
    }
    IDOS->UnLock(lock);
    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "path", path);
    cJSON_AddBoolToObject(r, "ok", cJSON_True);
    *out_result = r;
    return 0;
}

/* ---- fs.rename ------------------------------------------------- */

int fs_rename(cJSON *params, cJSON **out_result, cJSON **out_err) {
    const char *src = p_str(params, "src", out_err);
    if (!src) return 0;
    const char *dst = p_str(params, "dst", out_err);
    if (!dst) return 0;
    if (!IDOS->Rename(src, dst)) {
        *out_err = fs_error_for(src);
        return 0;
    }
    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "src", src);
    cJSON_AddStringToObject(r, "dst", dst);
    cJSON_AddBoolToObject(r, "ok", cJSON_True);
    *out_result = r;
    return 0;
}

/* ---- fs.protect ------------------------------------------------ */

/* Protection bits per dos/dos.h:
 *   FIBF_DELETE | FIBF_EXECUTE | FIBF_WRITE | FIBF_READ (low nibble:
 *   in AmigaOS these are *negation* bits - 0 means allowed, 1 means
 *   denied). FIBF_ARCHIVE | FIBF_PURE | FIBF_SCRIPT | FIBF_HIDDEN
 *   (high nibble: 1 means set, 0 means unset). The standard `Protect
 *   path +rwed` shape gets translated by the AmigaDOS Protect command
 *   into a 32-bit bitmask that we accept directly here.
 */
int fs_protect(cJSON *params, cJSON **out_result, cJSON **out_err) {
    const char *path = p_str(params, "path", out_err);
    if (!path) return 0;
    /* `bits` is the raw integer bitmask. */
    long long bits = p_int(params, "bits", -1);
    if (bits < 0) {
        cJSON *data = cJSON_CreateObject();
        if (data) cJSON_AddStringToObject(data, "missing", "bits");
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
                                  "missing required integer param 'bits'",
                                  data);
        return 0;
    }
    if (!IDOS->SetProtection(path, (uint32_t)bits)) {
        *out_err = fs_error_for(path);
        return 0;
    }
    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "path", path);
    cJSON_AddNumberToObject(r, "bits", (double)(unsigned int)(uint32_t)bits);
    cJSON_AddBoolToObject(r, "ok", cJSON_True);
    *out_result = r;
    return 0;
}

/* ---- fs.copy --------------------------------------------------- */

/* AmigaDOS doesn't have a single dos.library Copy() that we can call
 * directly with the right semantics (CLONE preserves protection +
 * date), so we shell out to `Copy <src> TO <dst> CLONE` via
 * SystemTagList - same pattern as exec.cmd / sys.version. The capture
 * is mostly empty on success; non-empty stdout indicates an error.
 */
int fs_copy(cJSON *params, cJSON **out_result, cJSON **out_err) {
    const char *src = p_str(params, "src", out_err);
    if (!src) return 0;
    const char *dst = p_str(params, "dst", out_err);
    if (!dst) return 0;

    char cmd[1024];
    /* Quote both paths in case of spaces. */
    int n = snprintf(cmd, sizeof(cmd),
                     "Copy \"%s\" TO \"%s\" CLONE", src, dst);
    if (n <= 0 || n >= (int)sizeof(cmd)) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
                                  "fs.copy: paths too long", NULL);
        return 0;
    }

    int rc = 0;
    /* Forward declaration to amigautil's run-cmd-capture. */
    extern char *amiga_run_command(const char *cmd, int *exit_code,
                                   double timeout_s);
    char *out = amiga_run_command(cmd, &rc, 30.0);
    if (!out) {
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
                                  "fs.copy: capture failed", NULL);
        return 0;
    }
    /* AmigaDOS Copy on success prints "<src> ... copied to <dst>"
     * sometimes; on failure it prints an error. Treat non-zero rc
     * as failure.
     */
    if (rc != 0) {
        cJSON *data = cJSON_CreateObject();
        if (data) {
            cJSON_AddStringToObject(data, "src", src);
            cJSON_AddStringToObject(data, "dst", dst);
            cJSON_AddNumberToObject(data, "rc", (double)rc);
            cJSON_AddStringToObject(data, "output", out);
        }
        *out_err = rpc_make_error(MCPD_ERR_TARGET,
                                  "fs.copy: AmigaDOS Copy failed", data);
        free(out);
        return 0;
    }
    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "src", src);
    cJSON_AddStringToObject(r, "dst", dst);
    cJSON_AddStringToObject(r, "output", out);
    cJSON_AddBoolToObject(r, "ok", cJSON_True);
    free(out);
    *out_result = r;
    return 0;
}
