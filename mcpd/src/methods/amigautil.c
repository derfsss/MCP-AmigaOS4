/* amigautil.c - see amigautil.h. */

#include "amigautil.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <proto/dos.h>
#include <proto/exec.h>
#include <dos/dos.h>

/* Maximum stdout we capture per command (excess is truncated). 1 MiB. */
#define AMIGA_CAP_MAX (1u << 20)

char *amiga_run_command(const char *cmd, int *exit_code, double timeout_s) {
    (void)timeout_s;

    char tmpname[64];
    /* Unique enough per task. */
    snprintf(tmpname, sizeof(tmpname), "T:mcpd_%08lx_out",
             (unsigned long)IExec->FindTask(NULL));

    BPTR out = IDOS->Open(tmpname, MODE_NEWFILE);
    if (out == ZERO) {
        if (exit_code) *exit_code = -1;
        return NULL;
    }

    LONG rc = IDOS->SystemTags(cmd,
                               SYS_Output,    (Tag)out,
                               SYS_Input,     (Tag)ZERO,
                               SYS_Asynch,    FALSE,
                               NP_StackSize,  65536,
                               TAG_DONE);
    IDOS->Close(out);
    if (exit_code) *exit_code = (int)rc;

    /* Read the captured output. */
    BPTR in = IDOS->Open(tmpname, MODE_OLDFILE);
    if (in == ZERO) {
        IDOS->Delete(tmpname);
        char *empty = (char *)malloc(1);
        if (empty) empty[0] = '\0';
        return empty;
    }

    /* Determine size. Examine the FileInfoBlock. */
    struct ExamineData *ed = IDOS->ExamineObjectTags(EX_FileHandleInput, in, TAG_END);
    LONG size = 0;
    if (ed) {
        size = (LONG)ed->FileSize;
        IDOS->FreeDosObject(DOS_EXAMINEDATA, ed);
    }

    if (size <= 0) {
        IDOS->Close(in);
        IDOS->Delete(tmpname);
        char *empty = (char *)malloc(1);
        if (empty) empty[0] = '\0';
        return empty;
    }

    if ((unsigned long)size > (unsigned long)AMIGA_CAP_MAX) size = (LONG)AMIGA_CAP_MAX;

    char *buf = (char *)malloc((size_t)size + 1);
    if (!buf) {
        IDOS->Close(in);
        IDOS->Delete(tmpname);
        return NULL;
    }
    LONG got = IDOS->Read(in, buf, size);
    IDOS->Close(in);
    IDOS->Delete(tmpname);
    if (got < 0) got = 0;
    buf[got] = '\0';

    /* AmigaOS commands produce output in ISO-8859-1 (or raw bytes
     * for things like C:DumpDebugBuffer). cJSON requires valid
     * UTF-8, so we re-encode every byte: ASCII (< 0x80) passes
     * through untouched; bytes 0x80..0xFF are treated as Latin-1
     * codepoints and re-emitted as their 2-byte UTF-8 sequence.
     * The buffer can at most double; allocate the worst-case size
     * and copy through.
     *
     * (utility.library v54 has UCS4toUTF8 / UTF8Encode for the
     * general case but Latin-1 -> UTF-8 is mechanical enough that
     * an inline loop avoids the IUtility dependency on this hot
     * path.) */
    char *utf8 = (char *)malloc((size_t)got * 2 + 1);
    if (!utf8) {
        /* Fall back to ASCII-only sanitisation in-place. */
        for (LONG i = 0; i < got; i++) {
            if ((unsigned char)buf[i] >= 0x80) buf[i] = '?';
        }
        return buf;
    }
    size_t out_n = 0;
    for (LONG i = 0; i < got; i++) {
        unsigned char c = (unsigned char)buf[i];
        if (c < 0x80) {
            utf8[out_n++] = (char)c;
        } else {
            /* U+0080..U+00FF -> two-byte UTF-8: 110xxxxx 10xxxxxx */
            utf8[out_n++] = (char)(0xC0 | (c >> 6));
            utf8[out_n++] = (char)(0x80 | (c & 0x3F));
        }
    }
    utf8[out_n] = '\0';
    free(buf);
    return utf8;
}


/* Latin-1 -> UTF-8 transcoder for streaming chunks. Returns malloc'd
 * NUL-terminated buffer on success; ASCII-clean fallback on OOM. */
