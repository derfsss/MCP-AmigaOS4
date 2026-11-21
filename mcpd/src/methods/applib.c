/* application.library wrappers:
 *   sys.applications  - enumerate registered applications
 *   app.notify        - post a Ringhio notification
 *   applib_register   - called from main at startup; starts the
 *                       applib server process, which registers MCPd
 *   applib_shutdown   - called at exit; stops it (it unregisters)
 *
 * application.library v53.11+ requires interface version 2 (per the
 * SDK autodoc -IMPORTANT section). We open v2; if the system has
 * only an older lib we degrade to no-op gracefully.
 *
 * Why a server process. Notify() called from any task other than the
 * one that registered the application never returns (measured on
 * QEMU AmigaOne guests, AOS 4.1 FE, Ringhio running: a test program
 * that registers in one Process and calls Notify() from another
 * blocks indefinitely; the same calls from one Process answer at
 * once). MCPd used to register from its main task and call Notify()
 * from connection workers, so app.notify hung, and on 2.4 every file
 * transfer then hung behind it (the blanker hold calls
 * SetApplicationAttrs, which waited on the stuck Notify inside the
 * library). Now one Process, "MCPd applib", registers MCPd and makes
 * every application.library call that names our appID. Workers only
 * queue requests and signal it; a transfer never waits for it. */

#include "../rpc.h"
#include "../jobs.h"
#include "methods.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include <proto/exec.h>
#include <proto/dos.h>
#include <dos/dosextens.h>
#include <devices/timer.h>

#include <libraries/application.h>
#include <interfaces/application.h>


/* The library and interface are opened by the main task and shared:
 * GetApplicationList / GetApplicationAttrs work from any task. Only
 * calls on our own appID go through the server. */
static struct Library          *_appBase = NULL;
static struct ApplicationIFace *_IApp    = NULL;
static volatile uint32_t        _our_appID = 0;


static int _applib_open(void) {
    if (_appBase && _IApp) return 1;
    _appBase = (struct Library *)
        IExec->OpenLibrary("application.library", 53);
    if (!_appBase) return 0;
    /* MUST request interface v2 on AOS4.1 FE per the v53.11 IMPORTANT
     * note. Falling back to v1 leads to the lib popping a requester. */
    _IApp = (struct ApplicationIFace *)
        IExec->GetInterface(_appBase, "application", 2, NULL);
    if (!_IApp) {
        IExec->CloseLibrary(_appBase);
        _appBase = NULL;
        return 0;
    }
    return 1;
}

static void _applib_close(void) {
    if (_IApp) {
        IExec->DropInterface((struct Interface *)_IApp);
        _IApp = NULL;
    }
    if (_appBase) {
        IExec->CloseLibrary(_appBase);
        _appBase = NULL;
    }
}


/* ---- shared state (all under g_sem) -----------------------------
 *
 * The blanker during file transfers. A transfer has no user input for
 * its whole length, so a screen blanker starts in the middle of it; on
 * the X5000 an animated blanker then took enough CPU that the network
 * stalled and uploads timed out. application.library AutoDoc
 * (RegisterApplication, REGAPP_AllowsBlanker; SetApplicationAttrs,
 * APPATTR_AllowsBlanker): an application that does not want a blanker
 * in front sets AllowsBlanker FALSE, the system broadcasts
 * APPLIBMT_BlankerDisallow, and a blanker that is already blanking
 * "has to immediately stop doing so"; TRUE broadcasts
 * APPLIBMT_BlankerAllow. The user's blanker settings are not touched
 * (screenblanker.library's SetBlankingMode would change them
 * machine-wide).
 *
 * Holds are counted across the connection workers. The server sets
 * FALSE while any hold is open, and TRUE again BL_LINGER_S seconds
 * after the last one ended (a timer.device request), so the gaps
 * between the chunks of one upload do not let the blanker in. */
#define BL_LINGER_S       30
#define NOTIFY_TIMEOUT_MS 10000

struct notify_req {
    struct notify_req *next;
    int     done;          /* the server has called Notify */
    int     abandoned;     /* the worker gave up; the server frees it */
    uint32  rc;
    uint32  pri;
    char   *title;         /* copies, in this allocation */
    char   *text;
};

