/* input.* method handlers - keyboard + mouse injection via input.device.
 *
 * ============================================================
 * THIS FILE IS THE ONLY PLACE IN MCPd THAT *WRITES* TO THE UI.
 * ============================================================
 *
 * Every other method in this daemon observes the machine or touches
 * the filesystem. These methods type and click as the logged-in user,
 * into whatever window happens to be focused. That is categorically
 * more dangerous than the rest of the surface, so:
 *
 *   input injection is DISABLED BY DEFAULT and must be switched on
 *   by a deliberate operator action ON THE AMIGA ITSELF.
 *
 * See "The gate" below. SECURITY.md documents the operator-facing
 * side of this.
 *
 *
 * The gate
 * --------
 * Two enable sources, OR'd, both default off:
 *
 *   1. --enable-input on the MCPd command line.
 *   2. The sentinel file SYS:System/MCPd/ENABLE-INPUT existing.
 *
 * The sentinel is the PRIMARY mechanism, and this is deliberate:
 * MCPd-Watchdog relaunches MCPd with NO arguments, and MCPd-Install
 * appends an argument-less launch line to S:Network-Startup. A
 * CLI-flag-only gate would therefore be silently dropped on the first
 * watchdog restart -- an operator who enabled input would find it
 * mysteriously off later, which is the worst possible failure mode for
 * a security control (it fails *open* in the operator's mental model
 * even though it fails closed in fact). The sentinel survives restarts,
 * and MCPd-Uninstall's `Delete SYS:System/MCPd ALL` removes it for free.
 *
 * The gate is read ONCE at startup, before the accept loop, into a
 * static that is read-only thereafter. Two consequences, both wanted:
 *
 *   - No locking is needed. The per-connection child Processes
 *     (CreateNewProcTags, main.c) share the parent's data segment, so
 *     they all see the same value.
 *   - There is NO RUNTIME TOGGLE. No RPC method can turn injection on.
 *     Enabling it requires filesystem access to the target and an MCPd
 *     restart. A client that has compromised the RPC surface cannot
 *     escalate into input injection.
 *
 * Host-side config ([targets.<n>.input] enabled) is a convenience gate
 * only. Anything that can reach port 4322 bypasses the host entirely,
 * so the daemon must not trust it.
 *
 *
 * Conventions
 * -----------
 * Follows the house style: open libraries/devices per call and drop
 * them on exit (see wb.c), device I/O via AllocSysObjectTags with a
 * `goto cleanup` unwind (see mcu.c:66-130).
 */

#include "../rpc.h"
#include "methods.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include <proto/exec.h>
#include <proto/dos.h>
#include <proto/intuition.h>
#include <proto/keymap.h>
#include <proto/timer.h>
#include <intuition/intuition.h>
#include <intuition/intuitionbase.h>
#include <devices/input.h>
#include <devices/inputevent.h>
#include <devices/timer.h>
#include <exec/io.h>
#include <exec/ports.h>


/* NewMouse wheel codes. These are the de-facto AmigaOS wheel standard
 * that MUI and ReAction understand, delivered as IECLASS_RAWMOUSE
 * codes. They are NOT reliably present in <devices/inputevent.h> on
 * every SDK revision -- some ship them only in a third-party
 * newmouse.h -- so define them defensively rather than let a missing
 * header break the build. */
#ifndef NM_WHEEL_UP
#define NM_WHEEL_UP    0x7A
#endif
#ifndef NM_WHEEL_DOWN
#define NM_WHEEL_DOWN  0x7B
#endif
#ifndef NM_WHEEL_LEFT
#define NM_WHEEL_LEFT  0x7C
#endif
#ifndef NM_WHEEL_RIGHT
#define NM_WHEEL_RIGHT 0x7D
#endif


/* ---- caps ---------------------------------------------------------- *
 *
 * Enforced HERE, in the daemon. The host-side equivalents in
 * tools/input.py are advisory only -- a raw JSON-RPC client bypasses
 * them entirely, so these are the ones that matter.
 */
#define INPUT_MAX_TEXT        512   /* chars per input.type            */
#define INPUT_MAX_CHORD_KEYS    8   /* keys in one chord               */
#define INPUT_MAX_MOVE       4096   /* |dx| / |dy| per mouse_move      */
#define INPUT_MAX_DRAG_STEPS   64
#define INPUT_MAX_SCROLL       32   /* wheel clicks per call           */
#define INPUT_MAX_DELAY_MS   1000
#define INPUT_MAX_EVENTS      256   /* events per call                 */

/* Wall-clock ceiling per call. Without this, delay_ms=1000 with a
 * 512-char string would pin a connection Process for eight minutes --
 * and since MCPd serialises requests per connection, that is a
 * self-inflicted denial of service on the client's own channel. */
#define INPUT_MAX_BUDGET_MS 20000

/* Default pacing. Injecting events back-to-back with no delay tends to
 * get them coalesced or dropped -- Intuition and the input handler
 * chain are not expecting a burst faster than a human hand. */
#define INPUT_DEF_KEY_MS      15   /* between a key down and its up    */
#define INPUT_DEF_CHAR_MS     25   /* between characters               */
#define INPUT_DEF_MOVE_MS     10   /* between mouse motion steps       */
#define INPUT_DEF_BUTTON_MS   50   /* around press/release in a drag   */


/* Sentinel file. Lives beside the MCPd binary so MCPd-Uninstall's
 * recursive delete takes it with everything else. */
#define INPUT_SENTINEL "SYS:System/MCPd/ENABLE-INPUT"

/* How to turn it on. Repeated in the error payload of every gated
 * method so a client sees the remedy without reading the docs. */
#define INPUT_ENABLE_HINT \
    "start MCPd with --enable-input, or create " INPUT_SENTINEL \
    " on the target and restart MCPd"


/* ---- the gate ----------------------------------------------------- *
 *
 * Written exactly once by input_init_gate() from main(), before any
 * connection Process exists. Read-only afterwards -- do not add a
 * setter.
 */
static int g_input_enabled = 0;


int input_is_enabled(void) {
    return g_input_enabled;
}


/* Does the sentinel file exist? Uses a shared lock so we don't disturb
 * anything else holding it. Any failure to Lock (missing file, missing
 * volume, unreadable) reads as "not enabled" -- the gate fails closed. */
static int _sentinel_present(void) {
    BPTR lock = IDOS->Lock(INPUT_SENTINEL, SHARED_LOCK);
    if (!lock) return 0;
    IDOS->UnLock(lock);
    return 1;
}


/* Call ONCE from main() before the accept loop. cli_flag is 1 if
 * --enable-input was passed. Prints the startup banner. */
void input_init_gate(int cli_flag) {
    int sentinel = _sentinel_present();
    g_input_enabled = (cli_flag || sentinel) ? 1 : 0;

    if (g_input_enabled) {
        /* Deliberately loud. An operator who enabled this by accident
         * -- or an operator who inherited a machine someone else
         * enabled it on -- should find out at boot, not by watching
         * the pointer move on its own. */
        IDOS->Printf(
            "MCPd: *** INPUT INJECTION ENABLED *** (%s)\n"
            "MCPd: remote clients can type and click on this machine.\n",
            cli_flag ? (sentinel ? "--enable-input + sentinel file"
                                 : "--enable-input")
                     : "sentinel file");
    } else {
        IDOS->Printf(
            "MCPd: input injection disabled (input.* -> -32003).\n"
            "MCPd: to enable: %s\n", INPUT_ENABLE_HINT);
    }
}


/* First statement of every input.* handler. Returns -32003 with a
 * machine-readable reason and the remedy. */
#define INPUT_GATE()                                                     \
    do {                                                                 \
        if (!input_is_enabled()) {                                       \
            cJSON *_d = cJSON_CreateObject();                            \
            if (_d) {                                                    \
                cJSON_AddStringToObject(_d, "reason", "input_disabled"); \
                cJSON_AddStringToObject(_d, "enable", INPUT_ENABLE_HINT);\
            }                                                            \
            *out_err = rpc_make_error(MCPD_ERR_NOTCAPABLE,               \
                "input injection is disabled on this daemon", _d);       \
            return 0;                                                    \
        }                                                                \
    } while (0)


/* ---- shared helpers ----------------------------------------------- */

/* Same treatment wb.c gives strings bound for JSON: printable ASCII
 * only, everything else becomes '?'. Window titles are attacker-
 * influenced (any app can set one), so they never reach the client raw. */
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


/* Open intuition.library + IIntuition for the duration of one call.
 * Mirrors wb.c's _obtain_intuition. */
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


/* ---- rawkey tables ------------------------------------------------- *
 *
 * The standard Amiga rawkey matrix. These codes are positional (they
 * identify a physical key, not a character), which is why typing a
 * *character* needs a keymap -- see _ascii_to_raw() below.
 */

