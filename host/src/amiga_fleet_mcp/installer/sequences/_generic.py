"""Generic per-machine sequence builder.

Composes the parameterised step builders in _steps.py into a Sequence
based on a MachineConfig + the per-machine UPDATES_TO_APPLY entry.
"""

from __future__ import annotations

from ..machines import UPDATES_TO_APPLY
from . import _steps as steps
from ._machine_config import MachineConfig
from ._runner import Sequence, Step


def build_sequence(config: MachineConfig, *, iso_filename: str) -> Sequence:
    machine = config["machine_id"]
    updates = UPDATES_TO_APPLY.get(machine, [])

    seq_steps: list[Step] = [
        steps.stage_diskimage_tools(),
        steps.extract_iso_lha(),
        steps.mount_iso(),
        steps.verify_cd_signature(config),
        steps.copy_base_os(),
        steps.copy_installation_extras(),
        steps.install_bootloader(config),
    ]
    for n in updates:
        seq_steps.append(steps.apply_one_update(config, n))
    if config.get("needs_enhancer"):
        seq_steps.append(steps.install_enhancer(config))
    if config.get("needs_ngf"):
        seq_steps.append(steps.install_ngf())
    # Universal post-driver-install steps. Order matters:
    #   install_extras runs while ISO is still mounted (it reads
    #     <ISO>:Installation-Files/Extras/).
    #   patch_amidock_prefs reads <dest>:tmp/AmiDock.amiga.com.xml
    #     (staged) and overwrites the dock prefs.
    #   install_network_config writes a baseline S:Network-Startup
    #     so install_mcpd has something to extend.
    #   install_serialshell runs before install_mcpd so SerialShell
    #     is available as a fallback if MCPd doesn't auto-start.
    seq_steps.append(steps.install_extras())
    seq_steps.append(steps.patch_amidock_prefs())
    seq_steps.append(steps.install_network_config(machine))
    seq_steps.append(steps.install_serialshell())
    if config.get("quarantine_emu10kx"):
        seq_steps.append(steps.quarantine_emu10kx())
    if config.get("quarantine_devs_monitors"):
        seq_steps.append(steps.quarantine_devs_monitors())
    # MCPd auto-start so the freshly installed system is reachable
    # on :4322 from first boot. Must come AFTER
    # install_network_config (which writes the baseline file MCPd
    # appends to).
    seq_steps.append(steps.install_mcpd())
    seq_steps.append(steps.unmount_iso())
    seq_steps.append(steps.dismount_combi_device())
    seq_steps.append(steps.cleanup_tmp())

    return Sequence(
        machine=machine,
        description=(f"Native AmigaOS 4.1 FE install for {machine} "
                     "(boot-critical pipeline)"),
        steps=seq_steps,
    )
