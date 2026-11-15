/* application.library wrappers:
 *   sys.applications  - enumerate registered applications
 *   app.notify        - post a Ringhio notification
 *   applib_register   - called from main at startup; registers MCPd
 *   applib_shutdown   - called at exit; unregisters
 *
 * application.library v53.11+ requires interface version 2 (per the
 * SDK autodoc -IMPORTANT section). We open v2; if the system has
 * only an older lib we degrade to no-op gracefully. */

#include "../rpc.h"
#include "methods.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include <proto/exec.h>
#include <proto/dos.h>
#include <dos/dosextens.h>

#include <libraries/application.h>
#include <interfaces/application.h>


/* Globals retained between startup register / sys.applications /
 * app.notify / shutdown unregister. NULL until applib_register
 * succeeds. */
static struct Library         *_appBase = NULL;
static struct ApplicationIFace *_IApp   = NULL;
static uint32_t                _our_appID = 0;


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


static void _bl_init(void);

/* Public: called once at MCPd startup from main.c. Best-effort. */
void applib_register(void) {
    if (!_applib_open()) return;
    _our_appID = _IApp->RegisterApplication(
        "MCPd",
        REGAPP_URLIdentifier,  (Tag)"com.derfsss.mcpd",
        REGAPP_Description,    (Tag)"Model Context Protocol daemon",
        REGAPP_NoIcon,         (Tag)TRUE,
        REGAPP_Hidden,         (Tag)TRUE,
        REGAPP_AllowsBlanker,  (Tag)TRUE,
        TAG_END);
    if (_our_appID) _bl_init();
}


/* ---- the blanker during file transfers --------------------------
 *
 * A transfer has no user input for its whole length, so a screen
 * blanker starts in the middle of it; on the X5000 an animated blanker
 * then took enough CPU that the network stalled and uploads timed out.
 * application.library AutoDoc (RegisterApplication, REGAPP_AllowsBlanker;
 * SetApplicationAttrs, APPATTR_AllowsBlanker): an application that does
 * not want a blanker in front sets AllowsBlanker FALSE, the system
 * broadcasts APPLIBMT_BlankerDisallow, and a blanker that is already
 * blanking "has to immediately stop doing so"; TRUE broadcasts
 * APPLIBMT_BlankerAllow. The user's blanker settings are not touched
 * (screenblanker.library's SetBlankingMode would change them
 * machine-wide).
 *
 * Holds are counted across the connection workers (one semaphore).
 * The first hold sets FALSE. TRUE comes back only after BL_LINGER_S
 * seconds with no transfer, from a small linger process, so the gaps
 * between the chunks of one upload do not let the blanker in. The
 * linger process is the jobs.c pattern: its death message goes to a
 * port that ignores signals, drained with GetMsg; shutdown waits for it.
 */
#define BL_LINGER_S 30
#define BL_TICK_S   5

static struct SignalSemaphore *g_bl_sem;
static struct MsgPort         *g_bl_port;   /* the linger process's death message */
static struct DeathMessage     g_bl_dm;
static int                     g_bl_holds;  /* transfers running now */
static int                     g_bl_idle_s; /* seconds since the last one ended */
static int                     g_bl_off;    /* AllowsBlanker is FALSE (ours) */
static int                     g_bl_alive;  /* a linger process exists */
static volatile int            g_bl_quit;

/* Caller holds g_bl_sem. */
static void _bl_drain_locked(void) {
    struct Message *m;
    while ((m = IExec->GetMsg(g_bl_port)) != NULL) {
        if (m == &g_bl_dm.dm_Msg) g_bl_alive = 0;
    }
}

/* Caller holds g_bl_sem. */
static void _bl_allow_locked(void) {
    if (g_bl_off && _IApp && _our_appID) {
        _IApp->SetApplicationAttrs(_our_appID,
            APPATTR_AllowsBlanker, (Tag)TRUE, TAG_END);
    }
    g_bl_off = 0;
}

static int32 _bl_linger_entry(STRPTR args, int32 len, struct ExecBase *sysbase) {
    (void)args; (void)len; (void)sysbase;
    for (;;) {
        IDOS->Delay(BL_TICK_S * TICKS_PER_SECOND);
        IExec->ObtainSemaphore(g_bl_sem);
        int done = 0;
        if (g_bl_quit) {
            done = 1;
        } else if (g_bl_holds == 0) {
            g_bl_idle_s += BL_TICK_S;
            if (g_bl_idle_s >= BL_LINGER_S) {
                _bl_allow_locked();
                done = 1;
            }
        }
        IExec->ReleaseSemaphore(g_bl_sem);
        if (done) break;
    }
    return 0;
}

/* Caller holds g_bl_sem. 1 if a linger process now runs. */
static int _bl_start_linger_locked(void) {
    _bl_drain_locked();
    if (g_bl_alive) return 1;
    if (g_bl_quit) return 0;
    memset(&g_bl_dm, 0, sizeof(g_bl_dm));
    g_bl_dm.dm_Msg.mn_ReplyPort = g_bl_port;
    g_bl_dm.dm_Msg.mn_Length = sizeof(g_bl_dm);
    struct Process *p = IDOS->CreateNewProcTags(
        NP_Entry,                (Tag)_bl_linger_entry,
        NP_Name,                 (Tag)"MCPd blanker hold",
        NP_Priority,             0,
        NP_StackSize,            16384,
        NP_Child,                FALSE,
        NP_NotifyOnDeathMessage, (Tag)&g_bl_dm,
        TAG_DONE);
    if (!p) return 0;
    g_bl_alive = 1;
    return 1;
}