/* Modifier keys. */
#define RAW_LSHIFT   0x60
#define RAW_RSHIFT   0x61
#define RAW_CAPSLOCK 0x62
#define RAW_CTRL     0x63
#define RAW_LALT     0x64
#define RAW_RALT     0x65
#define RAW_LAMIGA   0x66
#define RAW_RAMIGA   0x67

typedef struct { const char *name; uint8_t code; uint16_t qual; } named_key;

/* Named keys accepted by input.key. `qual` is non-zero only for the
 * modifiers -- pressing one both emits its rawkey event AND contributes
 * its qualifier bit to subsequent events in the chord. */
static const named_key g_named_keys[] = {
    /* modifiers */
    { "lshift",   RAW_LSHIFT,   IEQUALIFIER_LSHIFT   },
    { "rshift",   RAW_RSHIFT,   IEQUALIFIER_RSHIFT   },
    { "shift",    RAW_LSHIFT,   IEQUALIFIER_LSHIFT   },
    { "capslock", RAW_CAPSLOCK, IEQUALIFIER_CAPSLOCK },
    { "ctrl",     RAW_CTRL,     IEQUALIFIER_CONTROL  },
    { "control",  RAW_CTRL,     IEQUALIFIER_CONTROL  },
    { "lalt",     RAW_LALT,     IEQUALIFIER_LALT     },
    { "ralt",     RAW_RALT,     IEQUALIFIER_RALT     },
    { "alt",      RAW_LALT,     IEQUALIFIER_LALT     },
    { "lamiga",   RAW_LAMIGA,   IEQUALIFIER_LCOMMAND },
    { "lcommand", RAW_LAMIGA,   IEQUALIFIER_LCOMMAND },
    { "ramiga",   RAW_RAMIGA,   IEQUALIFIER_RCOMMAND },
    { "rcommand", RAW_RAMIGA,   IEQUALIFIER_RCOMMAND },
    { "amiga",    RAW_LAMIGA,   IEQUALIFIER_LCOMMAND },

    /* editing / navigation */
    { "space",     0x40, 0 },
    { "backspace", 0x41, 0 },
    { "tab",       0x42, 0 },
    { "return",    0x44, 0 },
    { "enter",     0x44, 0 },
    { "esc",       0x45, 0 },
    { "escape",    0x45, 0 },
    { "del",       0x46, 0 },
    { "delete",    0x46, 0 },
    { "help",      0x5F, 0 },
    { "up",        0x4C, 0 },
    { "down",      0x4D, 0 },
    { "right",     0x4E, 0 },
    { "left",      0x4F, 0 },

    /* function keys. F1..F10 are the classic Amiga block and are
     * solid. F11/F12 are DELIBERATELY ABSENT: they are a PC-keyboard
     * extension whose AOS4 rawkey codes were never verified on
     * hardware for this table, and an unknown key name fails cleanly
     * with "unknown key name" while a wrong code would silently press
     * some other key. Add them only with a hardware check behind it. */
    { "f1",  0x50, 0 }, { "f2",  0x51, 0 }, { "f3",  0x52, 0 },
    { "f4",  0x53, 0 }, { "f5",  0x54, 0 }, { "f6",  0x55, 0 },
    { "f7",  0x56, 0 }, { "f8",  0x57, 0 }, { "f9",  0x58, 0 },
    { "f10", 0x59, 0 },

    /* numeric keypad */
    { "kp0", 0x0F, 0 }, { "kp1", 0x1D, 0 }, { "kp2", 0x1E, 0 },
    { "kp3", 0x1F, 0 }, { "kp4", 0x2D, 0 }, { "kp5", 0x2E, 0 },
    { "kp6", 0x2F, 0 }, { "kp7", 0x3D, 0 }, { "kp8", 0x3E, 0 },
    { "kp9", 0x3F, 0 },
    { "kpdot",   0x3C, 0 }, { "kpenter", 0x43, 0 },
    { "kpminus", 0x4A, 0 }, { "kpplus",  0x5E, 0 },
    { "kpmul",   0x5D, 0 }, { "kpdiv",   0x5C, 0 },
    { "kplparen", 0x5A, 0 }, { "kprparen", 0x5B, 0 },

    { NULL, 0, 0 }
};


/* ASCII -> (rawkey, needs-shift) for the US layout.
 *
 * ------------------------------------------------------------------
 * THIS IS THE FALLBACK PATH, NOT THE DEFAULT.
 *
 * input.type maps characters through keymap.library's MapANSI()
 * against the system's configured keymap, which is layout-aware --
 * see input_type() below. This table is used only when
 * keymap.library cannot be opened, or when the caller explicitly
 * passes keymap="us".
 *
 * Being a US matrix, it produces the WRONG CHARACTERS on a machine
 * configured for another layout: a German keymap turns 'y' into 'z'
 * and moves every symbol key. The result's `keymap` field reports
 * which of the two paths actually ran, so a caller that cares can
 * check rather than assume. input.key with explicit named keys is
 * the layout-independent escape hatch in either case.
 * ------------------------------------------------------------------
 */
typedef struct { uint8_t code; uint8_t shift; } ascii_key;

#define NOKEY 0xFF

static const ascii_key g_us_ascii[96] = {
    /* 0x20 */ {0x40,0}, {0x01,1}, {0x2A,1}, {0x03,1},
    /* $%&' */ {0x04,1}, {0x05,1}, {0x07,1}, {0x2A,0},
    /* ()*+ */ {0x09,1}, {0x0A,1}, {0x08,1}, {0x0C,1},
    /* ,-./ */ {0x38,0}, {0x0B,0}, {0x39,0}, {0x3A,0},
    /* 0123 */ {0x0A,0}, {0x01,0}, {0x02,0}, {0x03,0},
    /* 4567 */ {0x04,0}, {0x05,0}, {0x06,0}, {0x07,0},
    /* 89:; */ {0x08,0}, {0x09,0}, {0x29,1}, {0x29,0},
    /* <=>? */ {0x38,1}, {0x0C,0}, {0x39,1}, {0x3A,1},
    /* @ABC */ {0x02,1}, {0x20,1}, {0x35,1}, {0x33,1},
    /* DEFG */ {0x22,1}, {0x12,1}, {0x23,1}, {0x24,1},
    /* HIJK */ {0x25,1}, {0x17,1}, {0x26,1}, {0x27,1},
    /* LMNO */ {0x28,1}, {0x37,1}, {0x36,1}, {0x18,1},
    /* PQRS */ {0x19,1}, {0x10,1}, {0x13,1}, {0x21,1},
    /* TUVW */ {0x14,1}, {0x16,1}, {0x34,1}, {0x11,1},
    /* XYZ[ */ {0x32,1}, {0x15,1}, {0x31,1}, {0x1A,0},
    /* \]^_ */ {0x0D,0}, {0x1B,0}, {0x06,1}, {0x0B,1},
    /* `abc */ {0x00,0}, {0x20,0}, {0x35,0}, {0x33,0},
    /* defg */ {0x22,0}, {0x12,0}, {0x23,0}, {0x24,0},
    /* hijk */ {0x25,0}, {0x17,0}, {0x26,0}, {0x27,0},
    /* lmno */ {0x28,0}, {0x37,0}, {0x36,0}, {0x18,0},
    /* pqrs */ {0x19,0}, {0x10,0}, {0x13,0}, {0x21,0},
    /* tuvw */ {0x14,0}, {0x16,0}, {0x34,0}, {0x11,0},
    /* xyz{ */ {0x32,0}, {0x15,0}, {0x31,0}, {0x1A,1},
    /* |}~  */ {0x0D,1}, {0x1B,1}, {0x00,1}, {NOKEY,0},
};

/* ---- UTF-8 -> codepoint ------------------------------------------- *
 *
 * The wire is UTF-8. The host sends
 * `json.dumps(env, ensure_ascii=False).encode("utf-8")`
 * (transports/mcpd.py), and cJSON decodes any \uXXXX escape into UTF-8
 * too, so a non-ASCII character reaches us as TWO OR MORE BYTES.
 *
 * MapANSI, by contrast, wants ONE ANSI (ISO-8859-1) byte per
 * character. Walking the string byte-by-byte therefore typed 0xC3
 * followed by 0xA9 for a single "e-acute" -- two wrong keystrokes, or
 * two entries in unmapped[], for one requested character. Decode
 * first, then map the codepoint.
 *
 * Returns the number of bytes consumed (always >= 1) and stores the
 * codepoint. A malformed or truncated sequence consumes exactly one
 * byte and yields that raw byte, so bad input is reported through
 * unmapped[] rather than resynchronising into something typeable.
 */
