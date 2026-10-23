/* jobs.c - see jobs.h. */

#include "jobs.h"
#include "rpc.h"
#include "methods/methods.h"

#include <stdio.h>
#include <string.h>

#include <proto/exec.h>
#include <proto/dos.h>
#include <dos/dos.h>
#include <dos/dostags.h>
#include <exec/ports.h>
#include <utility/hooks.h>

/* How many job records are kept, running and finished together. A
 * new job evicts the oldest finished one; if all are running the start
 * is refused. Installer sequences run hundreds of short commands, so
 * finished records must be cheap: their output file is deleted as soon
 * as exec.cmd has returned it. */
#define JOB_SLOTS 32

/* Default priority for started work, below the workers so a busy job
 * never competes with the daemon answering requests about it. */
#define JOB_DEFAULT_PRI (-2)
#define JOB_MIN_PRI     (-20)

/* Tasks of the TCP/IP stack (Roadshow) the workers depend on. Workers
 * run one below the highest of these so they never outrank the stack
 * that carries their replies. */
static const char *const k_stack_tasks[] = {
    "TCP/IP Control", "TCP/IP Superserver", "TCP/IP Configuration", NULL
};
#define WORKER_PRI_FALLBACK (-1)

struct job {
    /* First, so a message pulled off the port is the job itself. */
    struct DeathMessage dm;
    int               in_use;
    uint32_t          id;
    int               state;
    volatile uint32_t pid;        /* set by job_entry in the child */
    volatile int      started;
    struct Task      *task;       /* only trusted after a pid check */
    int               priority;
    uint32_t          signals_sent;
    int               output_deleted;
    int               sandboxed;
    uint32_t          mem_free_before;
    uint32_t          mem_free_after;
    struct DateStamp  t_start;
    struct DateStamp  t_end;
    char              out_path[40];
    char              command[96];
};

static struct job              *g_jobs;   /* JOB_SLOTS, MEMF_SHARED */
static struct SignalSemaphore  *g_sem;
static struct MsgPort          *g_port;
static uint32_t                 g_next_id = 1;
static int                      g_worker_pri = WORKER_PRI_FALLBACK;

int jobs_worker_priority(void)  { return g_worker_pri; }
int jobs_max_priority(void)     { return g_worker_pri - 1; }
int jobs_default_priority(void) {
    return JOB_DEFAULT_PRI < jobs_max_priority()
        ? JOB_DEFAULT_PRI : jobs_max_priority();
}

static uint32_t _ms_between(const struct DateStamp *a,
                            const struct DateStamp *b) {
    int64_t ticks =
        ((int64_t)(b->ds_Days - a->ds_Days) * 1440 +
         (b->ds_Minute - a->ds_Minute)) * 60 * TICKS_PER_SECOND +
        (b->ds_Tick - a->ds_Tick);
    if (ticks < 0) ticks = 0;
    return (uint32_t)(ticks * 1000 / TICKS_PER_SECOND);
}

/* Runs inside the new shell process, before the command. */
static void job_entry(int32 data, int32 unused) {
    (void)unused;
    struct job *j = (struct job *)data;
    struct Process *me = (struct Process *)IExec->FindTask(NULL);
    /* No requesters from a job: nobody is at this screen to answer
     * one, and it blocks the job until somebody does. SYS_WindowPtr
     * below should already have done this; belt and braces. */
    me->pr_WindowPtr = (APTR)-1;
    j->task = &me->pr_Task;
    j->pid = me->pr_ProcessID;
    j->started = 1;
}

/* Collect death messages. Caller holds g_sem. */
static void _drain_locked(void) {
    struct Message *m;
    while ((m = IExec->GetMsg(g_port)) != NULL) {
        struct job *j = (struct job *)m;
        j->state = JOB_EXITED;
        j->mem_free_after = (uint32_t)IExec->AvailMem(MEMF_ANY);
        IDOS->DateStamp(&j->t_end);
    }
}

static struct job *_find_locked(uint32_t id) {
    for (int i = 0; i < JOB_SLOTS; i++) {
        if (g_jobs[i].in_use && g_jobs[i].id == id) return &g_jobs[i];
    }
    return NULL;
}

