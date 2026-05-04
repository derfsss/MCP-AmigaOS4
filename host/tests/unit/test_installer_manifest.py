"""Per-machine required-files manifest + sources scan tests."""

from __future__ import annotations

import pytest

from amiga_fleet_mcp.installer import required_files, scan_sources


def _by_role(manifest, role):
    return [e for e in manifest if e["role"] == role]


def test_required_files_x5000():
    m = required_files("X5000")
    iso = _by_role(m, "iso")
    updates = _by_role(m, "update")
    enhancer = _by_role(m, "enhancer")
    extras = _by_role(m, "extra")

    assert len(iso) == 1
    # X5000 official ISO is `AmigaOneX5000InstallCD-<rev>.iso`
    # (Hyperion release naming). The "New..." variant once listed
    # was an unofficial rename / mislabel and never shipped.
    assert iso[0]["iso_prefix"] == "AmigaOneX5000InstallCD"

    # X5000 only needs updates 2 + 3 (1 baked in)
    update_indices = sorted(int(e["update_index"]) for e in updates)
    assert update_indices == [2, 3]

    assert len(enhancer) == 1
    assert enhancer[0]["filename"] == "enhancer_software_2.2.lha"

    extra_names = sorted(e["filename"] for e in extras)
    assert extra_names == ["NGFCheck.lha", "NGFS.lha"]


def test_required_files_a1222():
    """A1222 (post-2026-05-02 simplification): standard ISO + Enhancer
    + Update 3 pipeline. The previous RescueV2 USB special case was
    retired in favour of the shared flow."""
    m = required_files("A1222")
    assert len(_by_role(m, "enhancer")) == 1
    assert _by_role(m, "extra") == []
    update_indices = sorted(int(e["update_index"]) for e in _by_role(m, "update"))
    assert update_indices == [3]


def test_required_files_pegasos2_full_updates():
    m = required_files("PegasosII")
    update_indices = sorted(int(e["update_index"]) for e in _by_role(m, "update"))
    assert update_indices == [1, 2, 3]
    assert len(_by_role(m, "enhancer")) == 1
    assert _by_role(m, "extra") == []


def test_required_files_amigaone_classic():
    m = required_files("AmigaOne")
    assert len(_by_role(m, "enhancer")) == 1
    assert _by_role(m, "extra") == []


def test_required_files_sam460():
    m = required_files("Sam460")
    update_indices = sorted(int(e["update_index"]) for e in _by_role(m, "update"))
    assert update_indices == [1, 2, 3]
    assert len(_by_role(m, "enhancer")) == 1
    assert _by_role(m, "extra") == []


def test_required_files_unknown_machine_raises():
    with pytest.raises(ValueError):
        required_files("DragonBall")


# ---- scan_sources -------------------------------------------------


def test_scan_sources_missing_dir(tmp_path):
    out = scan_sources(tmp_path / "nonexistent")
    assert out["exists"] is False
    assert out["iso_files"] == {}
    assert out["lha_files"] == []


def test_scan_sources_empty_dir(tmp_path):
    out = scan_sources(tmp_path)
    assert out["exists"] is True
    assert out["detected_machine"] is None
    assert out["iso_files"] == {}
    assert out["lha_files"] == []


def test_scan_sources_x5000_full_set(tmp_path):
    """A typical X5000 sources directory should auto-detect cleanly."""
    (tmp_path / "AmigaOneX5000InstallCD-53.42.iso").write_bytes(b"")
    (tmp_path / "AmigaOS4.1FinalEditionUpdate2-53.14.lha").write_bytes(b"")
    (tmp_path / "AmigaOS4.1FinalEditionUpdate3-53.34.lha").write_bytes(b"")
    (tmp_path / "enhancer_software_2.2.lha").write_bytes(b"")
    (tmp_path / "NGFCheck.lha").write_bytes(b"")
    (tmp_path / "NGFS.lha").write_bytes(b"")

    out = scan_sources(tmp_path)
    assert out["detected_machine"] == "X5000"
    assert out["iso_files"]["X5000"] == ["AmigaOneX5000InstallCD-53.42.iso"]
    assert "NGFS.lha" in out["lha_files"]
    assert out["unsupported_isos"] == []
    assert out["ambiguous_machines"] == []


def test_scan_sources_unsupported_iso_separated(tmp_path):
    (tmp_path / "AmigaOneX5000InstallCD-53.42.iso").write_bytes(b"")
    (tmp_path / "ClassicInstallCD-3.1.iso").write_bytes(b"")
    out = scan_sources(tmp_path)
    assert out["detected_machine"] == "X5000"
    assert out["unsupported_isos"] == ["ClassicInstallCD-3.1.iso"]


def test_scan_sources_ambiguous_when_multiple_machines(tmp_path):
    (tmp_path / "AmigaOneX5000InstallCD-53.42.iso").write_bytes(b"")
    (tmp_path / "Pegasos2InstallCD-53.34.iso").write_bytes(b"")
    out = scan_sources(tmp_path)
    assert out["detected_machine"] is None
    assert sorted(out["ambiguous_machines"]) == ["PegasosII", "X5000"]


def test_scan_sources_iso_lha_only(tmp_path):
    """When only the LHA-of-ISO form is present (Hyperion-shipped),
    detect_machine works and iso_lha_files is populated."""
    (tmp_path / "AmigaOneX5000InstallCD-53.25.iso.lha").write_bytes(b"")
    out = scan_sources(tmp_path)
    assert out["detected_machine"] == "X5000"
    assert out["iso_files"] == {}
    assert out["iso_lha_files"]["X5000"] == [
        "AmigaOneX5000InstallCD-53.25.iso.lha",
    ]
    # Generic .lha files list shouldn't include the iso.lha.
    assert "AmigaOneX5000InstallCD-53.25.iso.lha" not in out["lha_files"]


def test_scan_sources_iso_and_iso_lha_both(tmp_path):
    """When both raw .iso and .iso.lha are present, both buckets get
    populated and detected_machine is the (single) machine."""
    (tmp_path / "AmigaOneX5000InstallCD-53.25.iso").write_bytes(b"")
    (tmp_path / "AmigaOneX5000InstallCD-53.25.iso.lha").write_bytes(b"")
    out = scan_sources(tmp_path)
    assert out["detected_machine"] == "X5000"
    assert "AmigaOneX5000InstallCD-53.25.iso" in out["iso_files"]["X5000"]
    assert "AmigaOneX5000InstallCD-53.25.iso.lha" in out["iso_lha_files"]["X5000"]


def test_scan_sources_unknown_iso_lha_falls_through_to_lha_files(tmp_path):
    """An .iso.lha whose iso name doesn't match any supported prefix
    should appear in lha_files, not iso_lha_files."""
    (tmp_path / "RandomThing.iso.lha").write_bytes(b"")
    out = scan_sources(tmp_path)
    assert out["iso_lha_files"] == {}
    assert "RandomThing.iso.lha" in out["lha_files"]
