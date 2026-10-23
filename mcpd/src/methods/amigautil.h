/* amigautil.h - shared AmigaOS helpers for method handlers. */
#ifndef MCPD_AMIGAUTIL_H
#define MCPD_AMIGAUTIL_H

#include <stddef.h>

/* Run an AmigaDOS command via SystemTagList and return its captured
 * stdout as a malloc'd NUL-terminated string. Caller frees.
 *
 * Returns NULL on internal failure (OOM, pipe failure). On AmigaDOS
 * command failure (non-zero exit), still returns the captured output
 * if any. *exit_code receives the SystemTagList return value (RC).
 *
 * Synchronous and untracked, so `timeout_s` is ignored: for the
 * daemon's own short internal commands (Version, Delete ALL). Anything
 * a client asks to run goes through jobs.c instead.
 */
char *amiga_run_command(const char *cmd, int *exit_code, double timeout_s);

/* Latin-1 -> UTF-8 (cJSON needs valid UTF-8; AmigaOS commands print
 * ISO-8859-1). Returns malloc'd NUL-terminated text, caller frees;
 * *out_n gets its length. NULL only when out of memory. */
char *amiga_latin1_to_utf8(const char *src, size_t n, size_t *out_n);

/* Read up to `max` bytes of `path` from `offset`, as UTF-8 text.
 * *raw_n gets the bytes read and *total the file's size, so a caller
 * can tell a capped read (offset + raw_n < total) from the end.
 * A missing file reads as empty. NULL only when out of memory. */
char *amiga_read_output(const char *path, size_t offset, size_t max,
                        size_t *raw_n, size_t *total);

#endif