/* Public: a file transfer starts (rpc.c, stream.c). Any process. */
void applib_blanker_hold(void) {
    if (!g_bl_sem || !_IApp || !_our_appID) return;
    IExec->ObtainSemaphore(g_bl_sem);
    g_bl_holds++;
    g_bl_idle_s = 0;
    if (!g_bl_off) {
        _IApp->SetApplicationAttrs(_our_appID,
            APPATTR_AllowsBlanker, (Tag)FALSE, TAG_END);
        g_bl_off = 1;
    }
    IExec->ReleaseSemaphore(g_bl_sem);
}

/* Public: the transfer ended. The linger process restores the blanker;
 * without one (CreateNewProc refused) it is restored here, at once. */
void applib_blanker_release(void) {
    if (!g_bl_sem || !_IApp || !_our_appID) return;
    IExec->ObtainSemaphore(g_bl_sem);
    if (g_bl_holds > 0) g_bl_holds--;
    g_bl_idle_s = 0;
    if (g_bl_holds == 0 && g_bl_off && !_bl_start_linger_locked()) {
        _bl_allow_locked();
    }
    IExec->ReleaseSemaphore(g_bl_sem);
}

static void _bl_init(void) {
    g_bl_sem = (struct SignalSemaphore *)IExec->AllocSysObjectTags(
        ASOT_SEMAPHORE, TAG_END);
    g_bl_port = (struct MsgPort *)IExec->AllocSysObjectTags(
        ASOT_PORT, ASOPORT_Action, PA_IGNORE, ASOPORT_AllocSig, FALSE,
        TAG_END);
    if (!g_bl_sem || !g_bl_port) {
        if (g_bl_port) IExec->FreeSysObject(ASOT_PORT, g_bl_port);
        if (g_bl_sem) IExec->FreeSysObject(ASOT_SEMAPHORE, g_bl_sem);
        g_bl_sem = NULL; g_bl_port = NULL;
    }
}

static void _bl_shutdown(void) {
    if (!g_bl_sem) return;
    g_bl_quit = 1;
    for (;;) {
        IExec->ObtainSemaphore(g_bl_sem);
        _bl_drain_locked();
        int alive = g_bl_alive;
        IExec->ReleaseSemaphore(g_bl_sem);
        if (!alive) break;
        /* The death message is written into g_bl_dm when the process
         * ends; freeing the port first would have DOS reply into freed
         * memory. At most one BL_TICK_S. */
        IDOS->Delay(TICKS_PER_SECOND);
    }
    IExec->ObtainSemaphore(g_bl_sem);
    _bl_allow_locked();
    IExec->ReleaseSemaphore(g_bl_sem);
    IExec->FreeSysObject(ASOT_PORT, g_bl_port);
    IExec->FreeSysObject(ASOT_SEMAPHORE, g_bl_sem);
    g_bl_sem = NULL; g_bl_port = NULL;
}


void applib_shutdown(void) {
    _bl_shutdown();
    if (_IApp && _our_appID) {
        _IApp->UnregisterApplication(_our_appID, TAG_END);
        _our_appID = 0;
    }
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
        BOOL hidden = FALSE;
        _IApp->GetApplicationAttrs(node->appID,
            APPATTR_URLIdentifier, (Tag)&url,
            APPATTR_FileName,      (Tag)&fname,
            APPATTR_Description,   (Tag)&descr,
            APPATTR_Hidden,        (Tag)&hidden,
            TAG_END);
        if (url)   cJSON_AddStringToObject(e, "url_identifier", url);
        if (fname) cJSON_AddStringToObject(e, "filename", fname);
        if (descr) cJSON_AddStringToObject(e, "description", descr);
        cJSON_AddBoolToObject(e, "hidden", hidden ? 1 : 0);
        cJSON_AddItemToArray(arr, e);
    }
    _IApp->FreeApplicationList(list);

    *out_result = r;
    return 0;
}


/* ---- app.notify ----------------------------------------------- */

/* Post a Ringhio popup. Requires the Ringhio server to be running
 * (it ships with AmigaOS 4.1 FE). */
int app_notify(cJSON *params, cJSON **out_result, cJSON **out_err) {
    const char *title = p_str(params, "title", out_err);
    if (!title) return 0;
    const char *text  = p_str(params, "text",  out_err);
    if (!text)  return 0;

    long long pri = p_int(params, "priority", 0);

    if (!_applib_open()) {
        *out_err = rpc_make_error(MCPD_ERR_NOTCAPABLE,
                                  "application.library v53 unavailable",
                                  NULL);
        return 0;
    }

    if (!_our_appID) {
        applib_register();
        if (!_our_appID) {
            *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
                                      "RegisterApplication failed",
                                      NULL);
            return 0;
        }
    }

    uint32 rc = _IApp->Notify(_our_appID,
        APPNOTIFY_Title, (Tag)title,
        APPNOTIFY_Text,  (Tag)text,
        APPNOTIFY_Pri,   (Tag)(uint32)pri,
        TAG_END);

    cJSON *r = cJSON_CreateObject();
    cJSON_AddNumberToObject(r, "result_code", (double)rc);
    cJSON_AddBoolToObject(r, "queued",
        (rc == APPNOTIFY_OK_MSGQUEUED ||
         rc == APPNOTIFY_OK_APPREGISTERED) ? 1 : 0);
    *out_result = r;
    return 0;
}
