/* wb.* method handlers - public screens + their windows.
 *
 * Walks IntuitionBase->FirstScreen / Screen->NextScreen and
 * Screen->FirstWindow / Window->NextWindow under
 * IIntuition->LockIBase() so the chain isn't mutated mid-walk.
 *
 * Each method opens intuition.library + IIntuition for the call and
 * drops them again on exit. The exec/sys introspection methods
 * don't need this because exec.library / IExec are always available;
 * here we have to take + release the intuition interface explicitly.
 */

#include "../rpc.h"
#include "methods.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <proto/exec.h>
#include <proto/intuition.h>
#include <intuition/intuition.h>
#include <intuition/intuitionbase.h>


static char *sanitize_str(const char *src, char *dst, size_t dst_cap) {
    if (!src) src = "";
    size_t n = 0;
    while (src[n] && n < dst_cap - 1) {
        unsigned char c = (unsigned char)src[n];
        dst[n] = (c >= 0x20 && c < 0x7f) ? (char)c : '?';
        n++;
    }
    dst[n] = '\0';
    return dst;
}


/* Open intuition.library + IIntuition. Returns NULL+sets out_err on
 * failure. Caller must call _release_intuition() when done. */
static struct IntuitionIFace *_obtain_intuition(struct Library **out_base,
                                                cJSON **out_err) {
    struct Library *base = IExec->OpenLibrary("intuition.library", 50);
    if (!base) {
        if (out_err && !*out_err)
            *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
                                      "OpenLibrary intuition.library v50 failed",
                                      NULL);
        return NULL;
    }
    struct IntuitionIFace *iface = (struct IntuitionIFace *)
        IExec->GetInterface(base, "main", 1, NULL);
    if (!iface) {
        IExec->CloseLibrary(base);
        if (out_err && !*out_err)
            *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
                                      "GetInterface(intuition main) failed",
                                      NULL);
        return NULL;
    }
    *out_base = base;
    return iface;
}


static void _release_intuition(struct IntuitionIFace *iface,
                               struct Library *base) {
    if (iface) IExec->DropInterface((struct Interface *)iface);
    if (base)  IExec->CloseLibrary(base);
}


/* ---- wb.screens -------------------------------------------------- */

int wb_screens(cJSON *params, cJSON **out_result, cJSON **out_err) {
    (void)params;

    struct Library *ibase = NULL;
    struct IntuitionIFace *ii = _obtain_intuition(&ibase, out_err);
    if (!ii) return 0;

    cJSON *arr = cJSON_CreateArray();
    char buf[160];

    ULONG lock = ii->LockIBase(0);
    /* In AOS4 the IntuitionBase pointer is the library base. The
     * FirstScreen field in struct IntuitionBase is preserved (legacy)
     * for direct chain walks. */
    struct IntuitionBase *ib = (struct IntuitionBase *)ibase;
    int idx = 0;
    for (struct Screen *s = ib->FirstScreen; s != NULL; s = s->NextScreen) {
        cJSON *o = cJSON_CreateObject();
        cJSON_AddNumberToObject(o, "index", (double)idx);
        cJSON_AddStringToObject(o, "title",
            sanitize_str((const char *)s->Title, buf, sizeof(buf)));
        cJSON_AddStringToObject(o, "default_title",
            sanitize_str((const char *)s->DefaultTitle, buf, sizeof(buf)));
        cJSON_AddNumberToObject(o, "left",   (double)s->LeftEdge);
        cJSON_AddNumberToObject(o, "top",    (double)s->TopEdge);
        cJSON_AddNumberToObject(o, "width",  (double)s->Width);
        cJSON_AddNumberToObject(o, "height", (double)s->Height);
        cJSON_AddNumberToObject(o, "bar_height", (double)s->BarHeight);
        cJSON_AddNumberToObject(o, "flags",  (double)(unsigned int)s->Flags);
        /* Window count for this screen. */
        int wcount = 0;
        for (struct Window *w = s->FirstWindow; w != NULL; w = w->NextWindow) {
            wcount++;
        }
        cJSON_AddNumberToObject(o, "window_count", (double)wcount);
        cJSON_AddItemToArray(arr, o);
        idx++;
    }
    ii->UnlockIBase(lock);

    _release_intuition(ii, ibase);
    *out_result = arr;
    return 0;
}


/* ---- wb.publicscreens ------------------------------------------- */

