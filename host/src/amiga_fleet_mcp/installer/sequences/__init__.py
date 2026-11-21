"""Per-machine install sequences.

Each `<machine>.py` defines a `build()` function that returns a
`Sequence` (list of named `Step`s). A SequenceRunner executes those
steps in order with halt-on-failure and per-step timing.

Implemented, each a thin wrapper over `_generic.build_sequence` with
its config from `_machine_config.py`:
    x5000     — AmigaOne X5000
    a1222     — A1222 (Tabor); standard Enhancer LHA flow, no RescueV2
    pegasos2  — Pegasos II
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
