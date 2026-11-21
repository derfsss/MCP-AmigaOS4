"""X5000 install sequence.

Composes the parameterised step builders from _steps.py with the
X5000 MachineConfig via _generic.build_sequence. Sequence shape
(pinned by tests/unit/test_installer_x5000_seq.py):

  stage_diskimage_tools, extract_iso_lha, mount_iso,
  verify_cd_signature, copy_base_os, copy_installation_extras,
  install_bootloader (skipped), apply_update_2, apply_update_3,
  install_enhancer, install_ngf, install_extras, patch_amidock_prefs,
  install_network_config, install_serialshell, quarantine_emu10kx,
  quarantine_devs_monitors, install_mcpd, unmount_iso,
  dismount_combi_device, cleanup_tmp
"""

from __future__ import annotations

from ._generic import build_sequence
from ._machine_config import X5000_CONFIG
from ._runner import Sequence


def build(*, iso_filename: str) -> Sequence:
    return build_sequence(X5000_CONFIG, iso_filename=iso_filename)