static struct SignalSemaphore *g_sem;
static int                 g_holds;       /* transfers running now */
static uint32              g_release_gen; /* bumped by every release */
static struct notify_req  *g_nq_head, *g_nq_tail;

/* Server process bookkeeping. g_srv is set by the server once it is
 * ready and cleared before it exits; workers read it under g_sem. */
static struct Task        *g_srv;
static uint32              g_srv_wake;    /* signal mask, server-owned */
static volatile int        g_srv_state;   /* 0 starting, 1 ready, -1 failed */
static volatile int        g_quit;
static struct MsgPort     *g_death_port;
static struct DeathMessage g_dm;
static int                 g_srv_alive;

static void _wake_server_locked(void) {
    if (g_srv && g_srv_wake) IExec->Signal(g_srv, g_srv_wake);
}


/* Public: a file transfer starts (rpc.c, stream.c). Any process.
 * Never waits for application.library. */
void applib_blanker_hold(void) {
    if (!g_sem) return;
    IExec->ObtainSemaphore(g_sem);
    g_holds++;
    _wake_server_locked();
    IExec->ReleaseSemaphore(g_sem);
}

/* Public: the transfer ended. */
void applib_blanker_release(void) {
    if (!g_sem) return;
    IExec->ObtainSemaphore(g_sem);
    if (g_holds > 0) g_holds--;
    g_release_gen++;
    _wake_server_locked();
    IExec->ReleaseSemaphore(g_sem);
}


/* ---- the server process ----------------------------------------- */

static void _srv_set_blanker(int allow) {
    _IApp->SetApplicationAttrs(_our_appID,
        APPATTR_AllowsBlanker, (Tag)(allow ? TRUE : FALSE), TAG_END);
}

