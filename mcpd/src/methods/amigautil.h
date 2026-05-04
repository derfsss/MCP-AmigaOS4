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
 * `timeout_s` is currently advisory - SystemTagList is synchronous.
 * A future version may swap to a CreateNewProcTags-based variant
 * with kill after `timeout_s` seconds.
 */
char *amiga_run_command(const char *cmd, int *exit_code, double timeout_s);

/* Streaming variant of amiga_run_command. Runs `cmd` asynchronously
 * via SystemTags(SYS_Asynch=TRUE)
 * with stdout captured to a temp file. Polls the temp file every
 * `poll_ms` ms; for each chunk of new bytes, calls the callback with
 * the bytes (UTF-8-reencoded) - typically the caller emits a
 * `proc.stdout` JSON-RPC notification per chunk. After the child
 * exits, returns the complete captured output as a malloc'd
 * NUL-terminated string (caller frees). The callback is invoked
 * with NULL/0 as a final "done" signal once the child exits.
 *
 * Returns NULL on internal failure. *exit_code receives RC.
 */
char *amiga_run_command_streaming(
    const char *cmd, int *exit_code, double timeout_s,
    int poll_ms,
    void (*on_chunk)(const char *bytes, size_t n, void *user),
    void *user);

#endif