static int _utf8_next(const char *s, size_t len, size_t i, uint32_t *out_cp) {
    unsigned char c0 = (unsigned char)s[i];
    int need;
    uint32_t cp;

    if (c0 < 0x80)            { *out_cp = c0; return 1; }
    else if ((c0 & 0xE0) == 0xC0) { need = 1; cp = c0 & 0x1Fu; }
    else if ((c0 & 0xF0) == 0xE0) { need = 2; cp = c0 & 0x0Fu; }
    else if ((c0 & 0xF8) == 0xF0) { need = 3; cp = c0 & 0x07u; }
    else                      { *out_cp = c0; return 1; }

    if (i + (size_t)need >= len) {   /* truncated at end of buffer */
        *out_cp = c0;
        return 1;
    }
    for (int k = 1; k <= need; k++) {
        unsigned char cn = (unsigned char)s[i + (size_t)k];
        if ((cn & 0xC0) != 0x80) { *out_cp = c0; return 1; }
        cp = (cp << 6) | (uint32_t)(cn & 0x3Fu);
    }
    *out_cp = cp;
    return need + 1;
}


/* Count characters, not bytes. The caller's 512-character budget was
 * counted in characters, so enforcing it on the byte length would
 * reject a perfectly legal 300-character accented string. */
static size_t _utf8_strlen(const char *s, size_t len) {
    size_t n = 0, i = 0;
    while (i < len) {
        uint32_t cp;
        i += (size_t)_utf8_next(s, len, i, &cp);
        n++;
    }
    return n;
}


/* Newline and tab are the two control characters worth honouring in a
 * typed string; everything else non-printable is rejected. */
static int _ascii_to_raw(unsigned char c, uint8_t *out_code, int *out_shift) {
    if (c == '\n' || c == '\r') { *out_code = 0x44; *out_shift = 0; return 1; }
    if (c == '\t')              { *out_code = 0x42; *out_shift = 0; return 1; }
    if (c < 0x20 || c > 0x7E)   return 0;
    const ascii_key *k = &g_us_ascii[c - 0x20];
    if (k->code == NOKEY) return 0;
    *out_code = k->code;
    *out_shift = k->shift;
    return 1;
}


static const named_key *_find_named_key(const char *name) {
    for (const named_key *k = g_named_keys; k->name != NULL; k++) {
        /* Case-insensitive without depending on locale-sensitive
         * tolower() from clib4. */
        const char *a = k->name, *b = name;
        while (*a && *b) {
            char ca = *a, cb = *b;
            if (cb >= 'A' && cb <= 'Z') cb = (char)(cb - 'A' + 'a');
            if (ca != cb) break;
            a++; b++;
        }
        if (*a == '\0' && *b == '\0') return k;
    }
    return NULL;
}


/* ---- injection context -------------------------------------------- */

typedef struct {
    struct MsgPort      *port;
    struct IOStdReq     *io;
    int                  io_open;

    struct MsgPort      *tport;
    struct TimeRequest  *tio;
    int                  tio_open;
    struct TimerIFace   *itimer;

    /* keymap.library, for layout-correct typing. NULL if it could not
     * be opened, in which case input.type falls back to the built-in
     * US table (and says so in the result's `keymap` field). */
    struct Library      *kbase;
    struct KeymapIFace  *ikeymap;

    /* The event buffer. ONE allocation per call, reused for every
     * event, in PUBLIC memory (MEMF_SHARED) because IND_WRITEEVENT
     * hands the pointer down the whole input-handler chain -- Intuition,
     * Commodities, anything else installed. A stack buffer here would
     * be a use-after-scope waiting to happen. Freed only in
     * _inject_close(), after a settle delay. */
    struct InputEvent   *ev;

    /* State we are responsible for undoing. See _inject_close(). */
    uint8_t  held_mods[INPUT_MAX_CHORD_KEYS];
    int      n_held_mods;
    uint16_t held_quals;
    uint16_t held_buttons;   /* IEQUALIFIER_*BUTTON bits             */
    uint8_t  held_button_code; /* IECODE_LBUTTON etc, 0 if none      */

    int events;
    int spent_ms;
    int truncated;
} inject_ctx;


static void _delay_ms(inject_ctx *c, int ms) {
    if (ms <= 0 || !c->tio_open) return;
    c->tio->Request.io_Command = TR_ADDREQUEST;
    c->tio->Time.Seconds      = (uint32)(ms / 1000);
    c->tio->Time.Microseconds = (uint32)((ms % 1000) * 1000);
    IExec->DoIO((struct IORequest *)c->tio);
    c->spent_ms += ms;
}


/* Emit whatever is currently in c->ev. Returns 0 on success, -1 if a
 * budget was hit (caller must stop; ctx is marked truncated). */
static int _write_event(inject_ctx *c) {
    if (c->events >= INPUT_MAX_EVENTS || c->spent_ms >= INPUT_MAX_BUDGET_MS) {
        c->truncated = 1;
        return -1;
    }

    /* Intuition uses the timestamp for double-click detection and key
     * repeat. Leaving it zeroed makes every click look simultaneous,
     * which turns a sequence of single clicks into an accidental
     * multi-click. */
    if (c->itimer) c->itimer->GetSysTime(&c->ev->ie_TimeStamp);

    c->ev->ie_NextEvent = NULL;
    c->io->io_Command = IND_WRITEEVENT;
    c->io->io_Data    = c->ev;
    c->io->io_Length  = sizeof(struct InputEvent);
    IExec->DoIO((struct IORequest *)c->io);

    c->events++;
    return 0;
}


static void _ev_reset(inject_ctx *c) {
    memset(c->ev, 0, sizeof(struct InputEvent));
}


/* Press or release one rawkey. `qual` is the full qualifier state that
 * should accompany the event (held modifiers + any extras). */
static int _key_event(inject_ctx *c, uint8_t code, int up, uint16_t qual) {
    _ev_reset(c);
    c->ev->ie_Class     = IECLASS_RAWKEY;
    c->ev->ie_Code      = up ? (UWORD)(code | IECODE_UP_PREFIX) : (UWORD)code;
    c->ev->ie_Qualifier = qual;
    return _write_event(c);
}


/* Press a modifier and remember it, so _inject_close() can let go of it
 * even if we bail out early. */
static int _push_modifier(inject_ctx *c, const named_key *k) {
    if (c->n_held_mods >= INPUT_MAX_CHORD_KEYS) return -1;
    if (_key_event(c, k->code, 0, c->held_quals) != 0) return -1;
    c->held_quals |= k->qual;
    c->held_mods[c->n_held_mods++] = k->code;
    return 0;
}


/* Release held modifiers in reverse order. Best-effort: this runs on
 * the failure path too, so it must not itself be able to fail out. */
static void _release_modifiers(inject_ctx *c) {
    while (c->n_held_mods > 0) {
        uint8_t code = c->held_mods[--c->n_held_mods];
        /* Bypass _write_event's budget check -- releasing a stuck key
         * is exactly what we must still do once the budget is blown. */
        _ev_reset(c);
        c->ev->ie_Class     = IECLASS_RAWKEY;
        c->ev->ie_Code      = (UWORD)(code | IECODE_UP_PREFIX);
        c->ev->ie_Qualifier = c->held_quals;
        c->ev->ie_NextEvent = NULL;
        if (c->itimer) c->itimer->GetSysTime(&c->ev->ie_TimeStamp);
        c->io->io_Command = IND_WRITEEVENT;
        c->io->io_Data    = c->ev;
        c->io->io_Length  = sizeof(struct InputEvent);
        IExec->DoIO((struct IORequest *)c->io);
    }
    c->held_quals = 0;
}


static void _release_buttons(inject_ctx *c) {
    if (!c->held_button_code) return;
    _ev_reset(c);
    c->ev->ie_Class     = IECLASS_RAWMOUSE;
    c->ev->ie_Code      = (UWORD)(c->held_button_code | IECODE_UP_PREFIX);
    c->ev->ie_Qualifier = IEQUALIFIER_RELATIVEMOUSE;
    c->ev->ie_X = 0;
    c->ev->ie_Y = 0;
    c->ev->ie_NextEvent = NULL;
    if (c->itimer) c->itimer->GetSysTime(&c->ev->ie_TimeStamp);
    c->io->io_Command = IND_WRITEEVENT;
    c->io->io_Data    = c->ev;
    c->io->io_Length  = sizeof(struct InputEvent);
    IExec->DoIO((struct IORequest *)c->io);
    c->held_button_code = 0;
    c->held_buttons = 0;
}


