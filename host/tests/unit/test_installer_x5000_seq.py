"""X5000 sequence shape tests + dry-run via the run tool."""

from __future__ import annotations

import pytest

from amiga_fleet_mcp.config import (
    Config,
    McpdChannel,
    TargetChannels,
    TargetConfig,
)
from amiga_fleet_mcp.errors import InvalidParams, TargetError
from amiga_fleet_mcp.fleet import Fleet
from amiga_fleet_mcp.installer.sequences import x5000 as x5000_seq
from amiga_fleet_mcp.tools import installer as it


def test_x5000_sequence_step_order():
    """Pin the X5000 sequence shape — these names + ordering define
    the boot-critical path. Reorder = update test deliberately.

    install_bootloader is structurally present for all machines but
    runtime-skipped for X5000 (per MACHINES_NO_BOOTLOADER_FILE).
    """
    seq = x5000_seq.build(iso_filename="AmigaOneX5000InstallCD-53.42.iso")
    names = [s.name for s in seq.steps]
    assert names == [
        "stage_diskimage_tools",
        "extract_iso_lha",
        "mount_iso",
        "verify_cd_signature",
        "copy_base_os",
        "copy_installation_extras",
        "install_bootloader",
        "apply_update_2",
        "apply_update_3",
        "install_enhancer",
        "install_ngf",
        "install_extras",
        "patch_amidock_prefs",
        "install_network_config",
        "install_serialshell",
        "quarantine_emu10kx",
        "quarantine_devs_monitors",
        "install_mcpd",
        "unmount_iso",
        "cleanup_tmp",
    ]


def test_x5000_sequence_metadata():
    seq = x5000_seq.build(iso_filename="x.iso")
    assert seq.machine == "X5000"
    assert "X5000" in seq.description
    # Every step has a non-empty doc
    for s in seq.steps:
        assert s.doc and len(s.doc) > 5


# ---- run tool wiring ----------------------------------------------


class FakeMcpd:
    def __init__(self):
        self.calls = []
        self.existing_paths = set()
        self.existing_paths_content = {}

    async def request(self, method, params=None, timeout_s=30.0):
        self.calls.append((method, params))
        if method == "fs.stat":
            if params["path"] in self.existing_paths:
                return {"type": "dir", "size": 0}
            raise TargetError("not found", data={"path": params["path"]})
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


@pytest.mark.asyncio
async def test_install_x5000_dry_run_returns_planned(fleet_with_fake):
    fleet, fake = fleet_with_fake
    res = await it.installer_install_x5000(
        fleet, "x5000",
        dest_volume="BootTest:",
        iso_filename="AmigaOneX5000InstallCD-53.42.iso",
        dry_run=True,
    )
    assert res.dry_run is True
    assert res.overall == "planned"
    assert res.machine == "X5000"
    # Dry-run touches nothing on the Amiga.
    assert fake.calls == []
    assert all(s.status == "planned" for s in res.steps)


@pytest.mark.asyncio
async def test_install_x5000_real_run_requires_confirm(fleet_with_fake):
    fleet, _ = fleet_with_fake
    with pytest.raises(InvalidParams):
        await it.installer_install_x5000(
            fleet, "x5000",
            dest_volume="BootTest:",
            iso_filename="AmigaOneX5000InstallCD-53.42.iso",
            dry_run=False,
            confirm=False,
        )


@pytest.mark.asyncio
async def test_install_x5000_auto_detects_iso(fleet_with_fake, tmp_path):
    """When iso_filename is None and sources_dir is given, the run
    tool should auto-detect the X5000 ISO from the host directory."""
    fleet, _ = fleet_with_fake
    (tmp_path / "AmigaOneX5000InstallCD-53.42.iso").write_bytes(b"")
    res = await it.installer_install_x5000(
        fleet, "x5000",
        dest_volume="BootTest:",
        sources_dir=str(tmp_path),
        dry_run=True,
    )
    # The sequence built includes the mount_iso step that uses the
    # detected name; no easy way to introspect that without running,
    # so we just confirm the sequence built without error.
    assert res.overall == "planned"


