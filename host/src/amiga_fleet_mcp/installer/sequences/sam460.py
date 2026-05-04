"""Sam460ex install sequence.

Sam460 ISO ships without CD-Version.txt so verify_cd_signature is a
no-op. Otherwise the standard pipeline: full updates + Enhancer.
"""

from __future__ import annotations

from ._generic import build_sequence
from ._machine_config import SAM460_CONFIG
from ._runner import Sequence


def build(*, iso_filename: str) -> Sequence:
    return build_sequence(SAM460_CONFIG, iso_filename=iso_filename)