static int _inject_open(inject_ctx *c, cJSON **out_err) {
    memset(c, 0, sizeof(*c));

    c->port  = (struct MsgPort *)IExec->AllocSysObjectTags(ASOT_PORT, TAG_END);
    c->tport = (struct MsgPort *)IExec->AllocSysObjectTags(ASOT_PORT, TAG_END);
    if (!c->port || !c->tport) {
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
            "AllocSysObject(MsgPort) failed", NULL);
        return -1;
    }

    c->io = (struct IOStdReq *)IExec->AllocSysObjectTags(ASOT_IOREQUEST,
        ASOIOR_Size,      sizeof(struct IOStdReq),
        ASOIOR_ReplyPort, c->port,
        TAG_END);
    c->tio = (struct TimeRequest *)IExec->AllocSysObjectTags(ASOT_IOREQUEST,
        ASOIOR_Size,      sizeof(struct TimeRequest),
        ASOIOR_ReplyPort, c->tport,
        TAG_END);
    if (!c->io || !c->tio) {
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
            "AllocSysObject(IORequest) failed", NULL);
        return -1;
    }

    /* input.device is always present on a running AOS4 system, so a
     * failure here means something is badly wrong -- INTERNAL, not
     * NOTCAPABLE. NOTCAPABLE is reserved for the gate, so the host can
     * tell "switched off" apart from "broken". */
    if (IExec->OpenDevice("input.device", 0,
                          (struct IORequest *)c->io, 0) != 0) {
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
            "OpenDevice(input.device) failed", NULL);
        return -1;
    }
    c->io_open = 1;

    /* UNIT_MICROHZ, not UNIT_VBLANK: IDOS->Delay() and the VBLANK unit
     * are 20ms-granular, which is far too coarse for the 10-25ms
     * inter-event pacing this code depends on. */
    if (IExec->OpenDevice("timer.device", UNIT_MICROHZ,
                          (struct IORequest *)c->tio, 0) == 0) {
        c->tio_open = 1;
        c->itimer = (struct TimerIFace *)IExec->GetInterface(
            (struct Library *)c->tio->Request.io_Device, "main", 1, NULL);
    }
    /* Timer is best-effort: without it we lose pacing and timestamps
     * but can still inject. Not worth failing the whole call. */

    /* keymap.library is best-effort: without it input.type falls back
     * to the built-in US table, which is wrong on a non-US layout but
     * better than refusing to type at all. The result reports which
     * path was actually used. */
    c->kbase = IExec->OpenLibrary("keymap.library", 53);
    if (c->kbase) {
        c->ikeymap = (struct KeymapIFace *)
            IExec->GetInterface(c->kbase, "main", 1, NULL);
        if (!c->ikeymap) {
            IExec->CloseLibrary(c->kbase);
            c->kbase = NULL;
        }
    }

    c->ev = (struct InputEvent *)IExec->AllocVecTags(
        sizeof(struct InputEvent),
        AVT_Type,            MEMF_SHARED,
        AVT_ClearWithValue,  0,
        TAG_END);
    if (!c->ev) {
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
            "AllocVecTags(InputEvent, MEMF_SHARED) failed", NULL);
        return -1;
    }

    return 0;
}


/* Tear down. MUST be reached on every path, including errors.
 *
 * The release-held-state step is the important part. A call that dies
 * halfway through input.drag with the left button still down, or
 * halfway through a chord with Ctrl still held, leaves the machine
 * effectively unusable until someone walks over and touches the real
 * mouse -- on an unattended lab box that means a trip to the machine
 * room. This is the single most likely way this feature ruins
 * somebody's afternoon, so it is unconditional. */
static void _inject_close(inject_ctx *c) {
    if (c->io_open && c->ev) {
        _release_buttons(c);
        _release_modifiers(c);
        /* Settle before freeing the event buffer: the pointer has just
         * been handed down the input-handler chain. */
        _delay_ms(c, 20);
    }

    if (c->ikeymap) IExec->DropInterface((struct Interface *)c->ikeymap);
    if (c->kbase)   IExec->CloseLibrary(c->kbase);
    if (c->itimer) IExec->DropInterface((struct Interface *)c->itimer);
    if (c->tio_open) IExec->CloseDevice((struct IORequest *)c->tio);
    if (c->io_open)  IExec->CloseDevice((struct IORequest *)c->io);
    if (c->tio)   IExec->FreeSysObject(ASOT_IOREQUEST, c->tio);
    if (c->io)    IExec->FreeSysObject(ASOT_IOREQUEST, c->io);
    if (c->tport) IExec->FreeSysObject(ASOT_PORT, c->tport);
    if (c->port)  IExec->FreeSysObject(ASOT_PORT, c->port);
    if (c->ev)    IExec->FreeVec(c->ev);

    /* Clear the handles so a double close is harmless -- but PRESERVE
     * the statistics. Callers build their JSON result after closing
     * (mouse_move deliberately re-reads the pointer only once the
     * events have settled), so a blanket memset here silently zeroed
     * every reported `events` / `duration_ms` and, worse, always
     * reported truncated:false -- making a budget overrun look like a
     * clean run. */
    int events = c->events;
    int spent = c->spent_ms;
    int trunc = c->truncated;
    memset(c, 0, sizeof(*c));
    c->events = events;
    c->spent_ms = spent;
    c->truncated = trunc;
}


/* Common tail: attach the bookkeeping every input.* result carries. */
static void _add_common(cJSON *r, inject_ctx *c) {
    cJSON_AddNumberToObject(r, "events", (double)c->events);
    cJSON_AddNumberToObject(r, "duration_ms", (double)c->spent_ms);
    cJSON_AddBoolToObject(r, "truncated", c->truncated ? 1 : 0);
}


/* Shared param validation for delay_ms. */
static int _get_delay(cJSON *params, int dflt, int *out, cJSON **out_err) {
    long long d = p_int(params, "delay_ms", dflt);
    if (d < 0 || d > INPUT_MAX_DELAY_MS) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "delay_ms out of range [0..1000]", NULL);
        return -1;
    }
    *out = (int)d;
    return 0;
}


/* Require confirm:true. Same accidental-fire guard as sys.cold_reboot
 * and sys.mcu_cmd cmd="s" -- defence in depth against raw JSON-RPC
 * callers that bypass the typed host wrapper. NOT authentication: a
 * connected client can always pass it. The real control is the gate. */
static int _require_confirm(cJSON *params, const char *what, cJSON **out_err) {
    cJSON *confirm = cJSON_GetObjectItemCaseSensitive(params, "confirm");
    if (!cJSON_IsBool(confirm) || !cJSON_IsTrue(confirm)) {
        char msg[128];
        snprintf(msg, sizeof(msg),
                 "%s requires confirm:true (injects real input)", what);
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS, msg, NULL);
        return -1;
    }
    return 0;
}


/* Read the frontmost screen's pointer position and dimensions.
 * Returns 0 on success. Opens/closes intuition itself. */
static int _pointer_pos(int *x, int *y, int *w, int *h, cJSON **out_err) {
    struct Library *ibase = NULL;
    struct IntuitionIFace *ii = _obtain_intuition(&ibase, out_err);
    if (!ii) return -1;

    ULONG lock = ii->LockIBase(0);
    struct IntuitionBase *ib = (struct IntuitionBase *)ibase;
    struct Screen *s = ib->FirstScreen;
    int ok = 0;
    if (s) {
        *x = s->MouseX; *y = s->MouseY;
        *w = s->Width;  *h = s->Height;
        ok = 1;
    }
    ii->UnlockIBase(lock);
    _release_intuition(ii, ibase);

    if (!ok) {
        *out_err = rpc_make_error(MCPD_ERR_INTERNAL,
            "no screen open; cannot determine pointer position", NULL);
        return -1;
    }
    return 0;
}


/* ---- input.state -------------------------------------------------- *
 *
 * Read-only. Gated like the rest so that a disabled daemon gives one
 * consistent answer everywhere rather than leaking UI state through a
 * side door.
 *
 * This is the "look before you click" method: an agent calls it to
 * learn where the pointer is and what is focused BEFORE committing to
 * an input.click. That is a far more useful safety property than an
 * idle-detection heuristic would be, and unlike idle detection it is
 * implementable with supported APIs.
 *
 * Deliberately NOT included: "seconds since last real user input".
 * There is no supported AOS4 API for it short of MCPd installing its
 * own commodities input handler, which is a much larger change with
 * its own risks. Omitted rather than approximated.
 */