@pytest.mark.asyncio
async def test_run_dispatcher_unknown_machine_raises(fleet_with_fake):
    fleet, _ = fleet_with_fake
    with pytest.raises(InvalidParams):
        await it.installer_run(
            fleet, "x5000",
            dest_volume="BootTest:",
            machine="DragonBall",
            dry_run=True,
        )


@pytest.mark.asyncio
async def test_extract_iso_lha_step_no_lha_no_op(fleet_with_fake):
    """When neither <iso> nor <iso>.lha is on the target, the step
    is a no-op + reports a clean reason. mount_iso will then fail
    cleanly and surface the real problem."""
    from amiga_fleet_mcp.installer.sequences import _steps
    fleet, _fake = fleet_with_fake
    step = _steps.extract_iso_lha()
    ctx = {
        "fleet": fleet, "target": "x5000",
        "mcpd": fleet.mcpd("x5000"),
        "dest_volume": "BootTest:",
        "iso_filename": "AmigaOneX5000InstallCD-53.42.iso",
    }
    res = await step.fn(ctx)
    assert res["action"] == "skipped"
    assert "neither" in res["reason"]


@pytest.mark.asyncio
async def test_extract_iso_lha_step_iso_present_no_op(fleet_with_fake):
    """If the bare ISO is already on target, skip extraction."""
    from amiga_fleet_mcp.installer.sequences import _steps
    fleet, fake = fleet_with_fake
    iso_filename = "AmigaOneX5000InstallCD-53.42.iso"
    fake.existing_paths.add(f"BootTest:tmp/{iso_filename}")
    step = _steps.extract_iso_lha()
    ctx = {
        "fleet": fleet, "target": "x5000",
        "mcpd": fleet.mcpd("x5000"),
        "dest_volume": "BootTest:",
        "iso_filename": iso_filename,
    }
    res = await step.fn(ctx)
    assert res["action"] == "skipped"
    assert "already present" in res["reason"]


@pytest.mark.asyncio
async def test_extract_iso_lha_step_extracts_when_only_lha_present(fleet_with_fake):
    """Bare ISO missing, .lha present -> step runs LhA x via apply_lha
    and confirms the .iso is recovered."""
    from amiga_fleet_mcp.installer.sequences import _steps
    fleet, fake = fleet_with_fake
    iso_filename = "AmigaOneX5000InstallCD-53.42.iso"
    # Initially: only the .lha is on target.
    fake.existing_paths.add(f"BootTest:tmp/{iso_filename}.lha")

    # Track when the .iso "appears" -- after apply_lha runs (via
    # exec.cmd "LhA x ..."), simulate the .iso being unpacked by
    # adding it to existing_paths.
    original_request = fake.request
    extraction_done = []

    async def _request(method, params=None, timeout_s=30.0):
        if (method == "exec.cmd" and params is not None
                and "LhA" in params.get("command", "")):
            fake.existing_paths.add(f"BootTest:tmp/{iso_filename}")
            extraction_done.append(params["command"])
            return {"output": "", "exit_code": 0}
        return await original_request(method, params, timeout_s)

    fake.request = _request

    step = _steps.extract_iso_lha()
    ctx = {
        "fleet": fleet, "target": "x5000",
        "mcpd": fleet.mcpd("x5000"),
        "dest_volume": "BootTest:",
        "iso_filename": iso_filename,
    }
    res = await step.fn(ctx)
    assert res["action"] == "extracted"
    assert res["from"].endswith(".iso.lha")
    assert res["to"].endswith(".iso")
    assert len(extraction_done) == 1
    assert "LhA" in extraction_done[0]