static char *_latin1_to_utf8_chunk(const char *src, size_t n,
                                   size_t *out_n_p) {
    char *utf8 = (char *)malloc(n * 2 + 1);
    if (!utf8) {
        char *ascii = (char *)malloc(n + 1);
        if (!ascii) return NULL;
        for (size_t i = 0; i < n; i++) {
            unsigned char c = (unsigned char)src[i];
            ascii[i] = (c < 0x80) ? (char)c : '?';
        }
        ascii[n] = '\0';
        if (out_n_p) *out_n_p = n;
        return ascii;
    }
    size_t out_n = 0;
    for (size_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)src[i];
        if (c < 0x80) {
            utf8[out_n++] = (char)c;
        } else {
            utf8[out_n++] = (char)(0xC0 | (c >> 6));
            utf8[out_n++] = (char)(0x80 | (c & 0x3F));
        }
    }
    utf8[out_n] = '\0';
    if (out_n_p) *out_n_p = out_n;
    return utf8;
}


char *amiga_run_command_streaming(
    const char *cmd, int *exit_code, double timeout_s,
    int poll_ms,
    void (*on_chunk)(const char *bytes, size_t n, void *user),
    void *user)
{
    /* SystemTags(SYS_Asynch=TRUE) returns immediately and the child
     * runs in its own Process; we poll the redirected output file as
     * it grows and stream chunks via on_chunk. After the child
     * finishes (SystemTags can use a child-finished signal), we
     * read whatever's left and return the full capture.
     *
     * To know when the child exits, use a one-shot signal: alloc a
     * signal bit, pass NP_NotifyOnDeath via the SystemTags wrapping
     * (actually NP_NotifyOnDeath isn't a SystemTags tag - the
     * portable way is to spawn via CreateNewProcTags directly and
     * pass NP_FinalCode). For simplicity here we shell SystemTags
     * with SYS_Asynch=FALSE inside a worker process the daemon
     * spawns explicitly. But that doubles the spawn overhead.
     *
     * Pragmatic v1: SystemTags(SYS_Asynch=TRUE) and watch for the
     * temp file to stop growing for `idle_threshold_ms`. Crude but
     * works for any AmigaDOS command. Final exit code lives in the
     * SystemTags return; in async mode AOS gives us a kick-off
     * status, NOT the child's RC. We re-read RC by reading a
     * marker file the child writes on exit (>VAL: $RC magic).
     */

    /* Build a wrapper that runs the command then writes RC to a
     * marker file. AmigaDOS shell `echo $$RC > T:mcpd_rc_<task>` */
    char rc_marker[64];
    snprintf(rc_marker, sizeof(rc_marker), "T:mcpd_%08lx_rc",
             (unsigned long)IExec->FindTask(NULL));

    char tmpname[64];
    snprintf(tmpname, sizeof(tmpname), "T:mcpd_%08lx_out",
             (unsigned long)IExec->FindTask(NULL));

    /* Pre-clean from any previous run. */
    IDOS->Delete(rc_marker);
    IDOS->Delete(tmpname);

    /* Wrap the command: 'CMD ; echo RC=$RC >T:rc_marker' so we can
     * recover the exit code despite SYS_Asynch losing it. */
    size_t wrap_len = strlen(cmd) + strlen(rc_marker) + 64;
    char *wrap = (char *)malloc(wrap_len);
    if (!wrap) {
        if (exit_code) *exit_code = -1;
        return NULL;
    }
    snprintf(wrap, wrap_len,
             "%s\nEcho >\"%s\" \"$RC\"\n",
             cmd, rc_marker);

    BPTR out_fh = IDOS->Open(tmpname, MODE_NEWFILE);
    if (out_fh == ZERO) {
        free(wrap);
        if (exit_code) *exit_code = -1;
        return NULL;
    }

    /* SYS_Asynch: TRUE means SystemTagList returns immediately, the
     * child runs concurrently. SYS_Output is closed by the child
     * when done.  */
    LONG kick = IDOS->SystemTags(wrap,
                                 SYS_Output,    (Tag)out_fh,
                                 SYS_Input,     (Tag)ZERO,
                                 SYS_Asynch,    TRUE,
                                 NP_StackSize,  65536,
                                 TAG_DONE);
    free(wrap);
    if (kick != 0 && kick != -1) {
        /* SystemTags failed to launch (RC -1 means "is async, kicked
         * off"; non-zero non-(-1) is a launch failure). */
        IDOS->Close(out_fh);
        IDOS->Delete(tmpname);
        if (exit_code) *exit_code = (int)kick;
        return NULL;
    }
    /* From here on out_fh is owned by the child - don't Close it. */

    /* Poll the temp file for new bytes. We read by re-Open'ing in
     * shared MODE_OLDFILE (the child has it MODE_NEWFILE which on
     * AOS4 is shared-readable). When the child writes the RC
     * marker, it's exited. */
    size_t total_read = 0;
    if (poll_ms < 50) poll_ms = 50;
    if (poll_ms > 1000) poll_ms = 1000;
    long ticks = (poll_ms * 50 + 999) / 1000;
    if (ticks < 1) ticks = 1;

    long long elapsed_ms = 0;
    long long timeout_ms_ll = (long long)(timeout_s * 1000.0);
    if (timeout_ms_ll <= 0) timeout_ms_ll = 30000;

    int child_done = 0;
    while (!child_done) {
        IDOS->Delay(ticks);
        elapsed_ms += poll_ms;
        if (elapsed_ms > timeout_ms_ll) break;

        /* Did the child finish? Existence of rc_marker means yes. */
        BPTR rc_lock = IDOS->Lock(rc_marker, SHARED_LOCK);
        if (rc_lock != ZERO) {
            IDOS->UnLock(rc_lock);
            child_done = 1;
        }

        /* Read any new bytes from the output file. */
        BPTR in = IDOS->Open(tmpname, MODE_OLDFILE);
        if (in != ZERO) {
            struct ExamineData *ed = IDOS->ExamineObjectTags(
                EX_FileHandleInput, in, TAG_END);
            int64_t fsize = 0;
            if (ed) {
                fsize = (int64_t)ed->FileSize;
                IDOS->FreeDosObject(DOS_EXAMINEDATA, ed);
            }
            if ((size_t)fsize > total_read) {
                size_t chunk_n = (size_t)fsize - total_read;
                if (chunk_n > 8192) chunk_n = 8192;
                if (total_read > 0) {
                    IDOS->ChangeFilePosition(in, (int64_t)total_read,
                                             OFFSET_BEGINNING);
                }
                char *chunk = (char *)malloc(chunk_n);
                if (chunk) {
                    LONG got = IDOS->Read(in, chunk, (LONG)chunk_n);
                    if (got > 0 && on_chunk) {
                        size_t utf8_n = 0;
                        char *u = _latin1_to_utf8_chunk(
                            chunk, (size_t)got, &utf8_n);
                        if (u) {
                            on_chunk(u, utf8_n, user);
                            free(u);
                        }
                    }
                    if (got > 0) total_read += (size_t)got;
                    free(chunk);
                }
            }
            IDOS->Close(in);
        }
    }

    /* Final read - drain any bytes between the last poll and now. */
    BPTR in = IDOS->Open(tmpname, MODE_OLDFILE);
    char *full = NULL;
    size_t full_n = 0;
    if (in != ZERO) {
        struct ExamineData *ed = IDOS->ExamineObjectTags(
            EX_FileHandleInput, in, TAG_END);
        int64_t fsize = 0;
        if (ed) {
            fsize = (int64_t)ed->FileSize;
            IDOS->FreeDosObject(DOS_EXAMINEDATA, ed);
        }
        if (fsize > 0 && (size_t)fsize <= AMIGA_CAP_MAX) {
            full = (char *)malloc((size_t)fsize + 1);
            if (full) {
                LONG got = IDOS->Read(in, full, (LONG)fsize);
                if (got < 0) got = 0;
                full[got] = '\0';
                full_n = (size_t)got;

                /* Emit any tail bytes the poll loop missed. */
                if (full_n > total_read && on_chunk) {
                    size_t utf8_n = 0;
                    char *u = _latin1_to_utf8_chunk(
                        full + total_read, full_n - total_read, &utf8_n);
                    if (u) {
                        on_chunk(u, utf8_n, user);
                        free(u);
                    }
                }
            }
        } else if (fsize == 0) {
            full = (char *)malloc(1);
            if (full) full[0] = '\0';
        }
        IDOS->Close(in);
    }
    IDOS->Delete(tmpname);

    /* Final "done" callback so the consumer can flush state. */
    if (on_chunk) on_chunk(NULL, 0, user);

    /* Recover RC from the marker file if it exists. */
    int rc = 0;
    BPTR rfh = IDOS->Open(rc_marker, MODE_OLDFILE);
    if (rfh != ZERO) {
        char rcbuf[16] = {0};
        LONG g = IDOS->Read(rfh, rcbuf, sizeof(rcbuf) - 1);
        IDOS->Close(rfh);
        if (g > 0) rc = atoi(rcbuf);
    } else {
        /* No marker - timed out or child crashed. */
        rc = -1;
    }
    IDOS->Delete(rc_marker);
    if (exit_code) *exit_code = rc;

    /* Re-encode the full capture as UTF-8 (consistent with the
     * non-streaming path). */
    if (full && full_n > 0) {
        size_t utf8_n = 0;
        char *u = _latin1_to_utf8_chunk(full, full_n, &utf8_n);
        free(full);
        return u;
    }
    if (!full) {
        full = (char *)malloc(1);
        if (full) full[0] = '\0';
    }
    return full;
}