static void _fill_info(const struct job *j, struct job_info *o) {
    struct DateStamp now;
    IDOS->DateStamp(&now);
    o->id = j->id;
    o->state = j->state;
    o->pid = j->pid;
    o->priority = j->priority;
    o->rc = j->dm.dm_ReturnCode;
    o->result2 = j->dm.dm_Result2;
    o->elapsed_ms = _ms_between(&j->t_start,
                                j->state == JOB_EXITED ? &j->t_end : &now);
    o->mem_free_before = j->mem_free_before;
    o->mem_free_after = j->mem_free_after;
    o->output_deleted = j->output_deleted;
    o->sandboxed = j->sandboxed;
    o->signals_sent = j->signals_sent;
    strncpy(o->out_path, j->out_path, sizeof(o->out_path) - 1);
    o->out_path[sizeof(o->out_path) - 1] = '\0';
    strncpy(o->command, j->command, sizeof(o->command) - 1);
    o->command[sizeof(o->command) - 1] = '\0';
}

int jobs_init(void) {
    g_jobs = (struct job *)IExec->AllocVecTags(
        sizeof(struct job) * JOB_SLOTS,
        AVT_Type, MEMF_SHARED, AVT_ClearWithValue, 0, TAG_END);
    g_sem = (struct SignalSemaphore *)IExec->AllocSysObjectTags(
        ASOT_SEMAPHORE, TAG_END);
    g_port = (struct MsgPort *)IExec->AllocSysObjectTags(
        ASOT_PORT, ASOPORT_Action, PA_IGNORE, ASOPORT_AllocSig, FALSE,
        TAG_END);
    if (!g_jobs || !g_sem || !g_port) {
        if (g_port) IExec->FreeSysObject(ASOT_PORT, g_port);
        if (g_sem) IExec->FreeSysObject(ASOT_SEMAPHORE, g_sem);
        if (g_jobs) IExec->FreeVec(g_jobs);
        g_jobs = NULL; g_sem = NULL; g_port = NULL;
        return -1;
    }

    int found = 0, best = -128;
    IExec->Forbid();
    for (int i = 0; k_stack_tasks[i]; i++) {
        struct Task *t = IExec->FindTask(k_stack_tasks[i]);
        if (t) {
            found = 1;
            if (t->tc_Node.ln_Pri > best) best = t->tc_Node.ln_Pri;
        }
    }
    IExec->Permit();
    g_worker_pri = found ? best - 1 : WORKER_PRI_FALLBACK;
    if (g_worker_pri > 10) g_worker_pri = 10;
    if (g_worker_pri < -10) g_worker_pri = -10;
    return 0;
}

void jobs_shutdown(void) {
    if (!g_sem) return;
    for (int round = 0; ; round++) {
        int running = 0;
        IExec->ObtainSemaphore(g_sem);
        _drain_locked();
        for (int i = 0; i < JOB_SLOTS; i++) {
            if (g_jobs[i].in_use && g_jobs[i].state == JOB_RUNNING) {
                running++;
            }
        }
        IExec->ReleaseSemaphore(g_sem);
        if (running == 0) break;
        if (round == 0) {
            for (int i = 0; i < JOB_SLOTS; i++) {
                if (g_jobs[i].in_use && g_jobs[i].state == JOB_RUNNING) {
                    jobs_signal(g_jobs[i].id, SIGBREAKF_CTRL_C);
                }
            }
        }
        /* DOS replies a job's death message to g_port, inside g_jobs,
         * when the job ends. Freeing either while a job still runs
         * would have that reply write into freed memory. Wait. */
        if (round % 10 == 0) {
            IExec->DebugPrintF("[MCPd] shutdown waiting for %ld "
                               "running job(s)\n", (long)running);
        }
        IDOS->Delay(TICKS_PER_SECOND);
    }
    for (int i = 0; i < JOB_SLOTS; i++) {
        if (g_jobs[i].in_use && !g_jobs[i].output_deleted) {
            IDOS->Delete(g_jobs[i].out_path);
        }
    }
    IExec->FreeSysObject(ASOT_PORT, g_port);
    IExec->FreeSysObject(ASOT_SEMAPHORE, g_sem);
    IExec->FreeVec(g_jobs);
    g_jobs = NULL; g_sem = NULL; g_port = NULL;
}

/* Pick a slot: a free one, else the oldest finished job (its output
 * file goes with it). Caller holds g_sem. */
