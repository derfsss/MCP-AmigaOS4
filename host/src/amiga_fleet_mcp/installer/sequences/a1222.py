"""A1222 (Tabor) install sequence.

Standard pipeline: ISO + Enhancer 2.2 LHA + Update 3.
Deliberately does NOT use the RescueV2 USB path; the simpler shared
flow used by every other machine is sufficient.

A1222-specific bits handled by the generic builder:
  - KickstartA1222 sub-package per update
  - A1222eth.device from Enhancer LHA
  - skip_bootloader=True (A1222 boot loader lives on the SD card,
    not in dest)
  - cd_signature "AmigaOS-A1222"

Caveat: a1222.audio (the per-machine audio driver) is NOT in the
Enhancer LHA — it shipped only on RescueV2. Installs via this path
will not have audio. Add a1222.audio manually post-install if needed.
"""

from __future__ import annotations

from ._generic import build_sequence
from ._machine_config import A1222_CONFIG
from ._runner import Sequence


def build(*, iso_filename: str) -> Sequence:
    return build_sequence(A1222_CONFIG, iso_filename=iso_filename)