static int32 _srv_entry(STRPTR args, int32 len, struct ExecBase *sysbase) {
    (void)args; (void)len; (void)sysbase;
    struct Task *me = IExec->FindTask(NULL);

    int8 wake_bit = (int8)IExec->AllocSignal(-1);
    struct MsgPort *tport = (struct MsgPort *)
        IExec->AllocSysObjectTags(ASOT_PORT, TAG_END);
    struct TimeRequest *treq = tport ? (struct TimeRequest *)
        IExec->AllocSysObjectTags(ASOT_IOREQUEST,
            ASOIOR_Size,      sizeof(struct TimeRequest),
            ASOIOR_ReplyPort, (Tag)tport,
            TAG_END) : NULL;
    int timer_open = treq && IExec->OpenDevice(TIMERNAME, UNIT_VBLANK,
                         (struct IORequest *)treq, 0) == 0;

    uint32 id = 0;
    if (wake_bit >= 0 && timer_open) {
        id = _IApp->RegisterApplication(
            "MCPd",
            REGAPP_URLIdentifier,  (Tag)"com.derfsss.mcpd",
            REGAPP_Description,    (Tag)"Model Context Protocol daemon",
            REGAPP_NoIcon,         (Tag)TRUE,
            REGAPP_Hidden,         (Tag)TRUE,
            REGAPP_AllowsBlanker,  (Tag)TRUE,
            TAG_END);
    }
    if (!id) {
        if (timer_open) IExec->CloseDevice((struct IORequest *)treq);
        if (treq) IExec->FreeSysObject(ASOT_IOREQUEST, treq);
        if (tport) IExec->FreeSysObject(ASOT_PORT, tport);
        if (wake_bit >= 0) IExec->FreeSignal(wake_bit);
        g_srv_state = -1;
        return 0;
    }

    struct MsgPort *aport = NULL;
    _IApp->GetApplicationAttrs(id, APPATTR_Port, (Tag)&aport, TAG_END);

    IExec->ObtainSemaphore(g_sem);
    _our_appID = id;
    g_srv_wake = 1u << (uint32)wake_bit;
    g_srv = me;
    IExec->ReleaseSemaphore(g_sem);
    g_srv_state = 1;

    int    off = 0;            /* AllowsBlanker is FALSE (ours) */
    int    timer_pending = 0;
    uint32 timer_gen = 0;      /* g_release_gen when the timer started */
    uint32 tsig = 1u << (uint32)tport->mp_SigBit;
    uint32 asig = aport ? (1u << (uint32)aport->mp_SigBit) : 0;

    for (;;) {
        uint32 got = IExec->Wait(g_srv_wake | tsig | asig | SIGBREAKF_CTRL_C);
        int fired = 0;
        if (timer_pending && IExec->CheckIO((struct IORequest *)treq)) {
            IExec->WaitIO((struct IORequest *)treq);
            timer_pending = 0;
            fired = 1;
        }
        if (g_quit || (got & SIGBREAKF_CTRL_C)) break;

        /* Messages application.library sends to every registered
         * application (quit, to-front, ...): reply and ignore them; a
         * daemon has no window to show and the watchdog owns its
         * lifetime. */
        if (aport) {
            struct Message *m;
            while ((m = IExec->GetMsg(aport)) != NULL) IExec->ReplyMsg(m);
        }

        /* Notifications. */
        for (;;) {
            IExec->ObtainSemaphore(g_sem);
            struct notify_req *r = g_nq_head;
            if (r) {
                g_nq_head = r->next;
                if (!g_nq_head) g_nq_tail = NULL;
            }
            int skip = r && r->abandoned;
            IExec->ReleaseSemaphore(g_sem);
            if (!r) break;
            if (skip) { IExec->FreeVec(r); continue; }
            uint32 rc = _IApp->Notify(id,
                APPNOTIFY_Title, (Tag)r->title,
                APPNOTIFY_Text,  (Tag)r->text,
                APPNOTIFY_Pri,   (Tag)r->pri,
                TAG_END);
            IExec->ObtainSemaphore(g_sem);
            if (r->abandoned) {
                IExec->ReleaseSemaphore(g_sem);
                IExec->FreeVec(r);
            } else {
                r->rc = rc;
                r->done = 1;
                IExec->ReleaseSemaphore(g_sem);
            }
        }

        /* The blanker. */
        IExec->ObtainSemaphore(g_sem);
        int    holds = g_holds;
        uint32 gen   = g_release_gen;
        IExec->ReleaseSemaphore(g_sem);
        if (holds > 0) {
            if (!off) { _srv_set_blanker(0); off = 1; }
            if (timer_pending) {
                IExec->AbortIO((struct IORequest *)treq);
                IExec->WaitIO((struct IORequest *)treq);
                timer_pending = 0;
            }
        } else if (off) {
            if (fired && gen == timer_gen) {
                _srv_set_blanker(1);
                off = 0;
            } else if (!timer_pending || gen != timer_gen) {
                if (timer_pending) {
                    IExec->AbortIO((struct IORequest *)treq);
                    IExec->WaitIO((struct IORequest *)treq);
                }
                treq->Request.io_Command = TR_ADDREQUEST;
                treq->Time.Seconds       = BL_LINGER_S;
                treq->Time.Microseconds  = 0;
                IExec->SendIO((struct IORequest *)treq);
                timer_pending = 1;
                timer_gen = gen;
            }
        }
    }

    /* Shutdown: no new requests, fail the queued ones, give the
     * blanker back, unregister. */
    IExec->ObtainSemaphore(g_sem);
    g_srv = NULL;
    g_srv_wake = 0;
    struct notify_req *r = g_nq_head;
    g_nq_head = g_nq_tail = NULL;
    while (r) {
        struct notify_req *nx = r->next;
        if (r->abandoned) IExec->FreeVec(r);
        else { r->rc = (uint32)-1; r->done = 1; }
        r = nx;
    }
    IExec->ReleaseSemaphore(g_sem);

    if (timer_pending) {
        IExec->AbortIO((struct IORequest *)treq);
        IExec->WaitIO((struct IORequest *)treq);
    }
    if (off) _srv_set_blanker(1);
    if (aport) {
        struct Message *m;
        while ((m = IExec->GetMsg(aport)) != NULL) IExec->ReplyMsg(m);
    }
    _IApp->UnregisterApplication(id, TAG_END);
    _our_appID = 0;
    IExec->CloseDevice((struct IORequest *)treq);
    IExec->FreeSysObject(ASOT_IOREQUEST, treq);
    IExec->FreeSysObject(ASOT_PORT, tport);
    IExec->FreeSignal(wake_bit);
    return 0;
}