static struct job *_claim_slot_locked(void) {
    struct job *oldest = NULL;
    for (int i = 0; i < JOB_SLOTS; i++) {
        struct job *j = &g_jobs[i];
        if (!j->in_use) return j;
        if (j->state == JOB_EXITED && (!oldest || j->id < oldest->id)) {
            oldest = j;
        }
    }
    if (oldest && !oldest->output_deleted) IDOS->Delete(oldest->out_path);
    return oldest;
}

int jobs_start(const char *cmd, int priority, int sandboxed,
               uint32_t *out_id, int *out_priority, cJSON **out_err) {
    if (!g_sem) {
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
                                  "exec: job table unavailable", NULL);
        return -1;
    }
    if (priority > jobs_max_priority()) priority = jobs_max_priority();
    if (priority < JOB_MIN_PRI) priority = JOB_MIN_PRI;

    IExec->ObtainSemaphore(g_sem);
    _drain_locked();
    struct job *j = _claim_slot_locked();
    if (!j) {
        IExec->ReleaseSemaphore(g_sem);
        cJSON *d = cJSON_CreateObject();
        cJSON_AddNumberToObject(d, "max_jobs", JOB_SLOTS);
        *out_err = rpc_make_error(MCPD_ERR_BUSY,
            "exec: every job slot holds a running job", d);
        return -1;
    }
    memset(j, 0, sizeof(*j));
    j->in_use = 1;
    j->id = g_next_id++;
    j->state = JOB_RUNNING;
    j->priority = priority;
    j->sandboxed = sandboxed;
    j->dm.dm_Msg.mn_ReplyPort = g_port;
    j->dm.dm_Msg.mn_Length = sizeof(j->dm);
    j->dm.dm_ReturnCode = -1;
    j->dm.dm_Result2 = 0;
    snprintf(j->out_path, sizeof(j->out_path), "T:mcpd_job_%lu.out",
             (unsigned long)j->id);
    strncpy(j->command, cmd, sizeof(j->command) - 1);
    j->mem_free_before = (uint32_t)IExec->AvailMem(MEMF_ANY);
    IDOS->DateStamp(&j->t_start);
    uint32_t id = j->id;
    IExec->ReleaseSemaphore(g_sem);

    /* MODE_NEWFILE would lock the file exclusively for the job's whole
     * run, and every reader (exec.output, streaming) would get "object
     * is in use". MODE_READWRITE takes a shared lock; deleting first
     * makes it start empty, since ids restart with the daemon and an
     * old file of the same name may survive in T:. */
    IDOS->Delete(j->out_path);
    BPTR out = IDOS->Open(j->out_path, MODE_READWRITE);
    if (out == ZERO) {
        IExec->ObtainSemaphore(g_sem);
        j->in_use = 0;
        IExec->ReleaseSemaphore(g_sem);
        *out_err = target_error("exec: cannot create output file",
                                "T:");
        return -1;
    }
    /* Note: a running job's output appears in this file when the
     * shell flushes its own buffer -- often only when the command ends.
     * Setting FH_BufferMode on this handle does not change that
     * (checked on QEMU), so exec.output on a running job can lag. */

    /* SYS_Asynch: the shell closes `out` itself when it ends. */
    int32 kick = IDOS->SystemTags(cmd,
        SYS_Asynch,              TRUE,
        SYS_Output,              (Tag)out,
        SYS_Input,               (Tag)ZERO,
        NP_StackSize,            65536,
        /* Without this the async shell does NOT inherit the worker's
         * pr_WindowPtr=-1, and a job touching a missing volume ("List
         * Work:") pops "Please insert volume" on Workbench (seen on
         * the X5000, 2026-10-23). */
        SYS_WindowPtr,           (Tag)-1,
        NP_Priority,             priority,
        NP_NotifyOnDeathMessage, (Tag)&j->dm,
        NP_EntryCode,            (Tag)job_entry,
        NP_EntryData,            (Tag)j,
        TAG_DONE);
    if (kick != 0) {
        /* Not started: no death message will ever come. */
        IDOS->Close(out);
        IDOS->Delete(j->out_path);
        IExec->ObtainSemaphore(g_sem);
        j->in_use = 0;
        IExec->ReleaseSemaphore(g_sem);
        cJSON *d = cJSON_CreateObject();
        cJSON_AddNumberToObject(d, "systemtags_rc", (double)kick);
        *out_err = rpc_make_error(MCPD_ERR_TARGET,
                                  "exec: command could not be started", d);
        return -1;
    }

    /* The entry code records the process id within a tick or two; wait
     * for it (bounded) so a status or kill straight after has a pid. */
    for (int i = 0; i < 2 * TICKS_PER_SECOND && !j->started; i++) {
        IDOS->Delay(1);
    }
    *out_id = id;
    if (out_priority) *out_priority = priority;
    return 0;
}

