"""installer.stage + installer.verify tests via fake transport."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from amiga_fleet_mcp.config import (
    Config,
    McpdChannel,
    TargetChannels,
    TargetConfig,
)
from amiga_fleet_mcp.errors import InvalidParams, TargetError
from amiga_fleet_mcp.fleet import Fleet
from amiga_fleet_mcp.tools import installer as it


class FakeMcpd:
    def __init__(self):
        self.calls = []
        self.existing_paths = set()

    def add_path(self, p):
        self.existing_paths.add(p)

    async def request(self, method, params=None, timeout_s=30.0):
        self.calls.append((method, params))
        if method == "fs.stat":
            if params["path"] in self.existing_paths:
                return {"type": "dir", "size": 0}
            raise TargetError("not found", data={"path": params["path"]})
        if method == "fs.hash":
            return {"hash": "abc123" * 10 + "ab", "algo": "sha256"}
        return {"output": "", "exit_code": 0}


@pytest.fixture
def fleet_with_fake():
    cfg = Config(targets={
        "x5000": TargetConfig(
            type="remote",
            channels=TargetChannels(
                mcpd=McpdChannel(endpoint="192.168.0.25:4322"),
            ),
        ),
    })
    fleet = Fleet(cfg)
    fake = FakeMcpd()
    fleet._mcpd["x5000"] = fake  # type: ignore[assignment]
    return fleet, fake


# ---- stage --------------------------------------------------------


def _make_x5000_sources(d: Path, *, with_bootstrap: bool = True,
                        with_mcpd: bool = True):
    """Lay out a complete X5000 install bundle in a temp dir."""
    iso_payload = b"x" * 1024
    (d / "AmigaOneX5000InstallCD-53.42.iso").write_bytes(iso_payload)
    (d / "AmigaOS4.1FinalEditionUpdate2-53.14.lha").write_bytes(b"u2")
    (d / "AmigaOS4.1FinalEditionUpdate3-53.34.lha").write_bytes(b"u3")
    (d / "enhancer_software_2.2.lha").write_bytes(b"enh")
    (d / "NGFCheck.lha").write_bytes(b"ngf-check")
    (d / "NGFS.lha").write_bytes(b"ngfs")
    if with_mcpd:
        (d / "MCPd").write_bytes(b"\x7fELF" + b"mcpd-test-bin")
    (d / "SerialShell").write_bytes(b"\x7fELF" + b"ss-test-bin")
    (d / "AmiDock.amiga.com.xml").write_bytes(b"<dock/>")
    if with_bootstrap:
        bs = d / "bootstrap"
        bs.mkdir()
        (bs / "MountDiskImage").write_bytes(b"mdi")
        (bs / "diskimage.device").write_bytes(b"dim")
        (bs / "CDFileSystem").write_bytes(b"cdfs")
        return bs
    return None


@pytest.mark.asyncio
async def test_stage_requires_confirm(fleet_with_fake, tmp_path):
    fleet, _ = fleet_with_fake
    _make_x5000_sources(tmp_path)
    with pytest.raises(InvalidParams):
        await it.installer_stage(
            fleet, "x5000",
            dest_volume="BootTest:",
            sources_dir=str(tmp_path),
            machine="X5000",
            confirm=False,
        )


@pytest.mark.asyncio
async def test_stage_uploads_full_x5000_bundle(fleet_with_fake, tmp_path):
    fleet, _fake = fleet_with_fake
    bootstrap = _make_x5000_sources(tmp_path)

    # Patch chunked_upload to a stub that records calls.
    uploaded: list[tuple[str, str]] = []

    class FakeStats:
        def __init__(self, size):
            self.bytes_total = size
            self.bytes_sent_compressed = size // 2
            self.elapsed_s = 0.1
            self.compression_ratio = 0.5

    async def fake_upload(fleet_, target, local, remote, **_):
        uploaded.append((str(local), remote))
        size = Path(local).stat().st_size
        return FakeStats(size)

    with mock.patch.object(it, "chunked_upload", fake_upload):
        res = await it.installer_stage(
            fleet, "x5000",
            dest_volume="BootTest:",
            sources_dir=str(tmp_path),
            machine="X5000",
            confirm=True,
        )

    # ISO + 2 updates + enhancer + 2 extras + MCPd + SerialShell +
    # AmiDock XML = 9 (no bootstrap upload -- diskimage tools come
    # from the running AmigaOS / install ISO).
    assert len(uploaded) == 9
    assert res.machine == "X5000"
    assert res.iso_filename == "AmigaOneX5000InstallCD-53.42.iso"
    assert res.skipped == []

    # Spot check: ISO landed under tmp/, no bootstrap files staged.
    iso_dst = next(dst for src, dst in uploaded if src.endswith(".iso"))
    assert iso_dst == "BootTest:tmp/AmigaOneX5000InstallCD-53.42.iso"
    bootstrap_dsts = [dst for src, dst in uploaded
                      if "diskimage-bootstrap" in dst]
    assert bootstrap_dsts == []
    # Make sure pyflakes doesn't complain about the unused fixture
    # output (kept so future tests can assert on it).
    assert bootstrap is not None


@pytest.mark.asyncio
async def test_stage_records_skipped_files(fleet_with_fake, tmp_path):
    fleet, _fake = fleet_with_fake
    # Only an ISO present; updates + enhancer missing
    (tmp_path / "AmigaOneX5000InstallCD-53.42.iso").write_bytes(b"i")
    # mandatory binaries (so the staging is allowed to start)
    (tmp_path / "MCPd").write_bytes(b"mcpd")
    (tmp_path / "SerialShell").write_bytes(b"ss")
    bs = tmp_path / "bootstrap"
    bs.mkdir()
    (bs / "MountDiskImage").write_bytes(b"mdi")
    (bs / "diskimage.device").write_bytes(b"dim")
    (bs / "CDFileSystem").write_bytes(b"cdfs")

    class FakeStats:
        def __init__(self):
            self.bytes_total = 1
            self.bytes_sent_compressed = 1
            self.elapsed_s = 0
            self.compression_ratio = 1.0

    async def fake_upload(*a, **kw):
        return FakeStats()

    with mock.patch.object(it, "chunked_upload", fake_upload):
        res = await it.installer_stage(
            fleet, "x5000",
            dest_volume="BootTest:",
            sources_dir=str(tmp_path),
            machine="X5000",
            confirm=True,
        )
    assert len(res.skipped) > 0
    skip_msgs = "\n".join(res.skipped)
    # Optional payloads that aren't fatal-missing; they get skipped.
    assert "AmigaOS4.1FinalEditionUpdate2" in skip_msgs


@pytest.mark.asyncio
async def test_stage_unknown_machine_rejected(fleet_with_fake, tmp_path):
    fleet, _ = fleet_with_fake
    with pytest.raises(InvalidParams):
        await it.installer_stage(
            fleet, "x5000",
            dest_volume="BootTest:",
            sources_dir=str(tmp_path),
            machine="DragonBall",
            confirm=True,
        )


# ---- verify -------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_all_present(fleet_with_fake):
    fleet, fake = fleet_with_fake
    # Add all the X5000 manifest paths to the fake "filesystem"
    paths = [
        "BootTest:S/Startup-Sequence", "BootTest:S/User-Startup",
        "BootTest:Devs/Monitors", "BootTest:C/", "BootTest:L/",
        "BootTest:Libs/", "BootTest:Kickstart/",
        "BootTest:Kickstart/Kicklayout",
        "BootTest:Kickstart/RadeonRX.chip",
        "BootTest:Kickstart/RadeonHD.chip",
        "BootTest:Kickstart/SmartFilesystem",
        "BootTest:Kickstart/diskcache.library.kmod",
        "BootTest:Devs/Networks/p50x0_eth.device",
        "BootTest:Kickstart/NGFileSystem",
        "BootTest:C/NGFCheck",
    ]
    for p in paths:
        fake.add_path(p)

    res = await it.installer_verify(
        fleet, "x5000",
        dest_volume="BootTest:",
        machine="X5000",
    )
    assert res.overall == "ok"
    assert res.missing == 0
    assert res.present == res.total
    assert res.sha256_mismatch == 0


@pytest.mark.asyncio
async def test_verify_reports_missing_files(fleet_with_fake):
    fleet, fake = fleet_with_fake
    # Add only Kicklayout; everything else should report missing
    fake.add_path("BootTest:Kickstart/Kicklayout")

    res = await it.installer_verify(
        fleet, "x5000",
        dest_volume="BootTest:",
        machine="X5000",
    )
    assert res.overall == "fail"
    assert res.missing >= 5  # most of the X5000 manifest is missing
    assert res.present == 1


@pytest.mark.asyncio
async def test_verify_extra_paths(fleet_with_fake):
    fleet, fake = fleet_with_fake
    fake.add_path("BootTest:Kickstart/Kicklayout")
    fake.add_path("BootTest:Custom/MyTool")

    res = await it.installer_verify(
        fleet, "x5000",
        dest_volume="BootTest:",
        machine="X5000",
        extra_paths=["Custom/MyTool", "Custom/NotPresent"],
    )
    custom_entries = [e for e in res.entries
                      if e.path.startswith("Custom/")]
    assert len(custom_entries) == 2
    paths_present = {e.path: e.present for e in custom_entries}
    assert paths_present["Custom/MyTool"] is True
    assert paths_present["Custom/NotPresent"] is False


@pytest.mark.asyncio
async def test_verify_machines_with_no_manifest_rejected(fleet_with_fake):
    fleet, _ = fleet_with_fake
    # All five supported machines now have verify manifests.
    # A truly-unsupported machine name should still raise.
    with pytest.raises(InvalidParams):
        await it.installer_verify(
            fleet, "x5000",
            dest_volume="BootTest:",
            machine="DragonBall",
        )