/* Public: called once at MCPd startup from main.c, before the socket
 * listens. Best-effort: without application.library, or if the
 * server cannot start, MCPd runs without registration, blanker hold
 * or app.notify. Waits up to 5 s for the registration. */
void applib_register(void) {
    if (!_applib_open()) return;
    g_sem = (struct SignalSemaphore *)IExec->AllocSysObjectTags(
        ASOT_SEMAPHORE, TAG_END);
    g_death_port = (struct MsgPort *)IExec->AllocSysObjectTags(
        ASOT_PORT, ASOPORT_Action, PA_IGNORE, ASOPORT_AllocSig, FALSE,
        TAG_END);
    if (!g_sem || !g_death_port) goto fail;

    memset(&g_dm, 0, sizeof(g_dm));
    g_dm.dm_Msg.mn_ReplyPort = g_death_port;
    g_dm.dm_Msg.mn_Length = sizeof(g_dm);
    g_srv_state = 0;
    g_quit = 0;
    /* At the workers' priority: a transfer's hold must not wait
     * behind a busy worker for the blanker to go off. */
    struct Process *p = IDOS->CreateNewProcTags(
        NP_Entry,                (Tag)_srv_entry,
        NP_Name,                 (Tag)"MCPd applib",
        NP_Priority,             jobs_worker_priority(),
        NP_StackSize,            32768,
        NP_Child,                FALSE,
        NP_NotifyOnDeathMessage, (Tag)&g_dm,
        TAG_DONE);
    if (!p) goto fail;
    g_srv_alive = 1;
    for (int i = 0; i < 250 && g_srv_state == 0; i++) IDOS->Delay(1);
    return;

fail:
    if (g_death_port) IExec->FreeSysObject(ASOT_PORT, g_death_port);
    if (g_sem) IExec->FreeSysObject(ASOT_SEMAPHORE, g_sem);
    g_death_port = NULL;
    g_sem = NULL;
}


void applib_shutdown(void) {
    if (g_srv_alive) {
        g_quit = 1;
        for (;;) {
            IExec->ObtainSemaphore(g_sem);
            if (g_srv) IExec->Signal(g_srv, SIGBREAKF_CTRL_C);
            IExec->ReleaseSemaphore(g_sem);
            struct Message *m;
            while ((m = IExec->GetMsg(g_death_port)) != NULL) {
                if (m == &g_dm.dm_Msg) g_srv_alive = 0;
            }
            if (!g_srv_alive) break;
            /* The death message is written into g_dm when the process
             * ends, and its code is in this binary: wait for it. */
            IDOS->Delay(5);
        }
    }
    if (g_death_port) IExec->FreeSysObject(ASOT_PORT, g_death_port);
    if (g_sem) IExec->FreeSysObject(ASOT_SEMAPHORE, g_sem);
    g_death_port = NULL;
    g_sem = NULL;
    _applib_close();
}


/* ---- sys.applications ----------------------------------------- */

int sys_applications(cJSON *params, cJSON **out_result, cJSON **out_err) {
    (void)params; (void)out_err;

    cJSON *r = cJSON_CreateObject();
    if (!_applib_open()) {
        cJSON_AddBoolToObject(r, "available", 0);
        cJSON_AddArrayToObject(r, "applications");
        *out_result = r;
        return 0;
    }
    cJSON_AddBoolToObject(r, "available", 1);

    struct MinList *list = _IApp->GetApplicationList();
    cJSON *arr = cJSON_AddArrayToObject(r, "applications");
    if (!list) {
        *out_result = r;
        return 0;
    }

    /* Walk MinList of ApplicationNode entries. */
    struct ApplicationNode *node;
    for (node = (struct ApplicationNode *)list->mlh_Head;
         node && node->node.mln_Succ;
         node = (struct ApplicationNode *)node->node.mln_Succ) {
        cJSON *e = cJSON_CreateObject();
        cJSON_AddNumberToObject(e, "appID", (double)(uint32_t)node->appID);
        if (node->name) cJSON_AddStringToObject(e, "name", node->name);

        /* Pull additional attrs by appID. */
        STRPTR url = NULL, fname = NULL, descr = NULL;
        BOOL hidden = FALSE, allows_blanker = TRUE;
        _IApp->GetApplicationAttrs(node->appID,
            APPATTR_URLIdentifier, (Tag)&url,
            APPATTR_FileName,      (Tag)&fname,
            APPATTR_Description,   (Tag)&descr,
            APPATTR_Hidden,        (Tag)&hidden,
            APPATTR_AllowsBlanker, (Tag)&allows_blanker,
            TAG_END);
        if (url)   cJSON_AddStringToObject(e, "url_identifier", url);
        if (fname) cJSON_AddStringToObject(e, "filename", fname);
        if (descr) cJSON_AddStringToObject(e, "description", descr);
        cJSON_AddBoolToObject(e, "hidden", hidden ? 1 : 0);
        cJSON_AddBoolToObject(e, "allows_blanker", allows_blanker ? 1 : 0);
        cJSON_AddItemToArray(arr, e);
    }
    _IApp->FreeApplicationList(list);

    *out_result = r;
    return 0;
}


