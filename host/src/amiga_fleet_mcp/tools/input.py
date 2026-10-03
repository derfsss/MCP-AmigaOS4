"""input.* — keyboard and mouse injection on the target.

These are the only tools in the project that *write* to the target's
UI. Everything else observes the machine or touches the filesystem;
these type and click as the logged-in user, into whatever window
happens to be focused.

Two independent gates, and it matters which is which:

1. **Daemon-side (the real one).** MCPd must have been started with
   `--enable-input`, or have `SYS:System/MCPd/ENABLE-INPUT` present.
   Otherwise every method returns -32003 regardless of host config.
   Enabling it requires filesystem access to the target *and* an MCPd
   restart — no RPC can turn it on.

2. **Host-side (a wrong-target guard).** Per-target config::

       [targets.x5000-real.input]
       enabled = true

   Absent or `enabled = false` raises `NotCapable` before anything
   reaches the wire. This stops an agent firing input at a target the
   operator never meant to drive; it is *not* access control, since
   anything that can reach TCP 4322 bypasses the host entirely.

`confirm=True` is required on the operations that can commit an action
(`type`, `key`, `click`, `drag`) and not on the ones that cannot
(`mouse_move`, `scroll`, `state`). As everywhere else in this project,
confirm is an accidental-fire guard, **not** authentication.

⚠️ Everything typed through `input.type` is recorded in cleartext in
the run archive, by design — it is an audit log. Do not type
credentials through it.

`input.type` is layout-correct: the daemon maps characters through
`keymap.library`'s `MapANSI` against the target's configured keymap,
so a German or French machine receives the characters you asked for
(verified on a QWERTZ guest). Pass `keymap="us"` to force the
built-in US table instead, which is also the automatic fallback if
`keymap.library` cannot be opened. The result's `keymap` field says
which path ran.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, Field

from ..config import InputConfig
from ..errors import InvalidParams, NotCapable
from ..fleet import Fleet

# ---- result models -------------------------------------------------


class InputResult(BaseModel):
    target: str
    op: str
    events: int = 0
    duration_ms: int = 0
    truncated: bool = False


class TypeResult(InputResult):
    text_len: int = 0
    chars_mapped: int = 0
    #: Hex codes the daemon could not map to a key. Reported rather
    #: than dropped so a caller can tell the target received a
    #: *different* string than requested.
    unmapped: list[str] = Field(default_factory=list)
    keymap: str = ""
    #: Lets a reviewer prove what was typed without the archive having
    #: to be re-read. The plaintext is in the archive too — this is a
    #: convenience, not a redaction.
    text_sha256: str = ""


class KeyResult(InputResult):
    keys: list[str] = Field(default_factory=list)


class MouseResult(InputResult):
    mode: str = "relative"
    button: str | None = None
    count: int | None = None
    direction: str | None = None
    clicks: int | None = None
    #: Pointer position actually achieved, read back after the move.
    x: int | None = None
    y: int | None = None


class InputState(BaseModel):
    target: str
    enabled: bool = False
    pointer_x: int | None = None
    pointer_y: int | None = None
    frontmost_screen: str | None = None
    active_screen: str | None = None
    active_window: str | None = None
    active_window_left: int | None = None
    active_window_top: int | None = None
    active_window_width: int | None = None
    active_window_height: int | None = None
    screen_width: int | None = None
    screen_height: int | None = None


# ---- helpers -------------------------------------------------------


def _resolve_input(fleet: Fleet, target: str) -> InputConfig:
    """Return the target's `[input]` policy block, or raise NotCapable.

    Deliberately names *both* gates in the error: an operator who has
    only flipped the host-side switch would otherwise get a confusing
    -32003 from the daemon on the very next call.
    """
    cfg = fleet.target_config(target)
    ic = cfg.input
    if ic is None or not ic.enabled:
        raise NotCapable(
            "input injection is not enabled for this target. TWO gates "
            "must both be open: (1) host config — set "
            "[targets.<name>.input] enabled = true; (2) the daemon — "
            "start MCPd with --enable-input, or create "
            "SYS:System/MCPd/ENABLE-INPUT on the target and restart "
            "MCPd (see MCPd-Enable-Input). The daemon gate is the real "
            "control; the host one is a wrong-target guard.",
            data={"target": target},
        )
    return ic


def _require_confirm(confirm: bool, op: str) -> None:
    if not confirm:
        raise InvalidParams(
            f"input.{op} requires confirm=True — this injects real "
            f"keyboard/mouse input into the target's UI, acting on "
            f"whatever window is currently focused",
            data={"hint": "pass confirm=True to acknowledge"},
        )


def _check_events_budget(requested: int, cfg: InputConfig,
                         op: str) -> None:
    """Refuse a call that cannot fit in the target's event budget.

    Advisory, like `max_text_len`: the daemon enforces its own cap and
    truncates cleanly, returning `truncated: true`. Checking here turns
    a half-finished action -- half a drag, half a typed line -- into an
    error before anything reaches the target's UI, which is the more
    useful failure for something that acts on a live desktop.
    """
    if requested > cfg.max_events:
        raise InvalidParams(
            f"input.{op} needs about {requested} input events; this "
            f"target allows {cfg.max_events} per call "
            f"([targets.<name>.input] max_events). Split it up, or "
            f"raise the limit.",
            data={"requested": requested, "max_events": cfg.max_events},
        )


def _check_delay(delay_ms: int | None) -> None:
    if delay_ms is not None and not (0 <= delay_ms <= 1000):
        raise InvalidParams(
            "delay_ms must be between 0 and 1000",
            data={"delay_ms": delay_ms},
        )


def _encode_text(text: str, cfg: InputConfig) -> str:
    """Validate a string is typeable and return it unchanged.

    The text crosses the wire as UTF-8 and the daemon decodes it back
    to codepoints before mapping, but both mapping paths on the Amiga
    side (`keymap.library` `MapANSI` and the built-in table) are ANSI /
    ISO-8859-1 — one byte per character. Any codepoint above U+00FF has
    no key on any Amiga keymap, so reject it here, naming the offending
    character, rather than letting it reach the wire and come back as a
    silent entry in `unmapped`.

    Length is counted in characters, matching the daemon's own cap.
    """
    if not text:
        raise InvalidParams("text must not be empty")
    if len(text) > cfg.max_text_len:
        raise InvalidParams(
            f"text is {len(text)} characters; limit is "
            f"{cfg.max_text_len}",
            data={"len": len(text), "max": cfg.max_text_len},
        )
    for ch in text:
        if ord(ch) > 0xFF:
            raise InvalidParams(
                f"text contains {ch!r} (U+{ord(ch):04X}), which cannot "
                f"be represented on an AmigaOS keymap — input.type is "
                f"limited to Latin-1",
                data={"char": ch, "codepoint": ord(ch)},
            )
    return text


def normalise_keys(
    keys: list[str] | None = None, chord: str | None = None,
) -> list[str]:
    """Reduce either accepted chord form to the canonical `keys` list.

    `chord="lamiga+q"` is human shorthand; `keys=["lamiga","q"]` is
    canonical. Normalising here means the daemon implements exactly one
    form — and the two spellings produce byte-identical wire params.
    """
    if keys and chord:
        raise InvalidParams("pass either keys= or chord=, not both")
    if chord:
        keys = [p for p in (s.strip() for s in chord.split("+")) if p]
    if not keys:
        raise InvalidParams(
            "provide keys=['lamiga','q'] or chord='lamiga+q'"
        )
    if len(keys) > 8:
        raise InvalidParams(
            f"a chord may contain at most 8 keys, got {len(keys)}"
        )
    return list(keys)


# ---- read-only (no confirm) ----------------------------------------


async def input_state(fleet: Fleet, target: str) -> InputState:
    """Pointer position, focused window, and screen geometry.

    Call this **before** `input.click` to know what you are about to
    click on. Blind clicking is the main way this tool surface does
    damage, and this is the cheap way to avoid it.
    """
    _resolve_input(fleet, target)
    raw = await fleet.mcpd(target).request("input.state", {})
    return InputState.model_validate({**raw, "target": target})


async def input_mouse_move(
    fleet: Fleet, target: str, *,
    x: int | None = None, y: int | None = None,
    dx: int | None = None, dy: int | None = None,
    absolute_mode: str = "delta",
    steps: int = 1,
    delay_ms: int | None = None,
) -> MouseResult:
    """Move the pointer. No confirm — motion commits nothing.

    Give either `x`+`y` (absolute) or `dx`/`dy` (relative). Absolute
    targets are clamped to the frontmost screen, and the position
    actually achieved is returned so the caller can verify rather than
    assume.

    `absolute_mode="raw"` emits a true absolute event instead of the
    default read-then-delta approach; it exists so the two can be A/B'd
    on real hardware and should not be needed in normal use.
    """
    cfg = _resolve_input(fleet, target)
    _check_delay(delay_ms)

    if (x is None) != (y is None):
        raise InvalidParams("x and y must be given together")
    if (x is None) == (dx is None and dy is None):
        raise InvalidParams(
            "provide either x+y (absolute) or dx/dy (relative)"
        )
    if absolute_mode not in ("delta", "raw"):
        raise InvalidParams('absolute_mode must be "delta" or "raw"')
    if not 1 <= steps <= 64:
        raise InvalidParams("steps must be between 1 and 64")

    params: dict[str, object] = {
        "steps": steps,
        "delay_ms": delay_ms if delay_ms is not None
        else cfg.default_delay_ms,
    }
    if x is not None:
        params["x"] = int(x)
        params["y"] = int(y)  # type: ignore[arg-type]
        params["absolute_mode"] = absolute_mode
    else:
        if dx is not None:
            params["dx"] = int(dx)
        if dy is not None:
            params["dy"] = int(dy)

    raw = await fleet.mcpd(target).request("input.mouse_move", params)
    return MouseResult.model_validate(
        {**raw, "target": target, "op": "mouse_move"}
    )


async def input_scroll(
    fleet: Fleet, target: str, *,
    clicks: int = 1, direction: str = "down",
    delay_ms: int | None = None,
) -> MouseResult:
    """Mouse wheel. No confirm — scrolling commits nothing."""
    cfg = _resolve_input(fleet, target)
    _check_delay(delay_ms)
    if not 1 <= clicks <= 32:
        raise InvalidParams("clicks must be between 1 and 32")
    if direction not in ("up", "down", "left", "right"):
        raise InvalidParams(
            'direction must be "up", "down", "left" or "right"'
        )

    raw = await fleet.mcpd(target).request("input.scroll", {
        "clicks": clicks,
        "direction": direction,
        "delay_ms": delay_ms if delay_ms is not None
        else cfg.default_delay_ms,
    })
    return MouseResult.model_validate(
        {**raw, "target": target, "op": "scroll"}
    )


# ---- committing operations (confirm: true) -------------------------


async def input_type(
    fleet: Fleet, target: str, *,
    text: str, keymap: str = "system",
    delay_ms: int | None = None, confirm: bool = False,
) -> TypeResult:
    """Type a string as keystrokes. Requires `confirm=True`.

    Layout-correct by default: the daemon maps each character through
    `keymap.library` against the target's configured keymap. Pass
    `keymap="us"` to force the built-in US table.

    ⚠️ The text is recorded in cleartext in the run archive. Do not
    type credentials through this tool.
    """
    _require_confirm(confirm, "type")
    cfg = _resolve_input(fleet, target)
    _check_delay(delay_ms)
    text = _encode_text(text, cfg)
    # Two events per character (down, up); a dead-key sequence costs
    # more, so this is the floor rather than the exact figure.
    _check_events_budget(len(text) * 2, cfg, "type")
    if keymap not in ("system", "us"):
        raise InvalidParams('keymap must be "system" or "us"')

    raw = await fleet.mcpd(target).request("input.type", {
        "text": text,
        "keymap": keymap,
        "delay_ms": delay_ms if delay_ms is not None
        else cfg.default_delay_ms,
        "confirm": True,
    })
    return TypeResult.model_validate({
        "text_len": len(text),
        **raw,
        "target": target,
        "op": "type",
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    })


async def input_key(
    fleet: Fleet, target: str, *,
    keys: list[str] | None = None, chord: str | None = None,
    delay_ms: int | None = None,
    confirm: bool = False, confirm_reset: bool = False,
) -> KeyResult:
    """Press a key or chord. Requires `confirm=True`.

    All entries but the last must be modifiers (`shift`, `ctrl`,
    `alt`, `lamiga`, `ramiga`, `capslock`); the last is the key pressed
    and released. Accepts `keys=["lamiga","q"]` or the equivalent
    shorthand `chord="lamiga+q"`.

    `ctrl+lamiga+ramiga` reboots the machine and additionally requires
    `confirm_reset=True`.
    """
    _require_confirm(confirm, "key")
    cfg = _resolve_input(fleet, target)
    _check_delay(delay_ms)
    resolved = normalise_keys(keys, chord)

    params: dict[str, object] = {
        "keys": resolved,
        "delay_ms": delay_ms if delay_ms is not None
        else cfg.default_delay_ms,
        "confirm": True,
    }
    # Only send the extra acknowledgement when it was actually given —
    # the daemon re-checks the reset chord itself, so this must not be
    # set unconditionally the way tools/debug.py does with `confirm`.
    if confirm_reset:
        params["confirm_reset"] = True

    raw = await fleet.mcpd(target).request("input.key", params)
    return KeyResult.model_validate(
        {**raw, "target": target, "op": "key", "keys": resolved}
    )


async def input_click(
    fleet: Fleet, target: str, *,
    button: str = "left", count: int = 1,
    x: int | None = None, y: int | None = None,
    delay_ms: int | None = None, confirm: bool = False,
) -> MouseResult:
    """Click at the pointer, or at `x`/`y` if given. Requires
    `confirm=True` — the click lands on whatever is under the pointer,
    which you cannot know for certain without calling `input.state`
    first."""
    _require_confirm(confirm, "click")
    cfg = _resolve_input(fleet, target)
    _check_delay(delay_ms)
    if button not in ("left", "right", "middle"):
        raise InvalidParams('button must be "left", "right" or "middle"')
    if not 1 <= count <= 8:
        raise InvalidParams("count must be between 1 and 8")
    if (x is None) != (y is None):
        raise InvalidParams("x and y must be given together")

    params: dict[str, object] = {
        "button": button,
        "count": count,
        "delay_ms": delay_ms if delay_ms is not None
        else cfg.default_delay_ms,
        "confirm": True,
    }
    if x is not None:
        params["x"] = int(x)
        params["y"] = int(y)  # type: ignore[arg-type]

    raw = await fleet.mcpd(target).request("input.click", params)
    return MouseResult.model_validate(
        {**raw, "target": target, "op": "click"}
    )


async def input_drag(
    fleet: Fleet, target: str, *,
    from_x: int, from_y: int, to_x: int, to_y: int,
    button: str = "left", steps: int = 16,
    delay_ms: int | None = None, confirm: bool = False,
) -> MouseResult:
    """Press at `from_*`, move to `to_*`, release. Requires
    `confirm=True`.

    The riskiest tool here — a drag can move, resize, or drag-to-trash.
    The daemon always releases the button, even if the call aborts on a
    budget overrun, so a failed drag cannot leave the machine with a
    stuck mouse button.

    Can be disabled per target with `[targets.<n>.input] allow_drag =
    false` while leaving the rest of the surface available.
    """
    _require_confirm(confirm, "drag")
    cfg = _resolve_input(fleet, target)
    _check_delay(delay_ms)
    if not cfg.allow_drag:
        raise NotCapable(
            "input.drag is disabled for this target "
            "([targets.<name>.input] allow_drag = false)",
            data={"target": target},
        )
    if button not in ("left", "right", "middle"):
        raise InvalidParams('button must be "left", "right" or "middle"')
    if not 1 <= steps <= 64:
        raise InvalidParams("steps must be between 1 and 64")
    _check_events_budget(steps + 3, cfg, "drag")

    raw = await fleet.mcpd(target).request("input.drag", {
        "from_x": int(from_x), "from_y": int(from_y),
        "to_x": int(to_x), "to_y": int(to_y),
        "button": button, "steps": steps,
        "delay_ms": delay_ms if delay_ms is not None
        else cfg.default_delay_ms,
        "confirm": True,
    })
    return MouseResult.model_validate(
        {**raw, "target": target, "op": "drag"}
    )