int input_state(cJSON *params, cJSON **out_result, cJSON **out_err) {
    (void)params;
    INPUT_GATE();

    struct Library *ibase = NULL;
    struct IntuitionIFace *ii = _obtain_intuition(&ibase, out_err);
    if (!ii) return 0;

    cJSON *r = cJSON_CreateObject();
    char buf[160];

    ULONG lock = ii->LockIBase(0);
    struct IntuitionBase *ib = (struct IntuitionBase *)ibase;
    struct Screen *front = ib->FirstScreen;
    struct Screen *active = ib->ActiveScreen;
    struct Window *aw = ib->ActiveWindow;

    if (front) {
        /* Pointer position is per-screen and lives on the frontmost
         * screen -- the same one a synthetic mouse event will act on. */
        cJSON_AddNumberToObject(r, "pointer_x", (double)front->MouseX);
        cJSON_AddNumberToObject(r, "pointer_y", (double)front->MouseY);
        cJSON_AddNumberToObject(r, "screen_width", (double)front->Width);
        cJSON_AddNumberToObject(r, "screen_height", (double)front->Height);
        cJSON_AddStringToObject(r, "frontmost_screen",
            sanitize_str((const char *)front->Title, buf, sizeof(buf)));
    } else {
        cJSON_AddNullToObject(r, "pointer_x");
        cJSON_AddNullToObject(r, "pointer_y");
        cJSON_AddNullToObject(r, "screen_width");
        cJSON_AddNullToObject(r, "screen_height");
        cJSON_AddNullToObject(r, "frontmost_screen");
    }

    if (active) {
        cJSON_AddStringToObject(r, "active_screen",
            sanitize_str((const char *)active->Title, buf, sizeof(buf)));
    } else {
        cJSON_AddNullToObject(r, "active_screen");
    }

    if (aw) {
        cJSON_AddStringToObject(r, "active_window",
            sanitize_str((const char *)aw->Title, buf, sizeof(buf)));
        cJSON_AddNumberToObject(r, "active_window_left",   (double)aw->LeftEdge);
        cJSON_AddNumberToObject(r, "active_window_top",    (double)aw->TopEdge);
        cJSON_AddNumberToObject(r, "active_window_width",  (double)aw->Width);
        cJSON_AddNumberToObject(r, "active_window_height", (double)aw->Height);
    } else {
        cJSON_AddNullToObject(r, "active_window");
    }

    ii->UnlockIBase(lock);
    _release_intuition(ii, ibase);

    /* Always true here -- INPUT_GATE() would have returned otherwise.
     * Present so the field exists unconditionally in the schema and a
     * client can read one shape regardless of how it got the object. */
    cJSON_AddBoolToObject(r, "enabled", 1);

    *out_result = r;
    return 0;
}


/* ---- input.key ----------------------------------------------------- *
 *
 * Chord injection. params:
 *   keys      : ["lamiga", "q"]  -- all but the last are modifiers held
 *                                   down; the last is pressed+released
 *   delay_ms  : optional pacing
 *   confirm   : REQUIRED
 *
 * The host normalises the "lamiga+q" string shorthand into `keys`
 * before it gets here, so the daemon implements exactly one form.
 *
 * Unlike input.type, this emits faithful modifier down/up events rather
 * than just setting the qualifier -- chords are precisely where
 * applications watch modifier keys directly (menu shortcuts, Amiga-Q),
 * so fidelity matters more than event economy.
 */
int input_key(cJSON *params, cJSON **out_result, cJSON **out_err) {
    INPUT_GATE();
    if (_require_confirm(params, "input.key", out_err) != 0) return 0;

    cJSON *keys = cJSON_GetObjectItemCaseSensitive(params, "keys");
    if (!cJSON_IsArray(keys)) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "keys must be an array of key names, e.g. [\"lamiga\",\"q\"]",
            NULL);
        return 0;
    }
    int n = cJSON_GetArraySize(keys);
    if (n < 1 || n > INPUT_MAX_CHORD_KEYS) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "keys must contain 1..8 entries", NULL);
        return 0;
    }

    int delay;
    if (_get_delay(params, INPUT_DEF_KEY_MS, &delay, out_err) != 0) return 0;

    /* Resolve every key BEFORE opening the device, so a typo'd key name
     * fails cleanly without having injected half a chord. */
    const named_key *resolved[INPUT_MAX_CHORD_KEYS];
    uint8_t  final_code = 0;
    int      final_shift = 0;
    for (int i = 0; i < n; i++) {
        cJSON *e = cJSON_GetArrayItem(keys, i);
        if (!cJSON_IsString(e) || !e->valuestring) {
            *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
                "keys entries must be strings", NULL);
            return 0;
        }
        const char *name = e->valuestring;
        const named_key *k = _find_named_key(name);

        if (i < n - 1) {
            /* Everything but the last must be a modifier -- otherwise
             * "keys" would silently mean something different from what
             * the caller drew in their head. */
            if (!k || k->qual == 0) {
                char msg[160];
                snprintf(msg, sizeof(msg),
                    "keys[%d] (\"%s\") must be a modifier "
                    "(shift/ctrl/alt/lamiga/ramiga/capslock); only the "
                    "last entry may be a regular key", i, name);
                *out_err = rpc_make_error(MCPD_ERR_INVPARAMS, msg, NULL);
                return 0;
            }
            resolved[i] = k;
        } else {
            if (k) {
                final_code = k->code;
                final_shift = 0;
            } else if (name[0] && name[1] == '\0'
                       && _ascii_to_raw((unsigned char)name[0],
                                        &final_code, &final_shift)) {
                /* Single printable character -- route through the same
                 * table input.type uses so ["lamiga","q"] works. */
            } else {
                char msg[160];
                snprintf(msg, sizeof(msg),
                    "unknown key name \"%s\" (expected a named key such "
                    "as f1/esc/return/up, or a single character)", name);
                *out_err = rpc_make_error(MCPD_ERR_INVPARAMS, msg, NULL);
                return 0;
            }
        }
    }

    /* Ctrl-Amiga-Amiga is the AmigaOS three-finger salute: it resets
     * the machine. Reachable through this API by construction, so it
     * gets its own explicit acknowledgement rather than riding on the
     * generic confirm. */
    {
        int has_ctrl = 0, has_lamiga = 0, has_ramiga = 0;
        for (int i = 0; i < n - 1; i++) {
            if (resolved[i]->code == RAW_CTRL)   has_ctrl = 1;
            if (resolved[i]->code == RAW_LAMIGA) has_lamiga = 1;
            if (resolved[i]->code == RAW_RAMIGA) has_ramiga = 1;
        }
        if (final_code == RAW_CTRL)   has_ctrl = 1;
        if (final_code == RAW_LAMIGA) has_lamiga = 1;
        if (final_code == RAW_RAMIGA) has_ramiga = 1;

        if (has_ctrl && has_lamiga && has_ramiga) {
            cJSON *cr = cJSON_GetObjectItemCaseSensitive(params,
                                                         "confirm_reset");
            if (!cJSON_IsBool(cr) || !cJSON_IsTrue(cr)) {
                *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
                    "ctrl+lamiga+ramiga REBOOTS the machine; "
                    "requires confirm_reset:true in addition to confirm",
                    NULL);
                return 0;
            }
        }
    }

    inject_ctx c;
    if (_inject_open(&c, out_err) != 0) { _inject_close(&c); return 0; }

    for (int i = 0; i < n - 1; i++) {
        if (_push_modifier(&c, resolved[i]) != 0) goto done;
        _delay_ms(&c, delay);
    }

    {
        uint16_t q = c.held_quals;
        if (final_shift) q |= IEQUALIFIER_LSHIFT;

        /* A shifted single character needs a real shift press too, for
         * the same reason chords do. */
        if (final_shift) {
            const named_key *sh = _find_named_key("lshift");
            if (sh && _push_modifier(&c, sh) != 0) goto done;
            q = c.held_quals;
        }
        if (_key_event(&c, final_code, 0, q) != 0) goto done;
        _delay_ms(&c, delay);
        if (_key_event(&c, final_code, 1, q) != 0) goto done;
    }

done:
    /* _inject_close releases every modifier we pushed, in reverse. */
    {
        cJSON *r = cJSON_CreateObject();
        cJSON *echo = cJSON_AddArrayToObject(r, "keys");
        for (int i = 0; i < n; i++) {
            cJSON *e = cJSON_GetArrayItem(keys, i);
            if (cJSON_IsString(e) && e->valuestring)
                cJSON_AddItemToArray(echo,
                    cJSON_CreateString(e->valuestring));
        }
        cJSON_AddStringToObject(r, "op", "key");
        _inject_close(&c);
        _add_common(r, &c);
        *out_result = r;
    }
    return 0;
}


/* ---- input.type ---------------------------------------------------- *
 *
 * Bulk text entry. params:
 *   text      : the string, UTF-8 on the wire, <= 512 CHARACTERS
 *   keymap    : optional, "system" (default, layout-correct via
 *               keymap.library MapANSI) or "us" (built-in table)
 *   delay_ms  : optional pacing
 *   confirm   : REQUIRED
 *
 * Characters are decoded from UTF-8 to a codepoint first (see
 * _utf8_next) because both mapping paths are ANSI / ISO-8859-1: one
 * byte per character. Anything above U+00FF, and anything the keymap
 * cannot generate, is reported in unmapped[] rather than typed as
 * something else.
 *
 * In the "us" fallback path this does NOT emit separate shift
 * press/release events -- it sets IEQUALIFIER_LSHIFT on the character
 * event itself. Intuition's RawKeyConvert path derives the character
 * from code + qualifier, so this is correct for text entry and halves
 * the event count, which matters against the 256-event cap. The
 * MapANSI path uses whatever qualifier the keymap returned.
 */