int wb_publicscreens(cJSON *params, cJSON **out_result, cJSON **out_err) {
    (void)params;

    struct Library *ibase = NULL;
    struct IntuitionIFace *ii = _obtain_intuition(&ibase, out_err);
    if (!ii) return 0;

    cJSON *arr = cJSON_CreateArray();
    char buf[160];

    /* LockPubScreenList returns a List of PubScreenNode. After the
     * walk, UnlockPubScreenList. The names are in the nodes; we
     * don't need to LockPubScreen each one for just listing. */
    struct List *list = ii->LockPubScreenList();
    if (list) {
        for (struct Node *n = list->lh_Head;
             n->ln_Succ != NULL; n = n->ln_Succ) {
            cJSON *o = cJSON_CreateObject();
            cJSON_AddStringToObject(o, "name",
                sanitize_str(n->ln_Name, buf, sizeof(buf)));
            cJSON_AddNumberToObject(o, "priority", (double)n->ln_Pri);
            cJSON_AddItemToArray(arr, o);
        }
        ii->UnlockPubScreenList();
    }

    _release_intuition(ii, ibase);
    *out_result = arr;
    return 0;
}

/* ---- wb.frontmost ----------------------------------------------- */

int wb_frontmost(cJSON *params, cJSON **out_result, cJSON **out_err) {
    (void)params;

    struct Library *ibase = NULL;
    struct IntuitionIFace *ii = _obtain_intuition(&ibase, out_err);
    if (!ii) return 0;

    cJSON *r = cJSON_CreateObject();
    char buf[160];

    ULONG lock = ii->LockIBase(0);
    struct IntuitionBase *ib = (struct IntuitionBase *)ibase;
    struct Screen *s = ib->FirstScreen;  /* frontmost screen */
    struct Screen *active = ib->ActiveScreen;
    struct Window *aw = ib->ActiveWindow;

    if (s) {
        cJSON *fs = cJSON_AddObjectToObject(r, "frontmost_screen");
        cJSON_AddStringToObject(fs, "title",
            sanitize_str((const char *)s->Title, buf, sizeof(buf)));
        cJSON_AddNumberToObject(fs, "width", (double)s->Width);
        cJSON_AddNumberToObject(fs, "height", (double)s->Height);
    } else {
        cJSON_AddNullToObject(r, "frontmost_screen");
    }

    if (active) {
        cJSON *as = cJSON_AddObjectToObject(r, "active_screen");
        cJSON_AddStringToObject(as, "title",
            sanitize_str((const char *)active->Title, buf, sizeof(buf)));
    } else {
        cJSON_AddNullToObject(r, "active_screen");
    }

    if (aw) {
        cJSON *w = cJSON_AddObjectToObject(r, "active_window");
        cJSON_AddStringToObject(w, "title",
            sanitize_str((const char *)aw->Title, buf, sizeof(buf)));
        cJSON_AddNumberToObject(w, "width", (double)aw->Width);
        cJSON_AddNumberToObject(w, "height", (double)aw->Height);
        cJSON_AddNumberToObject(w, "left", (double)aw->LeftEdge);
        cJSON_AddNumberToObject(w, "top", (double)aw->TopEdge);
    } else {
        cJSON_AddNullToObject(r, "active_window");
    }

    ii->UnlockIBase(lock);
    _release_intuition(ii, ibase);
    *out_result = r;
    return 0;
}

/* ---- wb.windows -------------------------------------------------- */

int wb_windows(cJSON *params, cJSON **out_result, cJSON **out_err) {
    (void)params;

    struct Library *ibase = NULL;
    struct IntuitionIFace *ii = _obtain_intuition(&ibase, out_err);
    if (!ii) return 0;

    cJSON *arr = cJSON_CreateArray();
    char buf[160];
    char screen_buf[160];

    ULONG lock = ii->LockIBase(0);
    struct IntuitionBase *ib = (struct IntuitionBase *)ibase;
    int s_idx = 0;
    for (struct Screen *s = ib->FirstScreen; s != NULL; s = s->NextScreen) {
        sanitize_str((const char *)s->Title, screen_buf, sizeof(screen_buf));
        for (struct Window *w = s->FirstWindow; w != NULL; w = w->NextWindow) {
            cJSON *o = cJSON_CreateObject();
            cJSON_AddStringToObject(o, "title",
                sanitize_str((const char *)w->Title, buf, sizeof(buf)));
            cJSON_AddStringToObject(o, "screen", screen_buf);
            cJSON_AddNumberToObject(o, "screen_index", (double)s_idx);
            cJSON_AddNumberToObject(o, "left",   (double)w->LeftEdge);
            cJSON_AddNumberToObject(o, "top",    (double)w->TopEdge);
            cJSON_AddNumberToObject(o, "width",  (double)w->Width);
            cJSON_AddNumberToObject(o, "height", (double)w->Height);
            cJSON_AddNumberToObject(o, "flags",  (double)(unsigned int)w->Flags);
            cJSON_AddItemToArray(arr, o);
        }
        s_idx++;
    }
    ii->UnlockIBase(lock);

    _release_intuition(ii, ibase);
    *out_result = arr;
    return 0;
}
