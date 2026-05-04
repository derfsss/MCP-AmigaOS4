"""Installer step-primitive wiring tests via fake MCPd transport.

Drives each primitive against a stub that records the (method, params)
calls and returns canned responses. Doesn't touch a real Amiga.
"""

from __future__ import annotations

import base64
from typing import Any

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
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict | None]] = []
        self.responses: dict[str, Any] = {}
        self.errors: dict[str, TargetError] = {}
        self.existing_paths: set[str] = set()

    def add_path(self, p: str) -> None:
        self.existing_paths.add(p)

    async def request(self, method: str, params: dict | None = None,
                      timeout_s: float = 30.0) -> Any:
        self.calls.append((method, params))
        if method in self.errors:
            raise self.errors[method]
        if method == "fs.stat":
            assert params is not None
            if params["path"] in self.existing_paths:
                return {"type": "dir", "size": 0}
            raise TargetError(
                f"path not found: {params['path']!r}",
                data={"path": params["path"]},
            )
        if method == "fs.read":
            assert params is not None
            content = self.existing_paths_content.get(params["path"], "")
            return {
                "content_b64": base64.b64encode(content.encode("ascii")
                                                ).decode("ascii"),
                "size": len(content),
            }
        if method in self.responses:
            return self.responses[method]
        return {"output": "", "exit_code": 0}

    existing_paths_content: dict[str, str] = {}  # noqa: RUF012


@pytest.fixture
def fleet_with_fake() -> tuple[Fleet, FakeMcpd]:
    cfg = Config(
        targets={
            "x5000": TargetConfig(
                type="remote",
                channels=TargetChannels(
                    mcpd=McpdChannel(endpoint="192.168.0.25:4322"),
                ),
            ),
        },
    )
    fleet = Fleet(cfg)
    fake = FakeMcpd()
    fleet._mcpd["x5000"] = fake  # type: ignore[assignment]
    return fleet, fake


# ---- mount / unmount ----------------------------------------------


@pytest.mark.asyncio
async def test_mount_iso_full_sequence(fleet_with_fake):
    fleet, fake = fleet_with_fake
    expected = "AmigaOS 4.1 Final Edition:"
    fake.add_path(expected)  # so the polling loop succeeds first try

    res = await it.installer_mount_iso(
        fleet, "x5000",
        iso_path="BootTest:tmp/AmigaOneX5000InstallCD-53.42.iso",
        timeout_s=2.0, settle_s=0.0,
    )
    assert res.mounted is True
    assert res.detected_volume == expected
    methods = [m for m, _ in fake.calls]
    # Expect MountDiskImage INSERT + Mount of DOSDriver + fs.stat poll
    assert any("INSERT=" in (p or {}).get("command", "")
               for m, p in fake.calls if m == "exec.cmd")
    assert any('Mount "DEVS:DOSDrivers/COMBI"' in (p or {}).get("command", "")
               for m, p in fake.calls if m == "exec.cmd")
    assert "fs.stat" in methods


@pytest.mark.asyncio
async def test_mount_iso_volume_never_appears(fleet_with_fake):
    fleet, _ = fleet_with_fake
    res = await it.installer_mount_iso(
        fleet, "x5000",
        iso_path="BootTest:tmp/foo.iso",
        timeout_s=0.1, settle_s=0.0,
    )
    assert res.mounted is False
    assert res.detected_volume is None


@pytest.mark.asyncio
async def test_unmount_iso_invokes_eject(fleet_with_fake):
    fleet, fake = fleet_with_fake
    fake.responses["exec.cmd"] = {
        "output": "Volume ejected", "exit_code": 0,
    }
    res = await it.installer_unmount_iso(fleet, "x5000", unit=50)
    assert "EJECT" in fake.calls[0][1]["command"]
    assert "U=50" in fake.calls[0][1]["command"]
    assert res.unit == 50


# ---- copy / lha (confirm gate) ------------------------------------


@pytest.mark.asyncio
async def test_copy_tree_requires_confirm(fleet_with_fake):
    fleet, _ = fleet_with_fake
    with pytest.raises(InvalidParams):
        await it.installer_copy_tree(
            fleet, "x5000",
            src="ISO:", dst="BootTest:",
            confirm=False,
        )


@pytest.mark.asyncio
async def test_copy_tree_emits_amigados(fleet_with_fake):
    fleet, fake = fleet_with_fake
    fake.responses["exec.cmd"] = {"output": "", "exit_code": 0}
    res = await it.installer_copy_tree(
        fleet, "x5000",
        src="ISO:", dst="BootTest:",
        confirm=True,
    )
    cmd = fake.calls[0][1]["command"]
    assert cmd.startswith('Copy "ISO:" "BootTest:"')
    assert "ALL" in cmd
    assert "CLONE" in cmd
    assert "QUIET" in cmd
    assert res.exit_code == 0


@pytest.mark.asyncio
async def test_copy_tree_flag_toggles(fleet_with_fake):
    fleet, fake = fleet_with_fake
    fake.responses["exec.cmd"] = {"output": "", "exit_code": 0}
    await it.installer_copy_tree(
        fleet, "x5000", src="A:", dst="B:",
        all_=False, clone=False, quiet=False, confirm=True,
    )
    cmd = fake.calls[0][1]["command"]
    assert "ALL" not in cmd
    assert "CLONE" not in cmd
    assert "QUIET" not in cmd


