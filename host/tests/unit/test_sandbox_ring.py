"""Telling "nothing happened" apart from "I cannot see".

`sandbox.last_trap` and `sandbox.run_batch` both read the kernel debug
ring: one for trap signatures, the other for per-guest exit codes. The
AmigaOS debug buffer does not wrap -- once full, the kernel stops
accepting entries rather than overwriting the oldest -- so on a machine
whose buffer filled during boot, neither ever sees anything.

Measured on a real X5000: 711 lines in the ring, all from the graphics
driver, zero from MCPd, and no growth over 60 s with two daemons
running and the GPU actively repainting. A second daemon started by
hand bound its port and still logged nothing.

Both tools used to answer that with silence-as-success: `found=False`
("no trap") and a batch of `exit_code=0` ("every guest passed"). On the
machine where `sandbox.*` is most used, that was the only answer either
could give, whatever the guests actually did.
"""

from __future__ import annotations

from amiga_fleet_mcp.tools.sandbox import (
    _parse_per_guest_exits,
    _ring_reports_daemon,
)

GPU_NOISE = [
    "[gpu warn] dpm levels periodic sample: sclk 86600 mclk 137500",
    "[gpu.chip] 2D batch: 733 op(s) on the engine in 733 submission(s)",
    "[dcsvc] op 3 -> OK",
    "[gpu warn] output-probe: pipe 0 master_en 1, blank_data_en 0",
]


def test_ring_full_of_other_peoples_output_is_not_usable() -> None:
    """The X5000 case: a busy ring that has never heard of us."""
    assert _ring_reports_daemon(GPU_NOISE) is False


def test_empty_ring_is_not_usable() -> None:
    assert _ring_reports_daemon([]) is False


def test_daemon_beacon_makes_it_usable() -> None:
    lines = [*GPU_NOISE, "[MCPd] ready name=MCPd version=1.4 port=4322"]
    assert _ring_reports_daemon(lines) is True


def test_sandboxvm_output_makes_it_usable() -> None:
    """SandboxVM's own lines count: it is the other thing these tools
    read the ring for."""
    lines = [*GPU_NOISE,
             "[sandboxvm] guest_run_elf RAM:hello returned 0"]
    assert _ring_reports_daemon(lines) is True


def test_a_quiet_but_working_ring_is_usable() -> None:
    """Absence of traps is not absence of the daemon -- a machine that
    simply has not crashed still shows its beacons."""
    lines = ["[MCPd] input_gate state=disabled source=off",
             "[MCPd] ready name=MCPd version=1.4 port=4322 input=off"]
    assert _ring_reports_daemon(lines) is True


# ---- the per-guest exit parsing this protects -----------------------


def test_exits_parse_in_batch_order() -> None:
    lines = [
        "[sandboxvm] guest_run_elf RAM:a returned 0; calling guest_destroy",
        "[sandboxvm] guest_run_elf RAM:b returned -768; calling guest_destroy",
        "[sandboxvm] guest_run_elf RAM:c returned 0; calling guest_destroy",
    ]
    assert _parse_per_guest_exits(lines, 3) == [
        ("RAM:a", 0), ("RAM:b", -768), ("RAM:c", 0),
    ]


def test_exits_from_an_unusable_ring_are_absent_not_zero() -> None:
    """The distinction the callers now make: nothing parsed, so there
    is nothing to report -- as opposed to three guests that passed."""
    assert _parse_per_guest_exits(GPU_NOISE, 3) == []
    assert _ring_reports_daemon(GPU_NOISE) is False


def test_only_the_trailing_window_is_kept() -> None:
    """A ring holding more runs than this batch had guests must not
    contribute exits from an earlier batch."""
    lines = [
        "[sandboxvm] guest_run_elf RAM:old returned 42",
        "[sandboxvm] guest_run_elf RAM:a returned 0",
        "[sandboxvm] guest_run_elf RAM:b returned 0",
    ]
    assert _parse_per_guest_exits(lines, 2) == [("RAM:a", 0), ("RAM:b", 0)]
