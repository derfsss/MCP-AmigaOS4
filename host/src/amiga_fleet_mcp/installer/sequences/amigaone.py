"""AmigaOne (XE/SE original A1 series) install sequence."""

from __future__ import annotations

from ._generic import build_sequence
from ._machine_config import AMIGAONE_CONFIG
from ._runner import Sequence


def build(*, iso_filename: str) -> Sequence:
    return build_sequence(AMIGAONE_CONFIG, iso_filename=iso_filename)