@pytest.mark.asyncio
async def test_apply_lha_requires_confirm(fleet_with_fake):
    fleet, _ = fleet_with_fake
    with pytest.raises(InvalidParams):
        await it.installer_apply_lha(
            fleet, "x5000",
            archive="BootTest:tmp/U2.lha", dest="BootTest:tmp/u2",
            confirm=False,
        )


@pytest.mark.asyncio
async def test_apply_lha_emits_amigados(fleet_with_fake):
    fleet, fake = fleet_with_fake
    fake.responses["exec.cmd"] = {"output": "extracted 100 files",
                                   "exit_code": 0}
    res = await it.installer_apply_lha(
        fleet, "x5000",
        archive="BootTest:tmp/U2.lha", dest="BootTest:tmp/u2",
        confirm=True,
    )
    cmd = fake.calls[0][1]["command"]
    assert cmd.startswith("LhA ")
    assert ' x "BootTest:tmp/U2.lha"' in cmd
    assert ' "BootTest:tmp/u2/"' in cmd
    assert ">NIL:" in cmd
    assert "extracted" in res.output


# ---- kicklayout ---------------------------------------------------


@pytest.mark.asyncio
async def test_read_kicklayout(fleet_with_fake):
    fleet, fake = fleet_with_fake
    klpath = "BootTest:Kickstart/Kicklayout"
    fake.existing_paths_content = {
        klpath: "LABEL Default\nEXEC Kickstart/kernel\n",
    }
    res = await it.installer_read_kicklayout(
        fleet, "x5000", dest_volume="BootTest:",
    )
    assert res.path == klpath
    assert "LABEL Default" in res.content
    assert res.dest_volume == "BootTest:"


@pytest.mark.asyncio
async def test_write_kicklayout_requires_confirm(fleet_with_fake):
    fleet, _ = fleet_with_fake
    with pytest.raises(InvalidParams):
        await it.installer_write_kicklayout(
            fleet, "x5000", dest_volume="BootTest:",
            content="dummy", confirm=False,
        )


@pytest.mark.asyncio
async def test_write_kicklayout_backs_up_existing(fleet_with_fake):
    fleet, fake = fleet_with_fake
    klpath = "BootTest:Kickstart/Kicklayout"
    fake.add_path(klpath)
    res = await it.installer_write_kicklayout(
        fleet, "x5000", dest_volume="BootTest:",
        content="LABEL X\nEXEC Kickstart/kernel\n",
        confirm=True,
    )
    assert res.backup_path == klpath + ".bak"
    methods = [m for m, _ in fake.calls]
    assert "fs.copy" in methods
    assert "fs.write" in methods


@pytest.mark.asyncio
async def test_write_kicklayout_no_existing_no_backup(fleet_with_fake):
    fleet, fake = fleet_with_fake
    res = await it.installer_write_kicklayout(
        fleet, "x5000", dest_volume="BootTest:",
        content="LABEL X\nEXEC Kickstart/kernel\n",
        confirm=True,
    )
    assert res.backup_path is None
    methods = [m for m, _ in fake.calls]
    assert "fs.copy" not in methods


@pytest.mark.asyncio
async def test_patch_kicklayout_noop_when_unchanged(fleet_with_fake):
    fleet, fake = fleet_with_fake
    klpath = "BootTest:Kickstart/Kicklayout"
    # Existing file already has the module to be added.
    text = ("LABEL Default\nEXEC Kickstart/kernel\n"
            "MODULE Kickstart/mounter.library\n")
    fake.existing_paths_content = {klpath: text}

    res = await it.installer_patch_kicklayout(
        fleet, "x5000", dest_volume="BootTest:",
        add_modules=[
            {"modules": ["Kickstart/mounter.library"], "label": "U1"},
        ],
        confirm=True,
    )
    assert res.changed is False
    methods = [m for m, _ in fake.calls]
    # Read should happen, but no write or copy.
    assert "fs.read" in methods
    assert "fs.write" not in methods


@pytest.mark.asyncio
async def test_patch_kicklayout_applies_and_writes(fleet_with_fake):
    fleet, fake = fleet_with_fake
    klpath = "BootTest:Kickstart/Kicklayout"
    text = ("LABEL Default\nEXEC Kickstart/kernel\n"
            "MODULE Kickstart/p5020sata.device.kmod\n")
    fake.existing_paths_content = {klpath: text}
    fake.add_path(klpath)  # so the backup gets taken

    res = await it.installer_patch_kicklayout(
        fleet, "x5000", dest_volume="BootTest:",
        add_modules=[
            {"modules": ["Kickstart/mounter.library"], "label": "U1"},
        ],
        replace_text=[
            {"old": "p5020sata.device.kmod", "new": "p50x0sata.device.kmod"},
        ],
        confirm=True,
    )
    assert res.changed is True
    assert res.replacements_done == 1
    assert "U1" in res.labels_applied
    assert res.modules_added >= 1
    methods = [m for m, _ in fake.calls]
    assert "fs.write" in methods
    assert "fs.copy" in methods


@pytest.mark.asyncio
async def test_patch_kicklayout_requires_confirm(fleet_with_fake):
    fleet, _ = fleet_with_fake
    with pytest.raises(InvalidParams):
        await it.installer_patch_kicklayout(
            fleet, "x5000", dest_volume="BootTest:",
            add_modules=[{"modules": ["Kickstart/foo"], "label": "L"}],
            confirm=False,
        )
