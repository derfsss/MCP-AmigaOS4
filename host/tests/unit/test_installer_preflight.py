"""Preflight composition test using a fake MCPd transport.

Drives installer.preflight against a stubbed Amiga side so we can
exercise every verdict branch without booting QEMU.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from amiga_fleet_mcp.config import (
    Config,
    McpdChannel,
    TargetChannels,
    TargetConfig,
)
from amiga_fleet_mcp.errors import TargetError
from amiga_fleet_mcp.fleet import Fleet
from amiga_fleet_mcp.tools import installer as installer_tool


class FakeMcpd:
    """Same stub pattern as test_tools_with_fake_transport."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []
        # Map of paths that exist on the simulated Amiga side.
        self.existing_paths: set[str] = set()

    def add_path(self, path: str) -> None:
        self.existing_paths.add(path)

    async def request(self, method: str, params: dict | None = None,
                      timeout_s: float = 30.0) -> Any:
        self.calls.append((method, params))
        if method == "fs.stat":
            assert params is not None
            p = params["path"]
            if p in self.existing_paths:
                return {"type": "dir", "size": 0}
            raise TargetError(
                f"path not found: {p!r}",
                data={"path": p},
            )
        return None


@pytest.fixture
def fleet_with_fake() -> tuple[Fleet, FakeMcpd]:
    cfg = Config(
        targets={
            "x5000": TargetConfig(
                type="remote",
                channels=TargetChannels(
                    mcpd=McpdChannel(endpoint="192.168.0.25:4322"),
                ),
            )
        }
    )
    fleet = Fleet(cfg)
    fake = FakeMcpd()
    fleet._mcpd["x5000"] = fake  # type: ignore[assignment]
    return fleet, fake


def _make_x5000_sources(d: Path) -> None:
    (d / "AmigaOneX5000InstallCD-53.42.iso").write_bytes(b"")
    (d / "AmigaOS4.1FinalEditionUpdate2-53.14.lha").write_bytes(b"")
    (d / "AmigaOS4.1FinalEditionUpdate3-53.34.lha").write_bytes(b"")
    (d / "enhancer_software_2.2.lha").write_bytes(b"")
    (d / "NGFCheck.lha").write_bytes(b"")
    (d / "NGFS.lha").write_bytes(b"")
    # mandatory binaries (preflight cross-checks these)
    (d / "MCPd").write_bytes(b"mcpd")
    (d / "SerialShell").write_bytes(b"ss")
    bs = d / "diskimage-bootstrap"
    bs.mkdir(exist_ok=True)
    (bs / "MountDiskImage").write_bytes(b"mdi")
    (bs / "diskimage.device").write_bytes(b"dim")
    (bs / "CDFileSystem").write_bytes(b"cdfs")


@pytest.mark.asyncio
async def test_preflight_happy_path(fleet_with_fake, tmp_path):
    fleet, fake = fleet_with_fake
    _make_x5000_sources(tmp_path)
    fake.add_path("BootTest:")  # only the volume root exists

    res = await installer_tool.installer_preflight(
        fleet, "x5000",
        dest_volume="BootTest:",
        sources_dir=str(tmp_path),
    )

    assert res.overall == "ok"
    assert res.machine == "X5000"
    assert res.iso == "AmigaOneX5000InstallCD-53.42.iso"
    assert res.missing_files == []
    assert res.forbidden_entries_present == []
    # All checks passed
    assert all(c.status in ("ok", "warn") for c in res.checks)


@pytest.mark.asyncio
async def test_preflight_rejects_system_volume(fleet_with_fake, tmp_path):
    fleet, _ = fleet_with_fake
    _make_x5000_sources(tmp_path)

    res = await installer_tool.installer_preflight(
        fleet, "x5000",
        dest_volume="SYS:",
        sources_dir=str(tmp_path),
    )
    assert res.overall == "fail"
    fail_names = {c.name for c in res.checks if c.status == "fail"}
    assert "dest_not_system" in fail_names


@pytest.mark.asyncio
async def test_preflight_missing_sources_dir(fleet_with_fake, tmp_path):
    fleet, _ = fleet_with_fake
    res = await installer_tool.installer_preflight(
        fleet, "x5000",
        dest_volume="BootTest:",
        sources_dir=str(tmp_path / "nope"),
    )
    assert res.overall == "fail"
    assert res.summary.startswith("sources_dir not found")


@pytest.mark.asyncio
async def test_preflight_dest_not_mounted(fleet_with_fake, tmp_path):
    fleet, _fake = fleet_with_fake
    _make_x5000_sources(tmp_path)
    # Don't add BootTest: to fake.existing_paths
    res = await installer_tool.installer_preflight(
        fleet, "x5000",
        dest_volume="BootTest:",
        sources_dir=str(tmp_path),
    )
    assert res.overall == "fail"
    fail_names = {c.name for c in res.checks if c.status == "fail"}
    assert "dest_mounted" in fail_names