@pytest.mark.asyncio
async def test_install_mcpd_copies_binary_and_edits_network_startup(fleet_with_fake):
    """install_mcpd copies the binary, sets +rwed, and appends a
    launch line to S:Network-Startup."""
    import base64

    from amiga_fleet_mcp.installer.sequences import _steps
    fleet, fake = fleet_with_fake
    fake.existing_paths.add("BootTest:tmp/MCPd")
    # Network-Startup exists with prior content
    netstart_path = "BootTest:S/Network-Startup"
    netstart_initial = "; existing network startup\nMount NETSTACK:\n"
    fake.add_path = lambda p: fake.existing_paths.add(p)
    # Track ops
    ops: list[tuple[str, dict]] = []
    original_request = fake.request
    written: dict[str, str] = {}

    async def _request(method, params=None, timeout_s=30.0):
        ops.append((method, dict(params or {})))
        if method == "fs.read" and (params or {}).get("path") == netstart_path:
            return {
                "content_b64": base64.b64encode(
                    netstart_initial.encode("ascii")
                ).decode("ascii"),
                "size": len(netstart_initial),
            }
        if method == "fs.write":
            p = (params or {}).get("path", "")
            data = base64.b64decode(
                (params or {}).get("content_b64", "")
            ).decode("ascii", errors="replace")
            written[p] = data
            return {"path": p, "size": len(data)}
        if method == "fs.copy":
            return {"ok": True}
        return await original_request(method, params, timeout_s)
    fake.request = _request

    step = _steps.install_mcpd()
    ctx = {
        "fleet": fleet, "target": "x5000",
        "mcpd": fleet.mcpd("x5000"),
        "dest_volume": "BootTest:",
    }
    res = await step.fn(ctx)
    assert res["action"] == "installed"
    assert res["binary"] == "BootTest:System/MCPd/MCPd"
    assert res["network_startup"] == netstart_path

    # The new Network-Startup must contain the MCPd launch line + keep
    # original content above it.
    new_text = written[netstart_path]
    assert "Mount NETSTACK:" in new_text  # original preserved
    assert "Run >NIL: <NIL: SYS:System/MCPd/MCPd" in new_text
    assert "; MCPd" in new_text

    # Should have run Protect +rwed via exec.cmd
    exec_cmds = [p["command"] for m, p in ops if m == "exec.cmd"]
    assert any('Protect "BootTest:System/MCPd/MCPd" +rwed' in c
               for c in exec_cmds)


@pytest.mark.asyncio
async def test_install_mcpd_idempotent_when_marker_present(fleet_with_fake):
    """If S:Network-Startup already references MCPd, don't append a
    second time. Binary copy still runs (idempotent overwrite)."""
    import base64

    from amiga_fleet_mcp.installer.sequences import _steps
    fleet, fake = fleet_with_fake
    fake.existing_paths.add("BootTest:tmp/MCPd")
    netstart_path = "BootTest:S/Network-Startup"
    netstart_initial = (
        "; pre-existing\n"
        "Run >NIL: <NIL: SYS:System/MCPd/MCPd\n"
    )
    written: dict[str, str] = {}
    original_request = fake.request

    async def _request(method, params=None, timeout_s=30.0):
        if method == "fs.read" and (params or {}).get("path") == netstart_path:
            return {
                "content_b64": base64.b64encode(
                    netstart_initial.encode("ascii")
                ).decode("ascii"),
                "size": len(netstart_initial),
            }
        if method == "fs.write":
            written[(params or {}).get("path", "")] = "??"
            return {"ok": True}
        return await original_request(method, params, timeout_s)
    fake.request = _request

    step = _steps.install_mcpd()
    ctx = {
        "fleet": fleet, "target": "x5000",
        "mcpd": fleet.mcpd("x5000"),
        "dest_volume": "BootTest:",
    }
    res = await step.fn(ctx)
    assert res["marker_already_present"] is True
    assert "Network-Startup unchanged" in res["action"]
    assert netstart_path not in written  # we did NOT rewrite it


