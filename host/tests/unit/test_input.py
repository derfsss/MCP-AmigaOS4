"""input.* tool tests with a fake MCPd transport.

input.* is the only tool surface in this project that *writes* to the
target's UI, so the disabled-by-default behaviour is the thing most
worth pinning down. In priority order, this file verifies:

- with no [input] config block at all, every tool raises NotCapable
  and NOTHING reaches the wire
- with enabled=false, the same
- TargetConfig defaults to input=None, so a future refactor cannot
  silently flip the default open
- a daemon-side NotCapable propagates even when host config is open --
  the host must not mask the real gate
- confirm gates on type / key / click / drag, and their absence on
  mouse_move / scroll / state
- chord and keys spellings produce byte-identical wire params
- host-side caps and Latin-1 validation reject before any wire call

No conftest.py in this repo -- fakes are defined per-module by
convention (see test_tools_with_fake_transport.py).
"""

from __future__ import annotations

from typing import Any

import pytest

from amiga_fleet_mcp.config import (
    Config,
    InputConfig,
    McpdChannel,
    TargetChannels,
    TargetConfig,
)
from amiga_fleet_mcp.errors import InvalidParams, NotCapable
from amiga_fleet_mcp.fleet import Fleet
from amiga_fleet_mcp.tools import input as input_tool