/* ---- app.notify ----------------------------------------------- */

/* Post a Ringhio popup. Requires the Ringhio server to be running
 * (it ships with AmigaOS 4.1 FE). The applib server makes the call
 * (see the top of this file); this worker waits up to
 * NOTIFY_TIMEOUT_MS for it. */
int app_notify(cJSON *params, cJSON **out_result, cJSON **out_err) {
    const char *title = p_str(params, "title", out_err);
    if (!title) return 0;
    const char *text  = p_str(params, "text",  out_err);
    if (!text)  return 0;

    long long pri = p_int(params, "priority", 0);

    size_t tl = strlen(title) + 1, xl = strlen(text) + 1;
    struct notify_req *req = (struct notify_req *)IExec->AllocVecTags(
        sizeof(*req) + tl + xl, AVT_Type, MEMF_SHARED, TAG_END);
    if (!req) {
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL, "out of memory", NULL);
        return 0;
    }
    memset(req, 0, sizeof(*req));
    req->title = (char *)(req + 1);
    req->text  = req->title + tl;
    memcpy(req->title, title, tl);
    memcpy(req->text, text, xl);
    req->pri = (uint32)pri;

    if (!g_sem) {
        IExec->FreeVec(req);
        *out_err = rpc_make_error(MCPD_ERR_NOTCAPABLE,
                                  "application.library v53 unavailable",
                                  NULL);
        return 0;
    }
    IExec->ObtainSemaphore(g_sem);
    if (!g_srv) {
        IExec->ReleaseSemaphore(g_sem);
        IExec->FreeVec(req);
        *out_err = rpc_make_error(MCPD_ERR_NOTCAPABLE,
                                  "MCPd is not registered with "
                                  "application.library", NULL);
        return 0;
    }
    if (g_nq_tail) g_nq_tail->next = req; else g_nq_head = req;
    g_nq_tail = req;
    _wake_server_locked();
    IExec->ReleaseSemaphore(g_sem);

    int done = 0;
    uint32 rc = 0;
    for (int waited = 0; ; waited += 20) {
        IExec->ObtainSemaphore(g_sem);
        done = req->done;
        rc = req->rc;
        if (!done && waited >= NOTIFY_TIMEOUT_MS) req->abandoned = 1;
        IExec->ReleaseSemaphore(g_sem);
        if (done || waited >= NOTIFY_TIMEOUT_MS) break;
        IDOS->Delay(1);   /* 20 ms */
    }
    if (!done) {
        /* The server frees it when it gets to it. */
        cJSON *d = cJSON_CreateObject();
        if (d) cJSON_AddNumberToObject(d, "timeout_ms", NOTIFY_TIMEOUT_MS);
        *out_err = rpc_make_error(MCPD_ERR_TARGET,
            "app.notify: application.library did not answer", d);
        return 0;
    }
    IExec->FreeVec(req);

    cJSON *r = cJSON_CreateObject();
    cJSON_AddNumberToObject(r, "result_code", (double)rc);
    cJSON_AddBoolToObject(r, "queued",
        (rc == APPNOTIFY_OK_MSGQUEUED ||
         rc == APPNOTIFY_OK_APPREGISTERED) ? 1 : 0);
    *out_result = r;
    return 0;
}
