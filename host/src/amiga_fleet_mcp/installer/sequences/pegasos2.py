"""PegasosII install sequence.

Composes the parameterised step builders with the PegasosII config.
Updates 1+2+3 + Enhancer 2.2; install_bootloader fires (Pegasos2
uses amigaboot.of on the dest partition); no NGFS step.
"""

from __future__ import annotations

from ._generic import build_sequence
from ._machine_config import PEGASOS2_CONFIG
from ._runner import Sequence


def build(*, iso_filename: str) -> Sequence:
    return build_sequence(PEGASOS2_CONFIG, iso_filename=iso_filename)