class FakeMcpd:
    """Stub MCPd transport: programmable per-method responses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.responses: dict[str, Any] = {}
        self.errors: dict[str, Exception] = {}

    async def request(self, method: str, params: dict | None = None,
                      timeout_s: float = 30.0) -> Any:
        self.calls.append((method, params))
        if method in self.errors:
            raise self.errors[method]
        return self.responses.get(method, {})


def _fleet(*, input_block: InputConfig | None) -> tuple[Fleet, FakeMcpd]:
    cfg = Config(targets={
        "x5000": TargetConfig(
            type="remote",
            channels=TargetChannels(
                mcpd=McpdChannel(endpoint="192.168.0.10:4322"),
            ),
            input=input_block,
        ),
    })
    fleet = Fleet(cfg)
    fake = FakeMcpd()
    fleet._mcpd["x5000"] = fake  # type: ignore[assignment]
    return fleet, fake


def _enabled() -> tuple[Fleet, FakeMcpd]:
    return _fleet(input_block=InputConfig(enabled=True))


async def _call_all(fleet: Fleet) -> list[Exception]:
    """Invoke every input.* tool with otherwise-valid arguments and
    collect whatever each one raised."""
    raised: list[Exception] = []
    attempts = [
        lambda: input_tool.input_state(fleet, "x5000"),
        lambda: input_tool.input_type(
            fleet, "x5000", text="hi", confirm=True),
        lambda: input_tool.input_key(
            fleet, "x5000", keys=["lamiga", "q"], confirm=True),
        lambda: input_tool.input_mouse_move(fleet, "x5000", dx=5, dy=5),
        lambda: input_tool.input_click(fleet, "x5000", confirm=True),
        lambda: input_tool.input_drag(
            fleet, "x5000", from_x=0, from_y=0, to_x=10, to_y=10,
            confirm=True),
        lambda: input_tool.input_scroll(fleet, "x5000", clicks=1),
    ]
    for make in attempts:
        try:
            await make()
        except Exception as exc:  # collecting for assertions
            raised.append(exc)
    return raised


# ---- disabled by default -- the point of this file -------------------


@pytest.mark.asyncio
async def test_absent_input_block_raises_not_capable() -> None:
    """No [input] block at all: every tool refuses, nothing hits the wire."""
    fleet, fake = _fleet(input_block=None)
    raised = await _call_all(fleet)

    assert len(raised) == 7
    assert all(isinstance(e, NotCapable) for e in raised)
    assert fake.calls == []


@pytest.mark.asyncio
async def test_explicitly_disabled_raises_not_capable() -> None:
    fleet, fake = _fleet(input_block=InputConfig(enabled=False))
    raised = await _call_all(fleet)

    assert len(raised) == 7
    assert all(isinstance(e, NotCapable) for e in raised)
    assert fake.calls == []


def test_target_config_defaults_to_no_input() -> None:
    """Pin the default. If this ever fails, someone has made input
    injection opt-out instead of opt-in."""
    assert TargetConfig(type="remote").input is None


def test_input_config_defaults_to_disabled() -> None:
    """And if the block IS present but says nothing, it is still off."""
    assert InputConfig().enabled is False


@pytest.mark.asyncio
async def test_not_capable_error_names_both_gates() -> None:
    """The error must mention the daemon gate too -- an operator who
    only flipped the host switch would otherwise hit a confusing
    -32003 on the very next call."""
    fleet, _ = _fleet(input_block=None)
    with pytest.raises(NotCapable) as ei:
        await input_tool.input_state(fleet, "x5000")
    msg = str(ei.value)
    assert "--enable-input" in msg
    assert "ENABLE-INPUT" in msg


@pytest.mark.asyncio
async def test_daemon_gate_propagates_through_open_host_gate() -> None:
    """Host config open, daemon says no. The host must surface that
    rather than swallowing or rewriting it."""
    fleet, fake = _enabled()
    fake.errors["input.state"] = NotCapable(
        "input injection is disabled on this daemon",
        data={"reason": "input_disabled"},
    )
    with pytest.raises(NotCapable):
        await input_tool.input_state(fleet, "x5000")
    assert [c[0] for c in fake.calls] == ["input.state"]


# ---- confirm gates ---------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["type", "key", "click", "drag"])
async def test_committing_tools_require_confirm(call: str) -> None:
    fleet, fake = _enabled()
    kwargs: dict[str, Any] = {
        "type": {"text": "hi"},
        "key": {"keys": ["lamiga", "q"]},
        "click": {},
        "drag": {"from_x": 0, "from_y": 0, "to_x": 5, "to_y": 5},
    }[call]

    fn = getattr(input_tool, f"input_{call}")
    with pytest.raises(InvalidParams):
        await fn(fleet, "x5000", **kwargs)

    # A gate that raises only after hitting the wire is not a gate.
    assert fake.calls == []


@pytest.mark.asyncio
async def test_non_committing_tools_need_no_confirm() -> None:
    fleet, fake = _enabled()
    await input_tool.input_state(fleet, "x5000")
    await input_tool.input_mouse_move(fleet, "x5000", dx=1, dy=1)
    await input_tool.input_scroll(fleet, "x5000", clicks=2)
    assert [c[0] for c in fake.calls] == [
        "input.state", "input.mouse_move", "input.scroll",
    ]


# ---- wire shape ------------------------------------------------------


@pytest.mark.asyncio
async def test_type_wire_params() -> None:
    fleet, fake = _enabled()
    await input_tool.input_type(
        fleet, "x5000", text="hello", delay_ms=20, confirm=True)
    assert fake.calls == [
        ("input.type", {"text": "hello", "keymap": "system",
                        "delay_ms": 20, "confirm": True}),
    ]


@pytest.mark.asyncio
async def test_type_defaults_to_system_keymap() -> None:
    """Layout-correct by default. If this ever flips to "us", non-US
    targets silently receive wrong characters."""
    fleet, fake = _enabled()
    await input_tool.input_type(fleet, "x5000", text="hi", confirm=True)
    params = fake.calls[0][1]
    assert params is not None
    assert params["keymap"] == "system"


@pytest.mark.asyncio
async def test_type_us_keymap_forced() -> None:
    fleet, fake = _enabled()
    await input_tool.input_type(
        fleet, "x5000", text="hi", keymap="us", confirm=True)
    params = fake.calls[0][1]
    assert params is not None
    assert params["keymap"] == "us"


@pytest.mark.asyncio
async def test_type_rejects_unknown_keymap() -> None:
    fleet, fake = _enabled()
    with pytest.raises(InvalidParams):
        await input_tool.input_type(
            fleet, "x5000", text="hi", keymap="dvorak", confirm=True)
    assert fake.calls == []


@pytest.mark.asyncio
async def test_chord_and_keys_produce_identical_params() -> None:
    fleet_a, fake_a = _enabled()
    fleet_b, fake_b = _enabled()
    await input_tool.input_key(
        fleet_a, "x5000", keys=["lamiga", "q"], confirm=True)
    await input_tool.input_key(
        fleet_b, "x5000", chord="lamiga+q", confirm=True)
    assert fake_a.calls == fake_b.calls


@pytest.mark.asyncio
async def test_confirm_reset_only_sent_when_given() -> None:
    """Must NOT be set unconditionally -- that is the bug tools/debug.py
    has with `confirm`, and it would defeat the daemon's reset gate."""
    fleet, fake = _enabled()
    await input_tool.input_key(
        fleet, "x5000", keys=["ctrl", "lamiga", "ramiga"], confirm=True)
    params = fake.calls[0][1]
    assert params is not None
    assert "confirm_reset" not in params


@pytest.mark.asyncio
async def test_confirm_reset_forwarded_when_given() -> None:
    fleet, fake = _enabled()
    await input_tool.input_key(
        fleet, "x5000", keys=["ctrl", "lamiga", "ramiga"],
        confirm=True, confirm_reset=True)
    params = fake.calls[0][1]
    assert params is not None
    assert params["confirm_reset"] is True


@pytest.mark.asyncio
async def test_mouse_move_absolute_wire_params() -> None:
    fleet, fake = _enabled()
    await input_tool.input_mouse_move(fleet, "x5000", x=100, y=200)
    method, params = fake.calls[0]
    assert method == "input.mouse_move"
    assert params is not None
    assert params["x"] == 100
    assert params["y"] == 200
    assert params["absolute_mode"] == "delta"


# ---- host-side validation (rejects before the wire) ------------------


@pytest.mark.asyncio
async def test_text_length_cap() -> None:
    fleet, fake = _enabled()
    with pytest.raises(InvalidParams):
        await input_tool.input_type(
            fleet, "x5000", text="x" * 513, confirm=True)
    assert fake.calls == []


