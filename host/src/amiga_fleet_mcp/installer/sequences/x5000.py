"""X5000 install sequence.

Composes the parameterised step builders from _steps.py with the
X5000 MachineConfig. Sequence shape:

  stage_diskimage_tools, mount_iso, verify_cd_signature, copy_base_os,
  copy_installation_extras, install_bootloader (skipped),
  apply_update_2, apply_update_3, install_enhancer, install_ngf,
  unmount_iso
"""

from __future__ import annotations

from ._generic import build_sequence
from ._machine_config import X5000_CONFIG
from ._runner import Sequence


def build(*, iso_filename: str) -> Sequence:
    return build_sequence(X5000_CONFIG, iso_filename=iso_filename)
