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
}


void applib_shutdown(void) {
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