int input_type(cJSON *params, cJSON **out_result, cJSON **out_err) {
    INPUT_GATE();
    if (_require_confirm(params, "input.type", out_err) != 0) return 0;

    cJSON *err = NULL;
    const char *text = p_str(params, "text", &err);
    if (!text) { *out_err = err; return 0; }

    size_t len = strlen(text);
    if (len == 0) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "text must not be empty", NULL);
        return 0;
    }
    /* In CHARACTERS, not bytes -- the wire is UTF-8, so an accented
     * string is longer in bytes than the caller counted. */
    size_t nchars = _utf8_strlen(text, len);
    if (nchars > INPUT_MAX_TEXT) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "text exceeds 512 characters", NULL);
        return 0;
    }

    /* keymap: "system" (default, layout-correct via keymap.library)
     * or "us" to force the built-in table. */
    int force_us = 0;
    cJSON *km = cJSON_GetObjectItemCaseSensitive(params, "keymap");
    if (cJSON_IsString(km) && km->valuestring) {
        if (strcmp(km->valuestring, "us") == 0) force_us = 1;
        else if (strcmp(km->valuestring, "system") != 0) {
            *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
                "keymap must be \"system\" (default) or \"us\"", NULL);
            return 0;
        }
    }

    int delay;
    if (_get_delay(params, INPUT_DEF_CHAR_MS, &delay, out_err) != 0) return 0;

    inject_ctx c;
    if (_inject_open(&c, out_err) != 0) { _inject_close(&c); return 0; }

    int use_keymap = (!force_us && c.ikeymap != NULL);

    cJSON *unmapped = cJSON_CreateArray();
    int mapped = 0;

    for (size_t i = 0; i < len; ) {
        uint32_t cp;
        i += (size_t)_utf8_next(text, len, i, &cp);

        /* MapANSI and the US table are both ANSI (ISO-8859-1): one
         * byte per character. A codepoint above 0xFF has no key on any
         * Amiga keymap, so report it rather than truncating it into a
         * different character. */
        if (cp > 0xFFu) {
            char one[12];
            snprintf(one, sizeof(one), "U+%04X", (unsigned)cp);
            cJSON_AddItemToArray(unmapped, cJSON_CreateString(one));
            continue;
        }
        unsigned char ch = (unsigned char)cp;

        if (use_keymap) {
            /* MapANSI yields code/qualifier PAIRS (2 bytes each) for
             * the keystrokes that would produce this character on the
             * system's configured keymap. Return value is the pair
             * count: 1 for a plain key, 2 or 3 when the character
             * needs a dead-key prefix (e.g. accents). 0 / -1 / -2 are
             * "ungeneratable" / overflow / internal error.
             *
             * `length` is the buffer size in BYTES DIVIDED BY TWO --
             * i.e. the number of pairs it may write, not the byte
             * count. Passing sizeof(rbuf) here would let MapANSI
             * write twice the space we own. */
            uint8_t rbuf[6];
            memset(rbuf, 0, sizeof(rbuf));
            int32 pairs = c.ikeymap->MapANSI((STRPTR)&ch, 1,
                                             (STRPTR)rbuf, 3, NULL);
            if (pairs < 1 || pairs > 3) {
                char one[12];
                snprintf(one, sizeof(one), "U+%04X", (unsigned)cp);
                cJSON_AddItemToArray(unmapped, cJSON_CreateString(one));
                continue;
            }

            /* Emit the pairs in order. For a dead-key sequence the
             * earlier pairs are the dead keys, and each subsequent
             * event carries the preceding down-codes as context so
             * the keymap can compose the accent. Unused context slots
             * must be 0x80, NOT 0 -- 0 is itself a valid deadkey code
             * (autodoc keymap.library/MapANSI). */
            int aborted = 0;
            for (int p = 0; p < pairs && !aborted; p++) {
                uint8_t code = rbuf[p * 2];
                uint8_t qual = rbuf[p * 2 + 1];

                _ev_reset(&c);
                c.ev->ie_Class     = IECLASS_RAWKEY;
                c.ev->ie_Code      = (UWORD)code;
                c.ev->ie_Qualifier = (UWORD)qual;
                c.ev->ie_Prev2DownCode = (p >= 2) ? rbuf[(p - 2) * 2] : 0x80;
                c.ev->ie_Prev2DownQual = (p >= 2) ? rbuf[(p - 2) * 2 + 1] : 0;
                c.ev->ie_Prev1DownCode = (p >= 1) ? rbuf[(p - 1) * 2] : 0x80;
                c.ev->ie_Prev1DownQual = (p >= 1) ? rbuf[(p - 1) * 2 + 1] : 0;
                if (_write_event(&c) != 0) { aborted = 1; break; }

                _delay_ms(&c, delay > 2 ? delay / 2 : delay);

                c.ev->ie_Code = (UWORD)(code | IECODE_UP_PREFIX);
                if (_write_event(&c) != 0) { aborted = 1; break; }
                _delay_ms(&c, delay);
            }
            if (aborted) break;
            mapped++;
            continue;
        }

        /* Fallback: built-in US table. */
        uint8_t code; int shift;
        if (!_ascii_to_raw(ch, &code, &shift)) {
            /* Report rather than drop silently -- a caller who typed a
             * password containing a character we can't map needs to
             * know the target got a DIFFERENT string than requested. */
            char one[12];
            snprintf(one, sizeof(one), "U+%04X", (unsigned)cp);
            cJSON_AddItemToArray(unmapped, cJSON_CreateString(one));
            continue;
        }
        uint16_t q = shift ? IEQUALIFIER_LSHIFT : 0;
        if (_key_event(&c, code, 0, q) != 0) break;
        _delay_ms(&c, delay > 2 ? delay / 2 : delay);
        if (_key_event(&c, code, 1, q) != 0) break;
        _delay_ms(&c, delay);
        mapped++;
    }

    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "op", "type");
    cJSON_AddNumberToObject(r, "text_len", (double)nchars);
    cJSON_AddNumberToObject(r, "chars_mapped", (double)mapped);
    cJSON_AddItemToObject(r, "unmapped", unmapped);
    cJSON_AddStringToObject(r, "keymap",
                            use_keymap ? "keymap.library" : "us-fallback");

    _inject_close(&c);
    _add_common(r, &c);
    *out_result = r;
    return 0;
}


/* ---- input.mouse_move ---------------------------------------------- *
 *
 * params (one of):
 *   dx, dy            -- relative motion
 *   x, y              -- absolute target position
 * plus:
 *   absolute_mode     -- "delta" (default) | "raw"
 *   steps             -- split the motion into N events (default 1)
 *   delay_ms
 *
 * No confirm: pure motion commits nothing.
 *
 * ABSOLUTE POSITIONING. The default "delta" mode reads the current
 * pointer position off the frontmost screen and emits a RELATIVE
 * delta to get there, then re-reads to report where it actually
 * landed. Omitting IEQUALIFIER_RELATIVEMOUSE is documented to make
 * ie_X/ie_Y absolute, but its behaviour on AOS4 with modern pointer
 * drivers is exactly the sort of thing that works on one machine and
 * silently does nothing on another. The delta approach uses only
 * Screen->MouseX/MouseY, which wb.c already proves is readable, and
 * it is self-verifying. "raw" exposes the absolute form so a reviewer
 * can A/B them on hardware without a rebuild.
 */
