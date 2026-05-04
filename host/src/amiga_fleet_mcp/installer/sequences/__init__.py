"""Per-machine install sequences.

Each `<machine>.py` defines a `build()` function that returns a
`Sequence` (list of named `Step`s). A SequenceRunner executes those
steps in order with halt-on-failure and per-step timing.

Currently implemented:
    x5000  — full bootable install for AmigaOne X5000

Deferred to follow-up sessions:
    a1222     — needs RescueV2 USB on the target
    pegasos2  — needs Pegasos2 install ISO + QEMU validation
    amigaone  — original A1XE/A1-SE
    sam460    — Sam460ex
"""

from ._runner import (
    Sequence,
    SequenceResult,
    SequenceRunner,
    Step,
    StepResult,
)

__all__ = [
    "Sequence",
    "SequenceResult",
    "SequenceRunner",
    "Step",
    "StepResult",
]