@pytest.mark.asyncio
async def test_latin1_text_accepted() -> None:
    fleet, fake = _enabled()
    await input_tool.input_type(fleet, "x5000", text="café", confirm=True)
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_latin1_text_reaches_the_wire_verbatim() -> None:
    """Pin the host half of the UTF-8 contract.

    The daemon decodes `text` from UTF-8 back to codepoints before
    mapping (input.c `_utf8_next`), because both of its mapping paths
    are one-byte ANSI. That is only correct if the host passes the
    string through untouched and the transport encodes it as UTF-8 --
    it does (`transports/mcpd.py`). Before the daemon-side decode
    existed, "café" was typed as five keystrokes instead of four, and
    the assertion below was the only thing a host test could see.
    """
    fleet, fake = _enabled()
    await input_tool.input_type(fleet, "x5000", text="café", confirm=True)
    _method, params = fake.calls[0]
    assert params is not None
    assert params["text"] == "café"
    assert params["text"].encode("utf-8") == b"caf\xc3\xa9"


@pytest.mark.asyncio
async def test_text_cap_counts_characters_not_bytes() -> None:
    """512 accented characters is 1024 bytes of UTF-8 and must still
    be accepted -- the daemon counts characters too (`_utf8_strlen`)."""
    fleet, fake = _enabled()
    await input_tool.input_type(fleet, "x5000", text="é" * 512, confirm=True)
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_non_latin1_text_rejected_naming_the_char() -> None:
    fleet, fake = _enabled()
    with pytest.raises(InvalidParams) as ei:
        await input_tool.input_type(fleet, "x5000", text="日本語", confirm=True)
    assert "日" in str(ei.value)
    assert fake.calls == []


@pytest.mark.asyncio
async def test_empty_text_rejected() -> None:
    fleet, fake = _enabled()
    with pytest.raises(InvalidParams):
        await input_tool.input_type(fleet, "x5000", text="", confirm=True)
    assert fake.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("delay", [-1, 1001])
async def test_delay_range(delay: int) -> None:
    fleet, fake = _enabled()
    with pytest.raises(InvalidParams):
        await input_tool.input_scroll(fleet, "x5000", delay_ms=delay)
    assert fake.calls == []


@pytest.mark.asyncio
async def test_scroll_clicks_cap() -> None:
    fleet, fake = _enabled()
    with pytest.raises(InvalidParams):
        await input_tool.input_scroll(fleet, "x5000", clicks=33)
    assert fake.calls == []


@pytest.mark.asyncio
async def test_scroll_direction_validated() -> None:
    fleet, fake = _enabled()
    with pytest.raises(InvalidParams):
        await input_tool.input_scroll(fleet, "x5000", direction="sideways")
    assert fake.calls == []


@pytest.mark.asyncio
async def test_mouse_move_requires_one_coordinate_form() -> None:
    fleet, fake = _enabled()
    with pytest.raises(InvalidParams):
        await input_tool.input_mouse_move(fleet, "x5000")
    with pytest.raises(InvalidParams):
        await input_tool.input_mouse_move(fleet, "x5000", x=1, y=2, dx=3)
    assert fake.calls == []


@pytest.mark.asyncio
async def test_click_button_validated() -> None:
    fleet, fake = _enabled()
    with pytest.raises(InvalidParams):
        await input_tool.input_click(
            fleet, "x5000", button="thumb", confirm=True)
    assert fake.calls == []


@pytest.mark.asyncio
async def test_allow_drag_false_blocks_only_drag() -> None:
    fleet, fake = _fleet(
        input_block=InputConfig(enabled=True, allow_drag=False))
    with pytest.raises(NotCapable):
        await input_tool.input_drag(
            fleet, "x5000", from_x=0, from_y=0, to_x=5, to_y=5,
            confirm=True)
    assert fake.calls == []

    # ...but the rest of the surface still works.
    await input_tool.input_click(fleet, "x5000", confirm=True)
    assert [c[0] for c in fake.calls] == ["input.click"]


def test_normalise_keys_rejects_both_forms() -> None:
    with pytest.raises(InvalidParams):
        input_tool.normalise_keys(keys=["a"], chord="lamiga+q")


def test_normalise_keys_rejects_empty() -> None:
    with pytest.raises(InvalidParams):
        input_tool.normalise_keys()


def test_normalise_keys_caps_chord_length() -> None:
    with pytest.raises(InvalidParams):
        input_tool.normalise_keys(keys=["shift"] * 9)


# ---- result models ---------------------------------------------------


def test_type_result_validates_truncated_payload() -> None:
    r = input_tool.TypeResult.model_validate({
        "target": "x5000", "op": "type",
        "events": 256, "duration_ms": 20000, "truncated": True,
        "text_len": 400, "chars_mapped": 128,
        "unmapped": ["0xE9"], "keymap": "us",
        "text_sha256": "deadbeef",
    })
    assert r.truncated is True
    assert r.unmapped == ["0xE9"]


def test_mouse_result_carries_achieved_position() -> None:
    r = input_tool.MouseResult.model_validate({
        "target": "x5000", "op": "mouse_move", "mode": "absolute",
        "events": 1, "duration_ms": 10, "truncated": False,
        "x": 640, "y": 400,
    })
    assert (r.x, r.y) == (640, 400)