int jobs_get(uint32_t id, struct job_info *out) {
    if (!g_sem) return -1;
    IExec->ObtainSemaphore(g_sem);
    _drain_locked();
    struct job *j = _find_locked(id);
    if (j) _fill_info(j, out);
    IExec->ReleaseSemaphore(g_sem);
    return j ? 0 : -1;
}

struct scan_ctx {
    uint32_t magic;
    uint32_t pid;
    uint32_t sigmask;
    int      sent;
};
#define SCAN_MAGIC 0x4D43504Au  /* "MCPJ" */

/* ProcessScan calls this for each process with the list held, so the
 * process cannot vanish between the id check and the Signal. Which of
 * the two pointer arguments is our context and which the Process is
 * told apart by the magic word rather than assumed. */
static int32 _scan_hook(struct Hook *h, APTR a, APTR b) {
    (void)h;
    struct scan_ctx *ctx = (struct scan_ctx *)a;
    struct Process *p = (struct Process *)b;
    if (!ctx || ctx->magic != SCAN_MAGIC) {
        ctx = (struct scan_ctx *)b;
        p = (struct Process *)a;
    }
    if (p && p->pr_ProcessID == ctx->pid) {
        IExec->Signal(&p->pr_Task, ctx->sigmask);
        ctx->sent = 1;
        return 1;  /* stop scanning */
    }
    return 0;
}

int jobs_signal(uint32_t id, uint32_t sigmask) {
    if (!g_sem) return -1;
    IExec->ObtainSemaphore(g_sem);
    _drain_locked();
    struct job *j = _find_locked(id);
    if (!j) {
        IExec->ReleaseSemaphore(g_sem);
        return -1;
    }
    int sent = 0;
    if (j->state == JOB_RUNNING && j->pid != 0) {
        struct scan_ctx ctx = { SCAN_MAGIC, j->pid, sigmask, 0 };
        struct Hook hook;
        memset(&hook, 0, sizeof(hook));
        hook.h_Entry = (HOOKFUNC)_scan_hook;
        IDOS->ProcessScan(&hook, &ctx, 0);
        sent = ctx.sent;
        if (sent) j->signals_sent |= sigmask;
    }
    IExec->ReleaseSemaphore(g_sem);
    return sent;
}

void jobs_drop_output(uint32_t id) {
    if (!g_sem) return;
    IExec->ObtainSemaphore(g_sem);
    struct job *j = _find_locked(id);
    if (j && !j->output_deleted && j->state == JOB_EXITED) {
        IDOS->Delete(j->out_path);
        j->output_deleted = 1;
    }
    IExec->ReleaseSemaphore(g_sem);
}

void jobs_list(cJSON *arr) {
    if (!g_sem) return;
    struct job_info info[JOB_SLOTS];
    int n = 0;
    IExec->ObtainSemaphore(g_sem);
    _drain_locked();
    for (int i = 0; i < JOB_SLOTS; i++) {
        if (g_jobs[i].in_use) _fill_info(&g_jobs[i], &info[n++]);
    }
    IExec->ReleaseSemaphore(g_sem);
    /* Newest first. n <= 32, so a simple insertion sort will do. */
    for (int i = 1; i < n; i++) {
        struct job_info t = info[i];
        int k = i - 1;
        while (k >= 0 && info[k].id < t.id) { info[k + 1] = info[k]; k--; }
        info[k + 1] = t;
    }
    for (int i = 0; i < n; i++) {
        cJSON *o = cJSON_CreateObject();
        cJSON_AddNumberToObject(o, "job_id", info[i].id);
        cJSON_AddStringToObject(o, "state",
            info[i].state == JOB_RUNNING ? "running" : "exited");
        if (info[i].state == JOB_EXITED) {
            cJSON_AddNumberToObject(o, "exit_code", info[i].rc);
        }
        cJSON_AddNumberToObject(o, "pid", info[i].pid);
        cJSON_AddNumberToObject(o, "priority", info[i].priority);
        cJSON_AddNumberToObject(o, "elapsed_ms", info[i].elapsed_ms);
        cJSON_AddStringToObject(o, "command", info[i].command);
        cJSON_AddItemToArray(arr, o);
    }
}
