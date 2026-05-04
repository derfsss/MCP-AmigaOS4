"""Machine-data sanity tests.

The static tables in amiga_fleet_mcp.installer.machines describe the
machines we support. These tests pin the values so accidental drift
is caught.
"""

from __future__ import annotations

from amiga_fleet_mcp.installer import (
    A1222_BACKUP_SIGNATURE,
    A1222_BACKUP_VOLS,
    CD_VERSION_SIGNATURES,
    FORBIDDEN_DEST_ENTRIES,
    ISO_PREFIX_TO_MACHINE,
    MACHINE_ALIASES,
    MACHINES_NO_BOOTLOADER_FILE,
    SUPPORTED_MACHINES,
    UNSUPPORTED_ISO_PREFIXES,
    UPDATE_LHA_NAME,
    UPDATES_TO_APPLY,
    detect_machine_from_iso,
    resolve_machine_alias,
)


def test_supported_machines_match_iso_prefixes():
    ids_from_prefixes = {info[0] for _, info in ISO_PREFIX_TO_MACHINE}
    assert set(SUPPORTED_MACHINES) == ids_from_prefixes


def test_iso_prefix_order_longer_first():
    """X5000/A1222 prefixes must appear before bare AmigaOne so the
    detector matches the more specific prefix first."""
    prefixes = [p for p, _ in ISO_PREFIX_TO_MACHINE]
    a1_idx = prefixes.index("AmigaOneInstallCD")
    x5_idx = prefixes.index("AmigaOneX5000InstallCD")
    a1222_idx = prefixes.index("AmigaOneA1222InstallCD")
    assert x5_idx < a1_idx
    assert a1222_idx < a1_idx


def test_detect_machine_specific_before_generic():
    info = detect_machine_from_iso("AmigaOneX5000InstallCD-53.42.iso")
    assert info == ("X5000", "AmigaOne X5000")
    info = detect_machine_from_iso("AmigaOneInstallCD-53.30.iso")
    assert info == ("AmigaOne", "AmigaOne (XE/SE)")
    info = detect_machine_from_iso("AmigaOneA1222InstallCD-53.37.iso")
    assert info == ("A1222", "AmigaOne A1222")


def test_detect_machine_returns_none_for_unknown():
    assert detect_machine_from_iso("Unknown.iso") is None
    assert detect_machine_from_iso("ClassicInstallCD-3.1.iso") is None


def test_unsupported_prefixes_are_disjoint_from_supported():
    """Unsupported and supported prefix lists must not overlap."""
    sup = {p for p, _ in ISO_PREFIX_TO_MACHINE}
    for u in UNSUPPORTED_ISO_PREFIXES:
        assert u not in sup, f"{u!r} is both supported and unsupported"


def test_resolve_machine_alias_canonical():
    for m in SUPPORTED_MACHINES:
        assert resolve_machine_alias(m) == m


def test_resolve_machine_alias_case_insensitive():
    assert resolve_machine_alias("X5000") == "X5000"
    assert resolve_machine_alias("x5000") == "X5000"
    assert resolve_machine_alias("AMIGAONE X5000") == "X5000"
    assert resolve_machine_alias("PEG2") == "PegasosII"
    assert resolve_machine_alias("a1xe") == "AmigaOne"


def test_resolve_machine_alias_unknown():
    assert resolve_machine_alias("amiga500") is None
    assert resolve_machine_alias("") is None


def test_aliases_resolve_to_supported_machines():
    for alias, machine_id in MACHINE_ALIASES.items():
        assert machine_id in SUPPORTED_MACHINES, (
            f"alias {alias!r} -> {machine_id!r} which isn't supported"
        )


def test_updates_to_apply_keys_are_supported():
    for m in UPDATES_TO_APPLY:
        assert m in SUPPORTED_MACHINES


def test_updates_apply_known_lha_indices():
    for indices in UPDATES_TO_APPLY.values():
        for i in indices:
            assert i in UPDATE_LHA_NAME, (
                f"UPDATES_TO_APPLY references update {i} but no "
                "UPDATE_LHA_NAME mapping for it"
            )


def test_updates_specific_per_machine():
    """Pin the per-machine update lists."""
    assert UPDATES_TO_APPLY["AmigaOne"] == [1, 2, 3]
    assert UPDATES_TO_APPLY["PegasosII"] == [1, 2, 3]
    assert UPDATES_TO_APPLY["Sam460"] == [1, 2, 3]
    assert UPDATES_TO_APPLY["X5000"] == [2, 3]
    assert UPDATES_TO_APPLY["A1222"] == [3]


def test_no_bootloader_file_machines():
    assert "X5000" in MACHINES_NO_BOOTLOADER_FILE
    assert "A1222" in MACHINES_NO_BOOTLOADER_FILE


def test_a1222_backup_constants_nonempty():
    assert len(A1222_BACKUP_VOLS) >= 1
    assert all(v.endswith(":") for v in A1222_BACKUP_VOLS)
    assert "/" in A1222_BACKUP_SIGNATURE  # path-like


def test_cd_version_signatures_cover_iso_machines():
    """All machines with CD-Version.txt signatures must be supported."""
    for m in CD_VERSION_SIGNATURES:
        assert m in SUPPORTED_MACHINES
    assert "Sam460" not in CD_VERSION_SIGNATURES  # no CD-Version.txt


def test_forbidden_dest_entries_are_paths():
    assert "S/Startup-Sequence" in FORBIDDEN_DEST_ENTRIES
    assert "Kickstart" in FORBIDDEN_DEST_ENTRIES
    for e in FORBIDDEN_DEST_ENTRIES:
        assert ":" not in e, f"{e!r} should be relative, not absolute"