@pytest.mark.asyncio
async def test_install_mcpd_skipped_when_binary_not_staged(fleet_with_fake):
    """If <dest>:tmp/MCPd is missing (caller forgot to stage it),
    install_mcpd reports skipped. Doesn't fail the sequence."""
    from amiga_fleet_mcp.installer.sequences import _steps
    fleet, _ = fleet_with_fake
    step = _steps.install_mcpd()
    ctx = {
        "fleet": fleet, "target": "x5000",
        "mcpd": fleet.mcpd("x5000"),
        "dest_volume": "BootTest:",
    }
    res = await step.fn(ctx)
    assert res["action"] == "skipped"
    assert "not staged" in res["reason"]


@pytest.mark.asyncio
async def test_quarantine_emu10kx_moves_when_present(fleet_with_fake):
    """quarantine_emu10kx renames DEVS:AHI/emu10kx.audio -> Storage/AHI/.
    Idempotent: skipped when source isn't present."""
    from amiga_fleet_mcp.installer.sequences import _steps
    fleet, fake = fleet_with_fake
    src = "BootTest:DEVS/AHI/emu10kx.audio"
    dst = "BootTest:Storage/AHI/emu10kx.audio"
    fake.existing_paths.add(src)

    renames: list[dict] = []
    original_request = fake.request

    async def _request(method, params=None, timeout_s=30.0):
        if method == "fs.rename":
            renames.append(dict(params or {}))
            return {"ok": True}
        return await original_request(method, params, timeout_s)
    fake.request = _request

    step = _steps.quarantine_emu10kx()
    ctx = {
        "fleet": fleet, "target": "x5000",
        "mcpd": fleet.mcpd("x5000"),
        "dest_volume": "BootTest:",
    }
    res = await step.fn(ctx)
    assert res["action"].startswith("moved")
    assert renames == [{"src": src, "dst": dst}]


@pytest.mark.asyncio
async def test_quarantine_devs_monitors_moves_each_entry(fleet_with_fake):
    """quarantine_devs_monitors lists DEVS:Monitors/, moves each
    entry to Storage/Monitors/ via fs.rename."""
    from amiga_fleet_mcp.installer.sequences import _steps
    fleet, fake = fleet_with_fake

    listed = [{"name": "VooDoo", "type": "file"},
              {"name": "VooDoo.info", "type": "file"},
              {"name": "RTG", "type": "file"},
              {"name": "RTG.info", "type": "file"}]
    renames: list[dict] = []
    original_request = fake.request

    async def _request(method, params=None, timeout_s=30.0):
        if method == "fs.list" and (params or {}).get("path") == \
                "BootTest:DEVS/Monitors":
            return {"entries": listed}
        if method == "fs.rename":
            renames.append(dict(params or {}))
            return {"ok": True}
        return await original_request(method, params, timeout_s)
    fake.request = _request

    step = _steps.quarantine_devs_monitors()
    ctx = {
        "fleet": fleet, "target": "x5000",
        "mcpd": fleet.mcpd("x5000"),
        "dest_volume": "BootTest:",
    }
    res = await step.fn(ctx)
    assert res["action"] == "moved"
    assert res["errors"] == []
    assert sorted(res["moved"]) == ["RTG", "RTG.info",
                                    "VooDoo", "VooDoo.info"]
    # Each rename targeted DEVS/Monitors/<name> -> Storage/Monitors/<name>
    assert len(renames) == 4
    for r in renames:
        assert r["src"].startswith("BootTest:DEVS/Monitors/")
        assert r["dst"].startswith("BootTest:Storage/Monitors/")


@pytest.mark.asyncio
async def test_quarantine_devs_monitors_skipped_when_empty(fleet_with_fake):
    from amiga_fleet_mcp.installer.sequences import _steps
    fleet, fake = fleet_with_fake
    original_request = fake.request

    async def _request(method, params=None, timeout_s=30.0):
        if method == "fs.list":
            return {"entries": []}
        return await original_request(method, params, timeout_s)
    fake.request = _request

    step = _steps.quarantine_devs_monitors()
    ctx = {
        "fleet": fleet, "target": "x5000",
        "mcpd": fleet.mcpd("x5000"),
        "dest_volume": "BootTest:",
    }
    res = await step.fn(ctx)
    assert res["action"] == "skipped"
    assert "already empty" in res["reason"]


