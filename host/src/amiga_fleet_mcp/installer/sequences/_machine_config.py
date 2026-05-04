"""Per-machine config dicts consumed by the parameterised step builders.

Each machine sequence (x5000.py, pegasos2.py, ...) is a thin wrapper
that imports its config + builds a Sequence by combining common
steps from _steps.py with any machine-specific steps.
"""

from __future__ import annotations

from typing import TypedDict


class MachineConfig(TypedDict, total=False):
    machine_id: str
    cd_signature: str | None       # None for Sam460 (no CD-Version.txt)
    kick_sub: str | None           # MACHINE_PKGS[m][0]
    wb_sub: str | None             # MACHINE_PKGS[m][1]
    ethernet_driver: str | None    # Enhancer's per-machine eth driver
    skip_bootloader: bool          # in MACHINES_NO_BOOTLOADER_FILE?
    needs_ngf: bool                # X5000-only NGFileSystem step
    needs_enhancer: bool           # all except A1222
    needs_a1222_extras: bool       # A1222 only
    kicklayout_replacements: list[dict[str, str]]
    bootloader_filename: str       # ISO source -> dest, for non-skip
    quarantine_emu10kx: bool       # SBLive driver bus-errors on X5000/A1222
    quarantine_devs_monitors: bool # move DEVS:Monitors/* -> Storage/Monitors/*


X5000_CONFIG: MachineConfig = {
    "machine_id": "X5000",
    "cd_signature": "AmigaOS-X5000",
    "kick_sub": "KickstartX5000",
    "wb_sub": "WorkbenchX5000",
    "ethernet_driver": "p50x0_eth.device",
    "skip_bootloader": True,
    "needs_ngf": True,
    "needs_enhancer": True,
    "needs_a1222_extras": False,
    "kicklayout_replacements": [
        {"old": "p5020sata.device.kmod",
         "new": "p50x0sata.device.kmod"},
    ],
    "bootloader_filename": "amigaboot.of",
    "quarantine_emu10kx": True,
    "quarantine_devs_monitors": True,  # X5000: RadeonHD/RX handles RTG
}


PEGASOS2_CONFIG: MachineConfig = {
    "machine_id": "PegasosII",
    "cd_signature": "AmigaOS-Pegasos2",
    "kick_sub": "KickstartPeg2",
    "wb_sub": None,
    "ethernet_driver": None,
    "skip_bootloader": False,
    "needs_ngf": False,
    "needs_enhancer": True,
    "needs_a1222_extras": False,
    "kicklayout_replacements": [],
    "bootloader_filename": "amigaboot.of",
}


AMIGAONE_CONFIG: MachineConfig = {
    "machine_id": "AmigaOne",
    "cd_signature": "AmigaOS-AmigaOne",
    "kick_sub": "KickstartA1",
    "wb_sub": "WorkbenchA1",
    "ethernet_driver": None,
    "skip_bootloader": False,
    "needs_ngf": False,
    "needs_enhancer": True,
    "needs_a1222_extras": False,
    "kicklayout_replacements": [],
    "bootloader_filename": "amigaboot.of",
}


SAM460_CONFIG: MachineConfig = {
    "machine_id": "Sam460",
    "cd_signature": None,  # Sam460 ISO ships without CD-Version.txt
    "kick_sub": "KickstartSam460",
    "wb_sub": None,
    "ethernet_driver": None,
    "skip_bootloader": False,
    "needs_ngf": False,
    "needs_enhancer": True,
    "needs_a1222_extras": False,
    "kicklayout_replacements": [],
    "bootloader_filename": "amigaboot.of",
}


A1222_CONFIG: MachineConfig = {
    # A1222 follows the standard ISO + Enhancer + Update 3 pipeline
    # (deliberately NOT using RescueV2 USB — the user opted out of
    # that path 2026-05-02 in favour of the simpler enhancer-LHA
    # flow that the other machines use).
    #
    # The Enhancer 2.2 LHA carries A1222eth.device alongside the
    # X5000's p50x0_eth.device, so install_enhancer's per-machine
    # ethernet_driver branch handles A1222 cleanly. a1222.audio is
    # NOT in the Enhancer LHA (it's a RescueV2-only file) so an
    # A1222 install via this path lands without audio support — the
    # user accepted that trade-off for installer simplicity.
    "machine_id": "A1222",
    "cd_signature": "AmigaOS-A1222",
    "kick_sub": "KickstartA1222",
    "wb_sub": None,
    "ethernet_driver": "A1222eth.device",
    "skip_bootloader": True,        # in MACHINES_NO_BOOTLOADER_FILE
    "needs_ngf": False,
    "needs_enhancer": True,         # standard LHA flow (was: RescueV2)
    "needs_a1222_extras": False,    # RescueV2 path retired 2026-05-02
    "quarantine_emu10kx": False,    # A1222 has its own audio chain
    "kicklayout_replacements": [],
    "bootloader_filename": "amigaboot.of",
}


CONFIGS: dict[str, MachineConfig] = {
    "X5000":     X5000_CONFIG,
    "PegasosII": PEGASOS2_CONFIG,
    "AmigaOne":  AMIGAONE_CONFIG,
    "Sam460":    SAM460_CONFIG,
    "A1222":     A1222_CONFIG,
}
