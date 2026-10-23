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


/* Latin-1 -> UTF-8. Returns a malloc'd NUL-terminated buffer, or an
 * ASCII-only copy ('?' for high bytes) if the larger one can't be had. */
char *amiga_latin1_to_utf8(const char *src, size_t n, size_t *out_n_p) {
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


char *amiga_read_output(const char *path, size_t offset, size_t max,
                        size_t *raw_n, size_t *total) {
    if (raw_n) *raw_n = 0;
    if (total) *total = 0;
    /* Shared read: a running job's shell still has the file open for
     * writing, which AmigaOS allows alongside MODE_OLDFILE readers. */
    BPTR in = IDOS->Open(path, MODE_OLDFILE);
    if (in == ZERO) return amiga_latin1_to_utf8("", 0, NULL);

    int64_t size = 0;
    struct ExamineData *ed = IDOS->ExamineObjectTags(
        EX_FileHandleInput, in, TAG_END);
    if (ed) {
        size = (int64_t)ed->FileSize;
        IDOS->FreeDosObject(DOS_EXAMINEDATA, ed);
    }
    if (size < 0) size = 0;
    if (total) *total = (size_t)size;

    size_t want = 0;
    if ((int64_t)offset < size) want = (size_t)(size - (int64_t)offset);
    if (want > max) want = max;
    char *buf = (char *)malloc(want + 1);
    if (!buf) {
        IDOS->Close(in);
        return NULL;
    }
    LONG got = 0;
    if (want > 0) {
        if (offset > 0) {
            IDOS->ChangeFilePosition(in, (int64_t)offset, OFFSET_BEGINNING);
        }
        got = IDOS->Read(in, buf, (LONG)want);
        if (got < 0) got = 0;
    }
    IDOS->Close(in);
    if (raw_n) *raw_n = (size_t)got;
    char *u = amiga_latin1_to_utf8(buf, (size_t)got, NULL);
    free(buf);
    return u;
}