@pytest.mark.asyncio
async def test_quarantine_emu10kx_skipped_when_absent(fleet_with_fake):
    from amiga_fleet_mcp.installer.sequences import _steps
    fleet, _ = fleet_with_fake
    step = _steps.quarantine_emu10kx()
    ctx = {
        "fleet": fleet, "target": "x5000",
        "mcpd": fleet.mcpd("x5000"),
        "dest_volume": "BootTest:",
    }
    res = await step.fn(ctx)
    assert res["action"] == "skipped"
    assert "not present" in res["reason"]


@pytest.mark.asyncio
async def test_cleanup_tmp_deletes_scratch(fleet_with_fake):
    """cleanup_tmp deletes <dest>:tmp/scratch + the extracted iso when
    extract_iso_lha set the flag. Skips deleting the iso when it was
    uploaded directly."""
    from amiga_fleet_mcp.installer.sequences import _steps
    fleet, fake = fleet_with_fake

    # Track fs.delete calls
    deletes: list[dict] = []
    original_request = fake.request

    async def _request(method, params=None, timeout_s=30.0):
        if method == "fs.delete":
            deletes.append(dict(params or {}))
            # Pretend the delete works
            return {"ok": True}
        return await original_request(method, params, timeout_s)

    # Add scratch path so fs.stat finds it
    fake.existing_paths.add("BootTest:tmp/scratch")
    fake.request = _request

    step = _steps.cleanup_tmp()
    ctx = {
        "fleet": fleet, "target": "x5000",
        "mcpd": fleet.mcpd("x5000"),
        "dest_volume": "BootTest:",
        "iso_filename": "AmigaOneX5000InstallCD-53.42.iso",
        "iso_was_extracted_from_lha": True,  # signal from extract step
    }
    res = await step.fn(ctx)
    assert "BootTest:tmp/scratch" in res["deleted"]
    iso_path = "BootTest:tmp/AmigaOneX5000InstallCD-53.42.iso"
    assert iso_path in res["deleted"]
    delete_paths = [d["path"] for d in deletes]
    assert "BootTest:tmp/scratch" in delete_paths
    assert iso_path in delete_paths


@pytest.mark.asyncio
async def test_cleanup_tmp_keeps_iso_when_uploaded_directly(fleet_with_fake):
    """When the install came from a directly-uploaded bare ISO (not
    extracted from a .lha), cleanup_tmp keeps it."""
    from amiga_fleet_mcp.installer.sequences import _steps
    fleet, fake = fleet_with_fake

    deletes: list[dict] = []
    original_request = fake.request

    async def _request(method, params=None, timeout_s=30.0):
        if method == "fs.delete":
            deletes.append(dict(params or {}))
            return {"ok": True}
        return await original_request(method, params, timeout_s)
    fake.request = _request

    step = _steps.cleanup_tmp()
    ctx = {
        "fleet": fleet, "target": "x5000",
        "mcpd": fleet.mcpd("x5000"),
        "dest_volume": "BootTest:",
        "iso_filename": "AmigaOneX5000InstallCD-53.42.iso",
        # iso_was_extracted_from_lha NOT set
    }
    res = await step.fn(ctx)
    delete_paths = [d["path"] for d in deletes]
    iso_path = "BootTest:tmp/AmigaOneX5000InstallCD-53.42.iso"
    assert iso_path not in delete_paths
    assert any("kept" in s for s in res["skipped"])


@pytest.mark.asyncio
async def test_run_dispatcher_unknown_machine_alias(fleet_with_fake):
    fleet, _ = fleet_with_fake
    # All five supported machines now have implemented sequences.
    # Truly unknown machines should still raise InvalidParams.
    with pytest.raises(InvalidParams):
        await it.installer_run(
            fleet, "x5000",
            dest_volume="BootTest:",
            machine="DragonBall",
            iso_filename="x.iso",
            dry_run=True,
        )