int input_mouse_move(cJSON *params, cJSON **out_result, cJSON **out_err) {
    INPUT_GATE();

    cJSON *jx  = cJSON_GetObjectItemCaseSensitive(params, "x");
    cJSON *jy  = cJSON_GetObjectItemCaseSensitive(params, "y");
    cJSON *jdx = cJSON_GetObjectItemCaseSensitive(params, "dx");
    cJSON *jdy = cJSON_GetObjectItemCaseSensitive(params, "dy");

    int absolute = (cJSON_IsNumber(jx) && cJSON_IsNumber(jy));
    int relative = (cJSON_IsNumber(jdx) || cJSON_IsNumber(jdy));

    if (absolute == relative) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "provide either x+y (absolute) or dx/dy (relative), not both",
            NULL);
        return 0;
    }

    int raw_absolute = 0;
    cJSON *am = cJSON_GetObjectItemCaseSensitive(params, "absolute_mode");
    if (cJSON_IsString(am) && am->valuestring) {
        if (strcmp(am->valuestring, "raw") == 0) raw_absolute = 1;
        else if (strcmp(am->valuestring, "delta") != 0) {
            *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
                "absolute_mode must be \"delta\" or \"raw\"", NULL);
            return 0;
        }
    }

    long long steps = p_int(params, "steps", 1);
    if (steps < 1 || steps > INPUT_MAX_DRAG_STEPS) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "steps out of range [1..64]", NULL);
        return 0;
    }

    int delay;
    if (_get_delay(params, INPUT_DEF_MOVE_MS, &delay, out_err) != 0) return 0;

    int dx = 0, dy = 0;
    int target_x = 0, target_y = 0;

    if (absolute) {
        target_x = (int)jx->valuedouble;
        target_y = (int)jy->valuedouble;

        if (!raw_absolute) {
            int cx, cy, w, h;
            if (_pointer_pos(&cx, &cy, &w, &h, out_err) != 0) return 0;
            /* Clamp into the screen so a bad coordinate cannot fling
             * the pointer somewhere unrecoverable. */
            if (target_x < 0) target_x = 0;
            if (target_y < 0) target_y = 0;
            if (target_x > w - 1) target_x = w - 1;
            if (target_y > h - 1) target_y = h - 1;
            dx = target_x - cx;
            dy = target_y - cy;
        }
    } else {
        dx = cJSON_IsNumber(jdx) ? (int)jdx->valuedouble : 0;
        dy = cJSON_IsNumber(jdy) ? (int)jdy->valuedouble : 0;
        if (dx > INPUT_MAX_MOVE || dx < -INPUT_MAX_MOVE ||
            dy > INPUT_MAX_MOVE || dy < -INPUT_MAX_MOVE) {
            *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
                "dx/dy out of range [-4096..4096]", NULL);
            return 0;
        }
    }

    inject_ctx c;
    if (_inject_open(&c, out_err) != 0) { _inject_close(&c); return 0; }

    if (absolute && raw_absolute) {
        _ev_reset(&c);
        c.ev->ie_Class     = IECLASS_RAWMOUSE;
        c.ev->ie_Code      = IECODE_NOBUTTON;
        c.ev->ie_Qualifier = 0;   /* no RELATIVEMOUSE => absolute */
        c.ev->ie_X = (WORD)target_x;
        c.ev->ie_Y = (WORD)target_y;
        _write_event(&c);
    } else {
        /* Split into `steps` events. Integer division leaves a
         * remainder; give it all to the final step so the total is
         * exact rather than landing a pixel or two short. */
        for (int i = 1; i <= (int)steps; i++) {
            int sx = (dx * i) / (int)steps - (dx * (i - 1)) / (int)steps;
            int sy = (dy * i) / (int)steps - (dy * (i - 1)) / (int)steps;
            if (sx == 0 && sy == 0 && steps > 1) continue;
            _ev_reset(&c);
            c.ev->ie_Class     = IECLASS_RAWMOUSE;
            c.ev->ie_Code      = IECODE_NOBUTTON;
            c.ev->ie_Qualifier = IEQUALIFIER_RELATIVEMOUSE | c.held_buttons;
            c.ev->ie_X = (WORD)sx;
            c.ev->ie_Y = (WORD)sy;
            if (_write_event(&c) != 0) break;
            if (i < (int)steps) _delay_ms(&c, delay);
        }
    }

    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "op", "mouse_move");
    cJSON_AddStringToObject(r, "mode", absolute ? "absolute" : "relative");

    _inject_close(&c);

    /* Report where the pointer ACTUALLY ended up, not where we asked
     * it to go. On the delta path this is what makes the operation
     * self-verifying; a caller can compare and retry. */
    {
        cJSON *pe = NULL;
        int cx, cy, w, h;
        if (_pointer_pos(&cx, &cy, &w, &h, &pe) == 0) {
            cJSON_AddNumberToObject(r, "x", (double)cx);
            cJSON_AddNumberToObject(r, "y", (double)cy);
        } else {
            cJSON_AddNullToObject(r, "x");
            cJSON_AddNullToObject(r, "y");
            if (pe) cJSON_Delete(pe);
        }
    }

    _add_common(r, &c);
    *out_result = r;
    return 0;
}


/* Map a button name to its IECODE_* and matching qualifier bit. */
static int _button_code(const char *name, uint8_t *code, uint16_t *qual) {
    if (!name || strcmp(name, "left") == 0) {
        *code = IECODE_LBUTTON; *qual = IEQUALIFIER_LEFTBUTTON; return 1;
    }
    if (strcmp(name, "right") == 0) {
        *code = IECODE_RBUTTON; *qual = IEQUALIFIER_RBUTTON; return 1;
    }
    if (strcmp(name, "middle") == 0) {
        *code = IECODE_MBUTTON; *qual = IEQUALIFIER_MIDBUTTON; return 1;
    }
    return 0;
}


/* ---- input.click --------------------------------------------------- *
 *
 * params: button ("left"), count (1), x/y (optional pre-move),
 *         delay_ms, confirm (REQUIRED)
 *
 * Confirm is required because a click lands on whatever is under the
 * pointer -- which the caller cannot know for certain without having
 * called input.state first. That is the whole argument for gating it.
 */
int input_click(cJSON *params, cJSON **out_result, cJSON **out_err) {
    INPUT_GATE();
    if (_require_confirm(params, "input.click", out_err) != 0) return 0;

    cJSON *jb = cJSON_GetObjectItemCaseSensitive(params, "button");
    const char *bname = (cJSON_IsString(jb) && jb->valuestring)
                        ? jb->valuestring : "left";
    uint8_t bcode; uint16_t bqual;
    if (!_button_code(bname, &bcode, &bqual)) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "button must be \"left\", \"right\" or \"middle\"", NULL);
        return 0;
    }

    long long count = p_int(params, "count", 1);
    if (count < 1 || count > 8) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "count out of range [1..8]", NULL);
        return 0;
    }

    int delay;
    if (_get_delay(params, INPUT_DEF_BUTTON_MS, &delay, out_err) != 0) return 0;

    /* Optional pre-move. Done as a relative delta for the same reason
     * input.mouse_move defaults to delta mode. */
    cJSON *jx = cJSON_GetObjectItemCaseSensitive(params, "x");
    cJSON *jy = cJSON_GetObjectItemCaseSensitive(params, "y");
    int want_move = (cJSON_IsNumber(jx) && cJSON_IsNumber(jy));
    int dx = 0, dy = 0;
    if (want_move) {
        int cx, cy, w, h;
        if (_pointer_pos(&cx, &cy, &w, &h, out_err) != 0) return 0;
        int tx = (int)jx->valuedouble;
        int ty = (int)jy->valuedouble;
        if (tx < 0) tx = 0;
        if (ty < 0) ty = 0;
        if (tx > w - 1) tx = w - 1;
        if (ty > h - 1) ty = h - 1;
        dx = tx - cx;
        dy = ty - cy;
    }

    inject_ctx c;
    if (_inject_open(&c, out_err) != 0) { _inject_close(&c); return 0; }

    if (want_move && (dx || dy)) {
        _ev_reset(&c);
        c.ev->ie_Class     = IECLASS_RAWMOUSE;
        c.ev->ie_Code      = IECODE_NOBUTTON;
        c.ev->ie_Qualifier = IEQUALIFIER_RELATIVEMOUSE;
        c.ev->ie_X = (WORD)dx;
        c.ev->ie_Y = (WORD)dy;
        _write_event(&c);
        _delay_ms(&c, delay);
    }

    for (int i = 0; i < (int)count; i++) {
        /* Press. ie_X/ie_Y stay 0 so the click does not nudge the
         * pointer off the thing we just aimed at. */
        _ev_reset(&c);
        c.ev->ie_Class     = IECLASS_RAWMOUSE;
        c.ev->ie_Code      = bcode;
        c.ev->ie_Qualifier = IEQUALIFIER_RELATIVEMOUSE;
        c.ev->ie_X = 0; c.ev->ie_Y = 0;
        if (_write_event(&c) != 0) break;
        c.held_button_code = bcode;
        c.held_buttons = bqual;

        _delay_ms(&c, delay);

        _ev_reset(&c);
        c.ev->ie_Class     = IECLASS_RAWMOUSE;
        c.ev->ie_Code      = (UWORD)(bcode | IECODE_UP_PREFIX);
        c.ev->ie_Qualifier = IEQUALIFIER_RELATIVEMOUSE;
        c.ev->ie_X = 0; c.ev->ie_Y = 0;
        if (_write_event(&c) != 0) break;
        c.held_button_code = 0;
        c.held_buttons = 0;

        if (i + 1 < (int)count) _delay_ms(&c, delay);
    }

    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "op", "click");
    cJSON_AddStringToObject(r, "mode", want_move ? "absolute" : "relative");
    cJSON_AddStringToObject(r, "button", bname);
    cJSON_AddNumberToObject(r, "count", (double)count);

    _inject_close(&c);
    _add_common(r, &c);
    *out_result = r;
    return 0;
}