@pytest.mark.asyncio
async def test_preflight_missing_lhas(fleet_with_fake, tmp_path):
    fleet, fake = fleet_with_fake
    # Put only the ISO; updates / enhancer / NGFS missing.
    (tmp_path / "AmigaOneX5000InstallCD-53.42.iso").write_bytes(b"")
    fake.add_path("BootTest:")

    res = await installer_tool.installer_preflight(
        fleet, "x5000",
        dest_volume="BootTest:",
        sources_dir=str(tmp_path),
    )
    assert res.overall == "fail"
    assert "AmigaOS4.1FinalEditionUpdate2-53.14.lha" in res.missing_files
    assert "enhancer_software_2.2.lha" in res.missing_files
    assert "NGFS.lha" in res.missing_files
    assert "NGFCheck.lha" in res.missing_files


@pytest.mark.asyncio
async def test_preflight_forbidden_entries(fleet_with_fake, tmp_path):
    fleet, fake = fleet_with_fake
    _make_x5000_sources(tmp_path)
    fake.add_path("BootTest:")
    # Simulate a previous AOS install on the dest.
    fake.add_path("BootTest:System")
    fake.add_path("BootTest:Kickstart")

    res = await installer_tool.installer_preflight(
        fleet, "x5000",
        dest_volume="BootTest:",
        sources_dir=str(tmp_path),
    )
    assert res.overall == "fail"
    assert "System" in res.forbidden_entries_present
    assert "Kickstart" in res.forbidden_entries_present


@pytest.mark.asyncio
async def test_preflight_machine_mismatch(fleet_with_fake, tmp_path):
    """User passes machine=X5000 but only a Pegasos2 ISO is present."""
    fleet, fake = fleet_with_fake
    (tmp_path / "Pegasos2InstallCD-53.34.iso").write_bytes(b"")
    fake.add_path("BootTest:")

    res = await installer_tool.installer_preflight(
        fleet, "x5000",
        dest_volume="BootTest:",
        sources_dir=str(tmp_path),
        machine="X5000",
    )
    assert res.overall == "fail"
    fail_names = {c.name for c in res.checks if c.status == "fail"}
    assert "iso_resolved" in fail_names


@pytest.mark.asyncio
async def test_preflight_accepts_iso_lha_only(fleet_with_fake, tmp_path):
    """Sources dir has only the LHA-of-ISO form (Hyperion-shipped).
    Preflight should resolve the iso correctly + report the LHA
    source in the detail."""
    fleet, fake = fleet_with_fake
    (tmp_path / "AmigaOneX5000InstallCD-53.25.iso.lha").write_bytes(b"")
    (tmp_path / "AmigaOS4.1FinalEditionUpdate2-53.14.lha").write_bytes(b"")
    (tmp_path / "AmigaOS4.1FinalEditionUpdate3-53.34.lha").write_bytes(b"")
    (tmp_path / "enhancer_software_2.2.lha").write_bytes(b"")
    (tmp_path / "NGFCheck.lha").write_bytes(b"")
    (tmp_path / "NGFS.lha").write_bytes(b"")
    (tmp_path / "MCPd").write_bytes(b"mcpd")
    (tmp_path / "SerialShell").write_bytes(b"ss")
    bs = tmp_path / "diskimage-bootstrap"
    bs.mkdir(exist_ok=True)
    (bs / "MountDiskImage").write_bytes(b"mdi")
    (bs / "diskimage.device").write_bytes(b"dim")
    (bs / "CDFileSystem").write_bytes(b"cdfs")
    fake.add_path("BootTest:")

    res = await installer_tool.installer_preflight(
        fleet, "x5000",
        dest_volume="BootTest:",
        sources_dir=str(tmp_path),
    )
    assert res.overall == "ok"
    assert res.machine == "X5000"
    assert res.iso == "AmigaOneX5000InstallCD-53.25.iso"
    iso_check = next(c for c in res.checks if c.name == "iso_resolved")
    assert "extract from" in iso_check.detail or ".lha" in iso_check.detail


@pytest.mark.asyncio
async def test_preflight_a1222_warns_about_rescuev2(fleet_with_fake, tmp_path):
    fleet, fake = fleet_with_fake
    (tmp_path / "AmigaOneA1222InstallCD-53.37.iso").write_bytes(b"")
    (tmp_path / "AmigaOS4.1FinalEditionUpdate3-53.34.lha").write_bytes(b"")
    fake.add_path("BootTest:")

    res = await installer_tool.installer_preflight(
        fleet, "x5000",  # config target name, irrelevant here
        dest_volume="BootTest:",
        sources_dir=str(tmp_path),
    )
    assert res.machine == "A1222"
    warns = {c.name for c in res.checks if c.status == "warn"}
    assert "rescuev2_volume" in warns
