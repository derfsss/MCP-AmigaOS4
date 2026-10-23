/* jobs.h - commands MCPd has started, tracked by id.
 *
 * Every exec.cmd runs as a job: an asynchronous SystemTags() shell
 * whose process id is recorded from inside the new process
 * (NP_EntryCode) and whose exit code arrives in a DeathMessage
 * (NP_NotifyOnDeathMessage). A job outlives the RPC that started it,
 * so a caller whose wait timed out can still ask whether it is
 * running, read its output, or send it a break signal.
 *
 * All job state is global and shared by the connection workers,
 * guarded by one semaphore. Death messages land on one port with
 * PA_IGNORE, so nobody is signalled: every jobs_* call drains the
 * port first. The port must outlive every job, which is why
 * jobs_shutdown() waits for running jobs instead of freeing it. */
#ifndef MCPD_JOBS_H
#define MCPD_JOBS_H

#include <stdint.h>
#include <stddef.h>

#include "cJSON.h"

enum job_state {
    JOB_RUNNING = 1,  /* started; no death message yet */
    JOB_EXITED  = 2   /* death message received; rc valid */
};

/* Snapshot of one job, copied out under the semaphore. */
struct job_info {
    uint32_t id;
    int      state;
    uint32_t pid;
    int      priority;
    int32_t  rc;
    int32_t  result2;
    uint32_t elapsed_ms;
    uint32_t mem_free_before;
    uint32_t mem_free_after;    /* 0 while running */
    int      output_deleted;
    uint32_t signals_sent;      /* SIGBREAKF_* mask sent so far */
    char     out_path[40];
    char     command[96];       /* truncated copy, for listings */
};

/* Set up the table and port, and work out the worker priority (one
 * below the TCP/IP stack's tasks). Call once from main() after
 * bsdsocket is open. Returns 0 on success. */
int jobs_init(void);

/* Ask running jobs to stop (CTRL-C) and wait for them; the port and
 * table are freed only once none is left running. */
void jobs_shutdown(void);

/* Priority for per-connection workers, decided in jobs_init(). */
int jobs_worker_priority(void);
/* Default and highest priority a job may run at. */
int jobs_default_priority(void);
int jobs_max_priority(void);

/* Start `cmd` as a job at `priority` (clamped to jobs_max_priority).
 * The current directory of the calling process is inherited. On
 * success returns 0 and the id in *out_id; on failure returns -1 and
 * an RPC error in *out_err. */
int jobs_start(const char *cmd, int priority, uint32_t *out_id,
               int *out_priority, cJSON **out_err);

/* Copy a job's state into *out. Returns 0, or -1 for an unknown id. */
int jobs_get(uint32_t id, struct job_info *out);

/* Send SIGBREAKF_* `sigmask` to a running job, after confirming its
 * process id is still in the process list. Returns 1 if sent, 0 if the
 * job is not running (or its process is already gone), -1 for an
 * unknown id. */
int jobs_signal(uint32_t id, uint32_t sigmask);

/* Delete a finished job's output file and remember it is gone. */
void jobs_drop_output(uint32_t id);

/* Append a summary of every job, newest first, to `arr`. */
void jobs_list(cJSON *arr);

#endif