/* ---- input.drag ---------------------------------------------------- *
 *
 * params: from_x, from_y, to_x, to_y, button ("left"), steps (16),
 *         delay_ms, confirm (REQUIRED)
 *
 * The riskiest method here: a drag can move, resize, or drag-to-trash.
 * It is also the one where an early abort does the most damage, since
 * bailing out mid-drag leaves the button down. _inject_close() releases
 * it unconditionally -- see the comment there.
 */
int input_drag(cJSON *params, cJSON **out_result, cJSON **out_err) {
    INPUT_GATE();
    if (_require_confirm(params, "input.drag", out_err) != 0) return 0;

    cJSON *jfx = cJSON_GetObjectItemCaseSensitive(params, "from_x");
    cJSON *jfy = cJSON_GetObjectItemCaseSensitive(params, "from_y");
    cJSON *jtx = cJSON_GetObjectItemCaseSensitive(params, "to_x");
    cJSON *jty = cJSON_GetObjectItemCaseSensitive(params, "to_y");
    if (!cJSON_IsNumber(jfx) || !cJSON_IsNumber(jfy)
        || !cJSON_IsNumber(jtx) || !cJSON_IsNumber(jty)) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "from_x, from_y, to_x, to_y are all required", NULL);
        return 0;
    }

    cJSON *jb = cJSON_GetObjectItemCaseSensitive(params, "button");
    const char *bname = (cJSON_IsString(jb) && jb->valuestring)
                        ? jb->valuestring : "left";
    uint8_t bcode; uint16_t bqual;
    if (!_button_code(bname, &bcode, &bqual)) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "button must be \"left\", \"right\" or \"middle\"", NULL);
        return 0;
    }

    long long steps = p_int(params, "steps", 16);
    if (steps < 1 || steps > INPUT_MAX_DRAG_STEPS) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "steps out of range [1..64]", NULL);
        return 0;
    }

    int delay;
    if (_get_delay(params, INPUT_DEF_MOVE_MS, &delay, out_err) != 0) return 0;

    int cx, cy, w, h;
    if (_pointer_pos(&cx, &cy, &w, &h, out_err) != 0) return 0;

    int fx = (int)jfx->valuedouble;
    int fy = (int)jfy->valuedouble;
    int tx = (int)jtx->valuedouble;
    int ty = (int)jty->valuedouble;
    if (fx < 0) fx = 0;
    if (fy < 0) fy = 0;
    if (tx < 0) tx = 0;
    if (ty < 0) ty = 0;
    if (fx > w - 1) fx = w - 1;
    if (fy > h - 1) fy = h - 1;
    if (tx > w - 1) tx = w - 1;
    if (ty > h - 1) ty = h - 1;

    inject_ctx c;
    if (_inject_open(&c, out_err) != 0) { _inject_close(&c); return 0; }

    /* 1. move to the start point */
    _ev_reset(&c);
    c.ev->ie_Class     = IECLASS_RAWMOUSE;
    c.ev->ie_Code      = IECODE_NOBUTTON;
    c.ev->ie_Qualifier = IEQUALIFIER_RELATIVEMOUSE;
    c.ev->ie_X = (WORD)(fx - cx);
    c.ev->ie_Y = (WORD)(fy - cy);
    if (_write_event(&c) != 0) goto drag_done;
    _delay_ms(&c, INPUT_DEF_BUTTON_MS);

    /* 2. button down -- recorded in ctx so _inject_close can undo it */
    _ev_reset(&c);
    c.ev->ie_Class     = IECLASS_RAWMOUSE;
    c.ev->ie_Code      = bcode;
    c.ev->ie_Qualifier = IEQUALIFIER_RELATIVEMOUSE;
    c.ev->ie_X = 0; c.ev->ie_Y = 0;
    if (_write_event(&c) != 0) goto drag_done;
    c.held_button_code = bcode;
    c.held_buttons = bqual;
    _delay_ms(&c, INPUT_DEF_BUTTON_MS);

    /* 3. interpolated motion, carrying the button qualifier throughout
     *    so the receiving app sees a genuine drag rather than a
     *    teleport between two unrelated positions */
    {
        int ddx = tx - fx, ddy = ty - fy;
        for (int i = 1; i <= (int)steps; i++) {
            int sx = (ddx * i) / (int)steps - (ddx * (i - 1)) / (int)steps;
            int sy = (ddy * i) / (int)steps - (ddy * (i - 1)) / (int)steps;
            if (sx == 0 && sy == 0) continue;
            _ev_reset(&c);
            c.ev->ie_Class     = IECLASS_RAWMOUSE;
            c.ev->ie_Code      = IECODE_NOBUTTON;
            c.ev->ie_Qualifier = IEQUALIFIER_RELATIVEMOUSE | c.held_buttons;
            c.ev->ie_X = (WORD)sx;
            c.ev->ie_Y = (WORD)sy;
            if (_write_event(&c) != 0) goto drag_done;
            _delay_ms(&c, delay);
        }
    }

    _delay_ms(&c, INPUT_DEF_BUTTON_MS);
    /* 4. release happens in _inject_close via _release_buttons */

drag_done:
    {
        cJSON *r = cJSON_CreateObject();
        cJSON_AddStringToObject(r, "op", "drag");
        cJSON_AddStringToObject(r, "mode", "absolute");
        cJSON_AddStringToObject(r, "button", bname);
        _inject_close(&c);
        {
            cJSON *pe = NULL;
            int ex, ey, ew, eh;
            if (_pointer_pos(&ex, &ey, &ew, &eh, &pe) == 0) {
                cJSON_AddNumberToObject(r, "x", (double)ex);
                cJSON_AddNumberToObject(r, "y", (double)ey);
            } else {
                cJSON_AddNullToObject(r, "x");
                cJSON_AddNullToObject(r, "y");
                if (pe) cJSON_Delete(pe);
            }
        }
        _add_common(r, &c);
        *out_result = r;
    }
    return 0;
}


/* ---- input.scroll -------------------------------------------------- *
 *
 * params: clicks (1..32), direction ("down"|"up"|"left"|"right"),
 *         delay_ms
 *
 * No confirm: scrolling commits nothing.
 *
 * Emits NewMouse wheel codes as IECLASS_RAWMOUSE events, which is what
 * MUI and ReAction actually listen for. See the NM_WHEEL_* guards at
 * the top -- those constants are not reliably in the SDK headers.
 */
int input_scroll(cJSON *params, cJSON **out_result, cJSON **out_err) {
    INPUT_GATE();

    long long clicks = p_int(params, "clicks", 1);
    if (clicks < 1 || clicks > INPUT_MAX_SCROLL) {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "clicks out of range [1..32]", NULL);
        return 0;
    }

    cJSON *jd = cJSON_GetObjectItemCaseSensitive(params, "direction");
    const char *dir = (cJSON_IsString(jd) && jd->valuestring)
                      ? jd->valuestring : "down";
    uint8_t code;
    if      (strcmp(dir, "down")  == 0) code = NM_WHEEL_DOWN;
    else if (strcmp(dir, "up")    == 0) code = NM_WHEEL_UP;
    else if (strcmp(dir, "left")  == 0) code = NM_WHEEL_LEFT;
    else if (strcmp(dir, "right") == 0) code = NM_WHEEL_RIGHT;
    else {
        *out_err = rpc_make_error(MCPD_ERR_INVPARAMS,
            "direction must be \"up\", \"down\", \"left\" or \"right\"",
            NULL);
        return 0;
    }

    int delay;
    if (_get_delay(params, INPUT_DEF_MOVE_MS, &delay, out_err) != 0) return 0;

    inject_ctx c;
    if (_inject_open(&c, out_err) != 0) { _inject_close(&c); return 0; }

    for (int i = 0; i < (int)clicks; i++) {
        _ev_reset(&c);
        c.ev->ie_Class     = IECLASS_RAWMOUSE;
        c.ev->ie_Code      = code;
        c.ev->ie_Qualifier = IEQUALIFIER_RELATIVEMOUSE;
        c.ev->ie_X = 0; c.ev->ie_Y = 0;
        if (_write_event(&c) != 0) break;
        if (i + 1 < (int)clicks) _delay_ms(&c, delay);
    }

    cJSON *r = cJSON_CreateObject();
    cJSON_AddStringToObject(r, "op", "scroll");
    cJSON_AddStringToObject(r, "mode", "relative");
    cJSON_AddStringToObject(r, "direction", dir);
    cJSON_AddNumberToObject(r, "clicks", (double)clicks);

    _inject_close(&c);
    _add_common(r, &c);
    *out_result = r;
    return 0;
}
