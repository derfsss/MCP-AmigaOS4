"""Per-machine sequence shape tests for PegasosII, AmigaOne, Sam460.

Pinning the shapes catches accidental changes to the generic
sequence builder. Each machine has slightly different content:

  X5000:     bootloader skipped, NGFS step, updates [2, 3]
  PegasosII: bootloader fires, no NGFS, updates [1, 2, 3]
  AmigaOne:  bootloader fires, no NGFS, updates [1, 2, 3], A1 packages
  Sam460:    bootloader fires, no NGFS, updates [1, 2, 3], no CD sig
"""

from __future__ import annotations

from amiga_fleet_mcp.installer.sequences import (
    a1222 as a1222_seq,
)
from amiga_fleet_mcp.installer.sequences import (
    amigaone as amigaone_seq,
)
from amiga_fleet_mcp.installer.sequences import (
    pegasos2 as pegasos2_seq,
)
from amiga_fleet_mcp.installer.sequences import (
    sam460 as sam460_seq,
)


def _names(seq):
    return [s.name for s in seq.steps]


def test_pegasos2_sequence_shape():
    seq = pegasos2_seq.build(iso_filename="Pegasos2InstallCD-53.42.iso")
    assert seq.machine == "PegasosII"
    names = _names(seq)
    # All 3 updates apply
    assert "apply_update_1" in names
    assert "apply_update_2" in names
    assert "apply_update_3" in names
    # Standard pipeline elements
    assert "install_bootloader" in names
    assert "install_enhancer" in names
    # Pegasos2 doesn't get NGFS or emu10kx quarantine (no config flag)
    assert "install_ngf" not in names
    assert "quarantine_emu10kx" not in names
    # Mount + unmount fence the install; cleanup runs last.
    assert names[0] == "stage_diskimage_tools"
    assert names[1] == "extract_iso_lha"
    assert names[2] == "mount_iso"
    assert names[-2] == "unmount_iso"
    assert names[-1] == "cleanup_tmp"


def test_amigaone_sequence_shape():
    seq = amigaone_seq.build(iso_filename="AmigaOneInstallCD-53.42.iso")
    assert seq.machine == "AmigaOne"
    names = _names(seq)
    for n in ("install_bootloader", "install_enhancer",
              "apply_update_1", "apply_update_2", "apply_update_3"):
        assert n in names
    assert "install_ngf" not in names


def test_a1222_sequence_shape():
    """A1222 simplified pipeline (no RescueV2 USB) — Update 3 only,
    Enhancer LHA flow, A1222eth.device, skip_bootloader."""
    seq = a1222_seq.build(iso_filename="AmigaOneA1222InstallCD-53.37.iso")
    assert seq.machine == "A1222"
    names = _names(seq)
    # Only Update 3 (Updates 1 + 2 baked into the A1222 ISO)
    assert "apply_update_3" in names
    assert "apply_update_1" not in names
    assert "apply_update_2" not in names
    # Standard pipeline elements (no NGFS, no emu10kx quarantine)
    assert "install_enhancer" in names
    assert "install_ngf" not in names
    assert "quarantine_emu10kx" not in names
    # Bootloader is skipped at runtime (skip_bootloader=True), but
    # the step is still structurally in the sequence.
    assert "install_bootloader" in names


def test_sam460_sequence_shape():
    seq = sam460_seq.build(iso_filename="Sam460InstallCD-53.42.iso")
    assert seq.machine == "Sam460"
    names = _names(seq)
    for n in ("install_bootloader", "install_enhancer",
              "apply_update_1", "apply_update_2", "apply_update_3"):
        assert n in names
    assert "install_ngf" not in names
    # verify_cd_signature is still in the sequence but its doc says
    # "skipped (Sam460 ISO has no signature)" since Sam460 ships
    # without CD-Version.txt.
    sig_step = next(s for s in seq.steps if s.name == "verify_cd_signature")
    assert "skipped" in sig_step.doc.lower() or "no signature" in sig_step.doc


def test_all_implemented_sequences_have_same_skeleton():
    """Each machine sequence must start with the same prelude + end
    with the same epilogue. This pins the structural invariant."""
    expected_prelude = [
        "stage_diskimage_tools",
        "extract_iso_lha",
        "mount_iso",
        "verify_cd_signature",
        "copy_base_os",
        "copy_installation_extras",
        "install_bootloader",
    ]
    # Universal post-install steps in order: extras (Filer/Ranger/
    # IBrowse/DiskImage), AmiDock prefs, Network-Startup baseline,
    # SerialShell auto-start, MCPd auto-start, then unmount + clean.
    # quarantine_emu10kx (X5000-only) sits between SerialShell and
    # MCPd when present, so we only pin the tail to the universal bits.
    expected_epilogue = ["install_mcpd", "unmount_iso", "cleanup_tmp"]
    for seq in (
        pegasos2_seq.build(iso_filename="Pegasos2InstallCD-53.42.iso"),
        amigaone_seq.build(iso_filename="AmigaOneInstallCD-53.42.iso"),
        sam460_seq.build(iso_filename="Sam460InstallCD-53.42.iso"),
        a1222_seq.build(iso_filename="AmigaOneA1222InstallCD-53.37.iso"),
    ):
        names = _names(seq)
        assert names[: len(expected_prelude)] == expected_prelude, seq.machine
        assert names[-len(expected_epilogue):] == expected_epilogue, seq.machine
