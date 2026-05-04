/* sys.executable.symbols - read ELF symbols from a shipped binary
 * via elf.library. Returns name + value + size + binding/type per
 * symbol. Useful for confirming a debug build's symbol presence
 * and resolving instruction addresses to names without re-running. */

#include "../rpc.h"
#include "methods.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include <proto/exec.h>
#include <proto/elf.h>
#include <libraries/elf.h>
#include <utility/hooks.h>


struct _sym_ctx {
    cJSON *symbols;
    int count;
    int max;
    /* Optional name-substring filter; NULL = take everything. */
    const char *match;
    /* Optional binding/type filters; <0 = no filter. */
    int filter_type;
    int filter_binding;
};


/* ScanSymbolTable hook receives a SymbolMsg per symbol. */
static int _sym_hook(struct Hook *hook, Elf32_Handle h, struct SymbolMsg *m) {
    (void)h;
    struct _sym_ctx *c = (struct _sym_ctx *)hook->h_Data;
    if (c->count >= c->max) return 0;
    if (!m || !m->Sym || !m->Name) return 1;

    UBYTE info = m->Sym->st_info;
    int bind = (info >> 4) & 0xF;
    int type = info & 0xF;
    if (c->filter_binding >= 0 && bind != c->filter_binding) return 1;
    if (c->filter_type    >= 0 && type != c->filter_type)    return 1;
    if (c->match && !strstr(m->Name, c->match)) return 1;

    cJSON *e = cJSON_CreateObject();
    cJSON_AddStringToObject(e, "name", m->Name);
    cJSON_AddNumberToObject(e, "value", (double)(uint32)m->Sym->st_value);
    cJSON_AddNumberToObject(e, "size",  (double)(uint32)m->Sym->st_size);
    cJSON_AddNumberToObject(e, "abs_value", (double)(uint32)m->AbsValue);
    cJSON_AddNumberToObject(e, "binding", (double)bind);
    cJSON_AddNumberToObject(e, "type",    (double)type);
    static const char *bnames[] = {"local", "global", "weak"};
    static const char *tnames[] = {
        "notype", "object", "func", "section", "file"
    };
    if (bind < 3) cJSON_AddStringToObject(e, "binding_name", bnames[bind]);
    if (type < 5) cJSON_AddStringToObject(e, "type_name", tnames[type]);
    cJSON_AddItemToArray(c->symbols, e);
    c->count++;
    return 1;
}


int sys_executable_symbols(cJSON *params, cJSON **out_result, cJSON **out_err) {
    const char *path = p_str(params, "path", out_err);
    if (!path) return 0;

    long long max_ll = p_int(params, "max", 256);
    int max = (int)max_ll;
    if (max < 1) max = 1;
    if (max > 4096) max = 4096;

    /* Optional filters. */
    cJSON *m_node = cJSON_GetObjectItemCaseSensitive(params, "match");
    const char *match =
        (cJSON_IsString(m_node) && m_node->valuestring && m_node->valuestring[0])
        ? m_node->valuestring : NULL;

    /* Optional binding / type filters as strings. */
    int fbind = -1, ftype = -1;
    cJSON *b_node = cJSON_GetObjectItemCaseSensitive(params, "binding");
    if (cJSON_IsString(b_node) && b_node->valuestring) {
        if      (!strcmp(b_node->valuestring, "local"))  fbind = 0;
        else if (!strcmp(b_node->valuestring, "global")) fbind = 1;
        else if (!strcmp(b_node->valuestring, "weak"))   fbind = 2;
    }
    cJSON *t_node = cJSON_GetObjectItemCaseSensitive(params, "type");
    if (cJSON_IsString(t_node) && t_node->valuestring) {
        if      (!strcmp(t_node->valuestring, "notype"))  ftype = 0;
        else if (!strcmp(t_node->valuestring, "object"))  ftype = 1;
        else if (!strcmp(t_node->valuestring, "func"))    ftype = 2;
        else if (!strcmp(t_node->valuestring, "section")) ftype = 3;
        else if (!strcmp(t_node->valuestring, "file"))    ftype = 4;
    }

    struct Library *elfBase = (struct Library *)
        IExec->OpenLibrary("elf.library", 52);
    if (!elfBase) {
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
                                  "elf.library not available", NULL);
        return 0;
    }
    struct ElfIFace *Elf = (struct ElfIFace *)
        IExec->GetInterface(elfBase, "main", 1, NULL);
    if (!Elf) {
        IExec->CloseLibrary(elfBase);
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
                                  "elf.library main interface", NULL);
        return 0;
    }

    Elf32_Handle eh = Elf->OpenElfTags(OET_Filename, (Tag)path, TAG_END);
    if (!eh) {
        IExec->DropInterface((struct Interface *)Elf);
        IExec->CloseLibrary(elfBase);
        cJSON *data = cJSON_CreateObject();
        if (data) cJSON_AddStringToObject(data, "path", path);
        *out_err = rpc_make_error(MCPD_ERR_TARGET,
                                  "OpenElf failed", data);
        return 0;
    }

    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "path", path);

    /* Pull the section count + filename for the result envelope. */
    uint32 nsects = 0;
    Elf->GetElfAttrsTags(eh, EAT_NumSections, (Tag)&nsects, TAG_END);
    cJSON_AddNumberToObject(r, "num_sections", (double)nsects);

    cJSON *symbols = cJSON_AddArrayToObject(r, "symbols");
    struct _sym_ctx ctx = {
        .symbols = symbols, .count = 0, .max = max,
        .match = match, .filter_type = ftype, .filter_binding = fbind,
    };
    struct Hook hk;
    memset(&hk, 0, sizeof(hk));
    hk.h_Entry = (uint32(*)())_sym_hook;
    hk.h_Data  = &ctx;
    Elf->ScanSymbolTable(eh, &hk, NULL);

    cJSON_AddNumberToObject(r, "symbol_count", (double)ctx.count);
    cJSON_AddBoolToObject(r, "truncated", ctx.count >= max);

    Elf->CloseElfTags(eh, TAG_END);
    IExec->DropInterface((struct Interface *)Elf);
    IExec->CloseLibrary(elfBase);
    *out_result = r;
    return 0;
}
