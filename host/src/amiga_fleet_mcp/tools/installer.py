"""installer.* tools.

Foundation tools:
    list_machines    — what we support
    required_files   — what we need on disk for a given machine
    scan_sources     — what's in a host directory
    preflight        — composed safety check before any install

Step primitives (each idempotent or reversible in isolation):
    mount_iso         — MountDiskImage U=N INSERT="path" + Mount + poll
    unmount_iso       — MountDiskImage U=N EJECT
    copy_tree         — Copy ALL CLONE QUIET
    apply_lha         — LhA x archive dest/
    read_kicklayout   — fs.read of <vol>Kickstart/Kicklayout
    write_kicklayout  — fs.write to <vol>Kickstart/Kicklayout (gated)
    patch_kicklayout  — read + apply text patches + write (gated)

All mutating tools require `confirm=True` (gates against accidents,
same pattern as sys.cold_reboot).
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

from pydantic import BaseModel

from ..errors import InvalidParams
from ..fleet import Fleet
from ..installer import (
    A1222_BACKUP_SIGNATURE,
    A1222_BACKUP_VOLS,
    CD_VERSION_SIGNATURES,
    FORBIDDEN_DEST_ENTRIES,
    ISO_PREFIX_TO_MACHINE,
    SUPPORTED_MACHINES,
    UPDATES_TO_APPLY,
    detect_machine_from_iso,
    required_files,
    resolve_machine_alias,
    scan_sources,
)
from ..installer import (
    kicklayout as kicklayout_engine,
)


class MachineInfo(BaseModel):
    machine_id: str
    friendly_name: str
    iso_prefix: str
    aliases: list[str]
    updates_applied: list[int]
    needs_enhancer_lha: bool
    needs_rescuev2_usb: bool
    cd_version_signature: str | None
    extras: list[str]


class ListMachinesResult(BaseModel):
    machines: list[MachineInfo]


class RequiredFile(BaseModel):
    role: str
    filename: str
    iso_prefix: str | None = None
    update_index: int | None = None


class RequiredFilesResult(BaseModel):
    machine: str
    files: list[RequiredFile]
    needs_rescuev2_usb: bool
    rescuev2_volumes: list[str]
    rescuev2_signature: str | None


class ScanSourcesResult(BaseModel):
    sources_dir: str
    exists: bool
    iso_files: dict[str, list[str]]
    unsupported_isos: list[str]
    lha_files: list[str]
    detected_machine: str | None
    ambiguous_machines: list[str]


class PreflightCheck(BaseModel):
    name: str
    status: str  # "ok", "warn", "fail"
    detail: str


class PreflightResult(BaseModel):
    target: str
    dest_volume: str
    machine: str | None
    iso: str | None
    sources_dir: str
    overall: str  # "ok", "warn", "fail"
    checks: list[PreflightCheck]
    missing_files: list[str]
    forbidden_entries_present: list[str]
    summary: str


# ---- Tool implementations ----------------------------------------


def _machine_info(machine_id: str) -> MachineInfo:
    """Build a MachineInfo record by mining the static tables."""
    iso_prefix = next(
        p for p, info in ISO_PREFIX_TO_MACHINE if info[0] == machine_id
    )
    friendly = next(
        info[1] for _, info in ISO_PREFIX_TO_MACHINE if info[0] == machine_id
    )
    from ..installer import MACHINE_ALIASES
    aliases = sorted(k for k, v in MACHINE_ALIASES.items() if v == machine_id)
    files = required_files(machine_id)
    extras = [f["filename"] for f in files if f["role"] == "extra"]
    return MachineInfo(
        machine_id=machine_id,
        friendly_name=friendly,
        iso_prefix=iso_prefix,
        aliases=aliases,
        updates_applied=UPDATES_TO_APPLY.get(machine_id, []),
        needs_enhancer_lha=(machine_id != "A1222"),
        needs_rescuev2_usb=(machine_id == "A1222"),
        cd_version_signature=CD_VERSION_SIGNATURES.get(machine_id),
        extras=extras,
    )


async def installer_list_machines() -> ListMachinesResult:
    """Return the list of supported AmigaOS 4.1 FE target machines."""
    return ListMachinesResult(
        machines=[_machine_info(m) for m in SUPPORTED_MACHINES],
    )


async def installer_required_files(machine: str) -> RequiredFilesResult:
    """Return the file manifest required for an install of `machine`.

    `machine` may be a canonical id ("X5000") or an alias
    ("amigaone x5000", "x5000", etc).
    """
    canonical = resolve_machine_alias(machine)
    if canonical is None:
        raise ValueError(
            f"unknown machine {machine!r}; "
            f"supported: {', '.join(SUPPORTED_MACHINES)}"
        )
    files = required_files(canonical)
    return RequiredFilesResult(
        machine=canonical,
        files=[
            RequiredFile(
                role=f["role"],
                filename=f["filename"],
                iso_prefix=f.get("iso_prefix"),
                update_index=(int(f["update_index"])
                              if "update_index" in f else None),
            )
            for f in files
        ],
        needs_rescuev2_usb=(canonical == "A1222"),
        rescuev2_volumes=list(A1222_BACKUP_VOLS) if canonical == "A1222" else [],
        rescuev2_signature=A1222_BACKUP_SIGNATURE if canonical == "A1222" else None,
    )


async def installer_scan_sources(sources_dir: str) -> ScanSourcesResult:
    """Walk a host-side directory and report what install files are
    present. Pure host-side I/O; doesn't touch the Amiga."""
    return ScanSourcesResult.model_validate(scan_sources(sources_dir))


def _normalize_dest(dest: str) -> str:
    """Ensure the destination volume name has a trailing colon."""
    d = dest.strip()
    if not d.endswith(":") and ":" not in d:
        d = d + ":"
    return d


def _is_system_volume(dest: str) -> bool:
    """Refuse to install over the running system. Checked against the
    canonical AOS4 system volume names."""
    return _normalize_dest(dest).upper() in (
        "SYS:", "AMIGAOS:", "WORKBENCH:", "SYSTEM:",
    )


async def installer_preflight(
    fleet: Fleet, target: str, *,
    dest_volume: str,
    sources_dir: str,
    machine: str | None = None,
    iso: str | None = None,
) -> PreflightResult:
    """Compose a full pre-install safety check.

    Validates:
      - sources_dir exists and contains a supported ISO + required LHAs
      - if `machine` was supplied, it matches the detected ISO machine
      - dest_volume is mounted on `target` and is NOT a system volume
      - dest_volume contains none of FORBIDDEN_DEST_ENTRIES (refuses
        to clobber a previous install)

    No Amiga-side mutation. Returns an aggregate verdict struct.
    """
    checks: list[PreflightCheck] = []
    missing_files: list[str] = []
    forbidden_present: list[str] = []
    overall = "ok"

    def add(name: str, status: str, detail: str) -> None:
        nonlocal overall
        checks.append(PreflightCheck(name=name, status=status, detail=detail))
        if status == "fail":
            overall = "fail"
        elif status == "warn" and overall == "ok":
            overall = "warn"

    dest_norm = _normalize_dest(dest_volume)

    # ---- destination: not the system volume -----------------------
    if _is_system_volume(dest_norm):
        add("dest_not_system", "fail",
            f"dest_volume {dest_norm!r} is a system volume; "
            "install would clobber the running OS")
    else:
        add("dest_not_system", "ok", f"{dest_norm} is safe to write")

    # ---- sources_dir scan -----------------------------------------
    scan = scan_sources(sources_dir)
    if not scan["exists"]:
        add("sources_exists", "fail",
            f"sources_dir {sources_dir!r} not found or not a directory")
        return PreflightResult(
            target=target, dest_volume=dest_norm, machine=machine, iso=iso,
            sources_dir=str(Path(sources_dir).expanduser().resolve()),
            overall="fail", checks=checks,
            missing_files=[], forbidden_entries_present=[],
            summary="sources_dir not found",
        )
    add("sources_exists", "ok", scan["sources_dir"])

    # ---- machine resolution ---------------------------------------
    detected = scan["detected_machine"]
    canonical: str | None = None
    if machine:
        canonical = resolve_machine_alias(machine)
        if canonical is None:
            add("machine_known", "fail", f"unknown machine alias {machine!r}")
        else:
            add("machine_known", "ok", canonical)
    elif detected:
        canonical = detected
        add("machine_known", "ok", f"auto-detected from ISO: {canonical}")
    elif scan["ambiguous_machines"]:
        add("machine_known", "fail",
            f"multiple supported ISOs in sources_dir; specify machine: "
            f"{scan['ambiguous_machines']}")
    else:
        add("machine_known", "fail",
            "no supported ISO in sources_dir and no machine= given")

    if scan["unsupported_isos"]:
        add("unsupported_isos", "warn",
            f"unsupported ISOs present (will be ignored): "
            f"{scan['unsupported_isos']}")

    # ---- iso resolution / cross-check -----------------------------
    chosen_iso = iso
    if canonical and not chosen_iso:
        candidates = scan["iso_files"].get(canonical, [])
        lha_candidates = scan["iso_lha_files"].get(canonical, [])
        if len(candidates) == 1:
            chosen_iso = candidates[0]
            add("iso_resolved", "ok", chosen_iso)
        elif len(candidates) > 1:
            add("iso_resolved", "fail",
                f"multiple ISOs matching {canonical}: {candidates}; "
                f"specify iso=")
        elif len(lha_candidates) == 1:
            # Only the LHA-of-ISO form is present. Derive the bare
            # iso filename so install_<machine>(iso_filename=...) and
            # mount_iso know what to look for after extraction.
            chosen_iso = lha_candidates[0][:-len(".lha")]
            add("iso_resolved", "ok",
                f"{chosen_iso} (will extract from {lha_candidates[0]})")
        elif len(lha_candidates) > 1:
            add("iso_resolved", "fail",
                f"multiple iso.lha files matching {canonical}: "
                f"{lha_candidates}; specify iso=")
        else:
            add("iso_resolved", "fail",
                f"no ISO (raw or .iso.lha) matching machine "
                f"{canonical} in sources_dir")
    elif chosen_iso and canonical:
        info = detect_machine_from_iso(chosen_iso)
        if info is None:
            add("iso_resolved", "fail",
                f"iso filename {chosen_iso!r} doesn't match any "
                "supported prefix")
        elif info[0] != canonical:
            add("iso_resolved", "fail",
                f"iso {chosen_iso!r} is for {info[0]}, not {canonical}")
        else:
            iso_path = Path(scan["sources_dir"]) / chosen_iso
            if not iso_path.exists():
                add("iso_resolved", "fail",
                    f"iso {chosen_iso!r} not found in sources_dir")
            else:
                add("iso_resolved", "ok", chosen_iso)

    # ---- required-files manifest ----------------------------------
    if canonical:
        manifest = required_files(canonical)
        present_lhas = set(scan["lha_files"])
        for entry in manifest:
            if entry["role"] == "iso":
                continue  # handled by iso_resolved
            if entry["filename"] not in present_lhas:
                missing_files.append(entry["filename"])
        if missing_files:
            add("required_files", "fail",
                f"missing {len(missing_files)} required file(s): "
                f"{missing_files}")
        else:
            add("required_files", "ok",
                f"all {len(manifest) - 1} non-ISO files present")

        # Mandatory host-side binaries: just MCPd. The AOS 4.1
        # diskimage tools come from the running AmigaOS / the install
        # ISO at install time -- no host-side dependency.
        src_path = Path(scan["sources_dir"])
        mand = _mandatory_binaries(src_path)
        missing_mand = [m["name"] for m in mand if m["path"] is None]
        if missing_mand:
            add("mandatory_binaries", "fail",
                f"missing {len(missing_mand)} mandatory binar(ies): "
                f"{missing_mand}")
            missing_files.extend(missing_mand)
        else:
            add("mandatory_binaries", "ok",
                f"all {len(mand)} mandatory binaries resolved "
                f"({', '.join(m['name'] for m in mand)})")

        if canonical == "A1222":
            add("rescuev2_volume", "warn",
                "A1222 install requires RESCUEV2: or AAATECH0: USB "
                "volume mounted on the target; check separately on the "
                "Amiga side. Preflight doesn't probe Amiga volumes.")

    # ---- dest_volume mounted + clean ------------------------------
    target_resolved = fleet.resolve_target(target)
    try:
        mcpd = fleet.mcpd(target_resolved)
    except Exception as e:
        add("target_reachable", "fail", f"target unreachable: {e}")
        return PreflightResult(
            target=target_resolved, dest_volume=dest_norm,
            machine=canonical, iso=chosen_iso,
            sources_dir=scan["sources_dir"],
            overall="fail", checks=checks,
            missing_files=missing_files,
            forbidden_entries_present=[],
            summary="target not reachable for fs checks",
        )

    # Stat the dest_volume root.
    try:
        await mcpd.request("fs.stat", {"path": dest_norm})
        add("dest_mounted", "ok", f"{dest_norm} is reachable")
    except Exception as e:
        add("dest_mounted", "fail",
            f"{dest_norm} not reachable: {e}; "
            "is the partition mounted on the target?")
        return PreflightResult(
            target=target_resolved, dest_volume=dest_norm,
            machine=canonical, iso=chosen_iso,
            sources_dir=scan["sources_dir"],
            overall="fail", checks=checks,
            missing_files=missing_files,
            forbidden_entries_present=[],
            summary=f"{dest_norm} not mounted on target",
        )

    # Walk FORBIDDEN_DEST_ENTRIES, fs.stat each.
    for forbidden in FORBIDDEN_DEST_ENTRIES:
        try:
            await mcpd.request("fs.stat", {"path": dest_norm + forbidden})
            forbidden_present.append(forbidden)
        except Exception:
            pass  # not present — good
    if forbidden_present:
        add("dest_clean", "fail",
            f"{dest_norm} contains existing AOS install: "
            f"{forbidden_present}; format the partition first")
    else:
        add("dest_clean", "ok",
            f"none of {len(FORBIDDEN_DEST_ENTRIES)} forbidden entries "
            f"present on {dest_norm}")

    summary = (
        f"preflight {overall}: machine={canonical} iso={chosen_iso} "
        f"dest={dest_norm} missing={len(missing_files)} "
        f"forbidden={len(forbidden_present)}"
    )

    return PreflightResult(
        target=target_resolved,
        dest_volume=dest_norm,
        machine=canonical,
        iso=chosen_iso,
        sources_dir=scan["sources_dir"],
        overall=overall,
        checks=checks,
        missing_files=missing_files,
        forbidden_entries_present=forbidden_present,
        summary=summary,
    )


# ===================================================================
# Step primitives
# ===================================================================


def _require_confirm(confirm: bool, op: str) -> None:
    if not confirm:
        raise InvalidParams(
            f"{op} requires confirm=True (mutating operation)",
            data={"hint": "pass confirm=True to acknowledge"},
        )


def _normalize_volume(vol: str) -> str:
    """Return `BootTest:` form (with trailing colon, no trailing slash)."""
    v = vol.strip()
    if v.endswith("/"):
        v = v[:-1]
    if not v.endswith(":"):
        v = v + ":"
    return v


# ---- mount / unmount ISO -----------------------------------------


class MountIsoResult(BaseModel):
    target: str
    iso_path: str
    dos_device: str
    unit: int
    expected_volume: str | None
    detected_volume: str | None
    mounted: bool
    settled: bool
    elapsed_s: float


async def installer_mount_iso(
    fleet: Fleet, target: str, *,
    iso_path: str,
    dos_device: str = "COMBI:",
    unit: int = 50,
    expected_volume: str | None = "AmigaOS 4.1 Final Edition:",
    timeout_s: float = 30.0,
    settle_s: float = 3.0,
) -> MountIsoResult:
    """Mount an ISO file as a virtual CD on the target.

    Standard mount sequence:
      1. `C:MountDiskImage U=<unit> INSERT="<iso_path>"` — inserts media
      2. (best-effort) Mount the DOS device if not already live
      3. Poll for the expected volume to appear (up to timeout_s)
      4. Settle pause (lets CDFS finish initialising)

    The diskimage.device + MountDiskImage + CDFileSystem must already
    be present on the target (staged via the bootstrap-files step).
    Per-machine sequences are responsible for staging those before
    calling this primitive. Reversible via unmount_iso.
    """
    import time
    t0 = time.monotonic()
    mcpd = fleet.mcpd(target)

    # Step 0: defensively EJECT any media currently in the unit. INSERT
    # on a unit that already has media is treated as a toggle by
    # MountDiskImage in some configurations -- the previously-inserted
    # ISO comes OUT and the volume goes away. Eject first (harmless
    # when nothing was inserted; rc=0 either way) so INSERT below
    # always operates on a known-empty unit.
    try:
        await mcpd.request("exec.cmd", {
            "command": f"C:MountDiskImage U={unit} EJECT",
            "timeout_ms": 10000,
        }, timeout_s=15.0)
        await asyncio.sleep(1.0)
    except Exception:
        pass

    # Step 1: insert media into the diskimage.device unit.
    insert_cmd = f'C:MountDiskImage U={unit} INSERT="{iso_path}"'
    await mcpd.request("exec.cmd", {
        "command": insert_cmd, "timeout_ms": int(timeout_s * 1000),
    }, timeout_s=max(timeout_s + 5.0, 30.0))
    await asyncio.sleep(2.0)  # let diskimage.device process the insert

    # Step 2: only Mount the DOS device if it ISN'T already a live DOS
    # device. Mount on an already-mounted device pops a system
    # requester ("device already mounted") which blocks the install
    # indefinitely. fs.stat(dos_device) underneath calls IDOS->Lock —
    # which is the SDK-recommended way to test for liveness — so a
    # successful stat means "device is mounted AND has media", which
    # is the post-INSERT state we expect when COMBI: was already up
    # from a previous run.
    device_live = False
    try:
        await mcpd.request("fs.stat", {"path": dos_device})
        device_live = True
    except Exception:
        pass

    if not device_live:
        drv_path = "DEVS:DOSDrivers/" + dos_device.rstrip(":")
        mount_cmd = f'Mount "{drv_path}"'
        try:
            await mcpd.request("exec.cmd", {
                "command": mount_cmd,
                "timeout_ms": int(timeout_s * 1000),
            }, timeout_s=max(timeout_s + 5.0, 30.0))
        except Exception:
            pass  # Mount failure here is non-fatal — the volume-poll
                  # in step 3 will catch a real problem.

    # Step 3: poll for the volume to appear via fs.stat.
    detected_volume: str | None = None
    settled = False
    if expected_volume:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                await mcpd.request("fs.stat", {"path": expected_volume})
                detected_volume = expected_volume
                break
            except Exception:
                await asyncio.sleep(1.0)
        if detected_volume:
            await asyncio.sleep(settle_s)
            settled = True

    return MountIsoResult(
        target=target,
        iso_path=iso_path,
        dos_device=dos_device,
        unit=unit,
        expected_volume=expected_volume,
        detected_volume=detected_volume,
        mounted=detected_volume is not None,
        settled=settled,
        elapsed_s=time.monotonic() - t0,
    )


class UnmountIsoResult(BaseModel):
    target: str
    unit: int
    output: str


async def installer_unmount_iso(
    fleet: Fleet, target: str, *,
    unit: int = 50,
    timeout_s: float = 15.0,
) -> UnmountIsoResult:
    """Eject the ISO from a diskimage.device unit. Always safe to
    call — on a unit with no media, MountDiskImage returns harmlessly."""
    cmd = f"C:MountDiskImage U={unit} EJECT"
    raw = await fleet.mcpd(target).request("exec.cmd", {
        "command": cmd, "timeout_ms": int(timeout_s * 1000),
    }, timeout_s=max(timeout_s + 5.0, 30.0))
    return UnmountIsoResult(
        target=target, unit=unit, output=str(raw.get("output", "")),
    )


# ---- copy / lha ---------------------------------------------------


class CopyTreeResult(BaseModel):
    target: str
    src: str
    dst: str
    output: str
    exit_code: int
    duration_s: float


async def installer_copy_tree(
    fleet: Fleet, target: str, *,
    src: str, dst: str,
    all_: bool = True,
    clone: bool = True,
    quiet: bool = True,
    confirm: bool,
    timeout_s: float = 600.0,
) -> CopyTreeResult:
    """Recursive AmigaDOS Copy: `Copy <src> <dst> ALL CLONE QUIET`.

    Mutating; requires confirm=True. Default 10-minute timeout suits
    multi-GB tree copies; bump for very slow targets.
    """
    _require_confirm(confirm, "installer.copy_tree")
    flags = []
    if all_:
        flags.append("ALL")
    if clone:
        flags.append("CLONE")
    if quiet:
        flags.append("QUIET")
    cmd = f'Copy "{src}" "{dst}"' + (" " + " ".join(flags) if flags else "")
    import time
    t0 = time.monotonic()
    raw = await fleet.mcpd(target).request("exec.cmd", {
        "command": cmd, "timeout_ms": int(timeout_s * 1000),
    }, timeout_s=max(timeout_s + 30.0, 60.0))
    return CopyTreeResult(
        target=target, src=src, dst=dst,
        output=str(raw.get("output", "")),
        exit_code=int(raw.get("exit_code", 0)),
        duration_s=time.monotonic() - t0,
    )


class ApplyLhaResult(BaseModel):
    target: str
    archive: str
    dest: str
    output: str
    exit_code: int
    duration_s: float


async def installer_apply_lha(
    fleet: Fleet, target: str, *,
    archive: str, dest: str,
    confirm: bool,
    timeout_s: float = 600.0,
) -> ApplyLhaResult:
    """Extract an LHA archive into a destination directory:
    `LhA >NIL: x <archive> <dest>/`.

    Caller is responsible for ensuring `dest` exists (use exec.cmd
    'MakeDir <dest> ALL' if needed). Mutating; requires confirm=True.
    """
    _require_confirm(confirm, "installer.apply_lha")
    dest_with_slash = dest if dest.endswith("/") else dest + "/"
    cmd = f'LhA >NIL: x "{archive}" "{dest_with_slash}"'
    import time
    t0 = time.monotonic()
    raw = await fleet.mcpd(target).request("exec.cmd", {
        "command": cmd, "timeout_ms": int(timeout_s * 1000),
    }, timeout_s=max(timeout_s + 30.0, 60.0))
    return ApplyLhaResult(
        target=target, archive=archive, dest=dest,
        output=str(raw.get("output", "")),
        exit_code=int(raw.get("exit_code", 0)),
        duration_s=time.monotonic() - t0,
    )


# ---- kicklayout ---------------------------------------------------


def _kicklayout_path(dest_volume: str) -> str:
    return _normalize_volume(dest_volume) + "Kickstart/Kicklayout"


class ReadKicklayoutResult(BaseModel):
    target: str
    dest_volume: str
    path: str
    content: str
    size: int


async def installer_read_kicklayout(
    fleet: Fleet, target: str, *,
    dest_volume: str,
) -> ReadKicklayoutResult:
    """Read the Kicklayout file from a destination volume. Returns the
    decoded text — Kicklayout is plain ASCII so no binary handling
    needed."""
    klpath = _kicklayout_path(dest_volume)
    raw = await fleet.mcpd(target).request("fs.read", {"path": klpath})
    content_b64 = raw.get("content_b64", "")
    text = base64.b64decode(content_b64).decode("ascii", errors="replace")
    return ReadKicklayoutResult(
        target=target,
        dest_volume=_normalize_volume(dest_volume),
        path=klpath,
        content=text,
        size=len(text),
    )


class WriteKicklayoutResult(BaseModel):
    target: str
    dest_volume: str
    path: str
    bytes_written: int
    backup_path: str | None


async def installer_write_kicklayout(
    fleet: Fleet, target: str, *,
    dest_volume: str,
    content: str,
    confirm: bool,
    backup_suffix: str | None = ".bak",
) -> WriteKicklayoutResult:
    """Write a new Kicklayout file. Mutating; requires confirm=True.

    If `backup_suffix` is set, the existing file (if any) is copied to
    <Kicklayout>+<backup_suffix> first. Pass backup_suffix=None to skip
    the backup (e.g. when you've already taken one).
    """
    _require_confirm(confirm, "installer.write_kicklayout")
    klpath = _kicklayout_path(dest_volume)
    mcpd = fleet.mcpd(target)

    backup_path: str | None = None
    if backup_suffix:
        try:
            await mcpd.request("fs.stat", {"path": klpath})
            # Existing file -> back it up.
            backup_path = klpath + backup_suffix
            await mcpd.request("fs.copy", {
                "src": klpath, "dst": backup_path,
            })
        except Exception:
            backup_path = None  # no existing file, no backup needed

    content_b64 = base64.b64encode(content.encode("ascii")).decode("ascii")
    await mcpd.request("fs.write", {
        "path": klpath, "content_b64": content_b64,
    }, timeout_s=120.0)
    return WriteKicklayoutResult(
        target=target,
        dest_volume=_normalize_volume(dest_volume),
        path=klpath,
        bytes_written=len(content.encode("ascii")),
        backup_path=backup_path,
    )


class PatchKicklayoutResult(BaseModel):
    target: str
    dest_volume: str
    path: str
    changed: bool
    modules_added: int
    labels_applied: list[str]
    replacements_done: int
    replacements: list[dict]
    bytes_written: int
    backup_path: str | None


async def installer_patch_kicklayout(
    fleet: Fleet, target: str, *,
    dest_volume: str,
    add_modules: list[dict] | None = None,
    replace_text: list[dict] | None = None,
    confirm: bool,
    backup_suffix: str | None = ".bak",
) -> PatchKicklayoutResult:
    """Atomic read-modify-write of the Kicklayout. Composes
    read_kicklayout + kicklayout text engine + write_kicklayout.

    `add_modules`: list of `{"modules": [str], "label": str}` — each
    entry adds those modules into every config block where they're
    missing. Idempotent.

    `replace_text`: list of `{"old": str, "new": str}` — verbatim
    substring replacement. Mirrors X5000 SATA module rename.

    Mutating; requires confirm=True. If patches result in no change,
    the file is not rewritten and `changed=False` is returned.
    """
    _require_confirm(confirm, "installer.patch_kicklayout")
    rd = await installer_read_kicklayout(
        fleet, target, dest_volume=dest_volume,
    )
    new_text, summary = kicklayout_engine.apply_patches(
        rd.content,
        add_modules_patches=add_modules,
        replace_text_patches=replace_text,
    )
    if not summary["changed"]:
        return PatchKicklayoutResult(
            target=target, dest_volume=rd.dest_volume, path=rd.path,
            changed=False, modules_added=0, labels_applied=[],
            replacements_done=0, replacements=[],
            bytes_written=0, backup_path=None,
        )
    # Capture original mtime so the rewrite-after-patch doesn't show
    # the file as "today" in Workbench. Kicklayout is from the ISO
    # via copy_base_os; CLONE preserved its date through the copy.
    mcpd = fleet.mcpd(target)
    orig_mtime = None
    try:
        s = await mcpd.request("fs.stat", {"path": rd.path}, timeout_s=10.0)
        orig_mtime = s.get("modified") if isinstance(s, dict) else None
    except Exception:
        pass
    wr = await installer_write_kicklayout(
        fleet, target,
        dest_volume=rd.dest_volume, content=new_text,
        confirm=True, backup_suffix=backup_suffix,
    )
    # Restore the captured mtime via AmigaDOS SetDate.
    if orig_mtime:
        from ..installer.sequences._steps import _aos_modified_to_setdate
        setdate = _aos_modified_to_setdate(orig_mtime)
        if setdate:
            try:
                await mcpd.request("exec.cmd", {
                    "command": f'SetDate "{rd.path}" {setdate}',
                    "timeout_ms": 10000,
                }, timeout_s=15.0)
            except Exception:
                pass
    return PatchKicklayoutResult(
        target=target, dest_volume=rd.dest_volume, path=rd.path,
        changed=True,
        modules_added=summary["modules_added"],
        labels_applied=summary["labels_applied"],
        replacements_done=summary["replacements_done"],
        replacements=summary["replacements"],
        bytes_written=wr.bytes_written,
        backup_path=wr.backup_path,
    )


# ===================================================================
# Per-machine install sequences
# ===================================================================

from ..installer.sequences import (  # noqa: E402
    SequenceResult,
    SequenceRunner,
)
from ..installer.sequences import (  # noqa: E402
    a1222 as _a1222_seq,
)
from ..installer.sequences import (  # noqa: E402
    amigaone as _amigaone_seq,
)
from ..installer.sequences import (  # noqa: E402
    pegasos2 as _pegasos2_seq,
)
from ..installer.sequences import (  # noqa: E402
    sam460 as _sam460_seq,
)
from ..installer.sequences import (  # noqa: E402
    x5000 as _x5000_seq,
)

_SEQUENCE_BUILDERS = {
    "X5000":     _x5000_seq.build,
    "PegasosII": _pegasos2_seq.build,
    "AmigaOne":  _amigaone_seq.build,
    "Sam460":    _sam460_seq.build,
    "A1222":     _a1222_seq.build,
}


def _resolve_sequence_machine(machine: str) -> str:
    canonical = resolve_machine_alias(machine)
    if canonical is None:
        raise InvalidParams(
            f"unknown machine {machine!r}; "
            f"supported: {', '.join(SUPPORTED_MACHINES)}",
        )
    if canonical not in _SEQUENCE_BUILDERS:
        impl = ", ".join(sorted(_SEQUENCE_BUILDERS))
        raise InvalidParams(
            f"machine {canonical!r} sequence not yet implemented "
            f"(implemented: {impl})",
            data={"machine": canonical, "implemented": list(_SEQUENCE_BUILDERS)},
        )
    return canonical


async def installer_run(
    fleet: Fleet, target: str, *,
    dest_volume: str,
    machine: str,
    iso_filename: str | None = None,
    sources_dir: str | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> SequenceResult:
    """Run a per-machine install sequence.

    `dry_run=True` (default) returns the planned step list without
    executing — preview before commitment. To actually run, pass
    `dry_run=False, confirm=True`.

    `iso_filename` is the basename of the ISO under `<dest>:tmp/`. If
    omitted and `sources_dir` is given, we auto-detect from a host-side
    scan_sources of `sources_dir`.

    The caller is responsible for having already staged ISO + LHAs
    into `<dest>:tmp/` before this runs (use `installer.stage` to do
    that).
    """
    canonical = _resolve_sequence_machine(machine)

    if iso_filename is None:
        if not sources_dir:
            raise InvalidParams(
                "must pass iso_filename, or sources_dir for auto-detect",
            )
        scan = scan_sources(sources_dir)
        candidates = scan["iso_files"].get(canonical, [])
        lha_candidates = scan["iso_lha_files"].get(canonical, [])
        if len(candidates) == 1:
            iso_filename = candidates[0]
        elif len(lha_candidates) == 1:
            # Only the LHA-of-ISO form. Derive the bare iso name —
            # extract_iso_lha will recover it on the target before
            # mount_iso runs.
            iso_filename = lha_candidates[0][:-len(".lha")]
        else:
            raise InvalidParams(
                f"could not auto-detect iso_filename: "
                f"{len(candidates)} bare ISO + {len(lha_candidates)} "
                f"iso.lha candidates for {canonical} in {sources_dir}",
                data={"iso_candidates": candidates,
                      "iso_lha_candidates": lha_candidates},
            )

    if not dry_run and not confirm:
        raise InvalidParams(
            "installer.run requires confirm=True when dry_run=False "
            "(install will rewrite the destination volume)",
        )

    sequence = _SEQUENCE_BUILDERS[canonical](iso_filename=iso_filename)

    ctx = {
        "fleet": fleet,
        "target": target,
        "mcpd": fleet.mcpd(target),
        "dest_volume": _normalize_volume(dest_volume),
        "machine": canonical,
        "iso_filename": iso_filename,
    }
    runner = SequenceRunner(sequence=sequence, initial_ctx=ctx)
    return await runner.run(dry_run=dry_run)


async def installer_install_x5000(
    fleet: Fleet, target: str, *,
    dest_volume: str,
    iso_filename: str | None = None,
    sources_dir: str | None = None,
    dry_run: bool = True,
    confirm: bool = False,
) -> SequenceResult:
    """Convenience for installer_run(machine='X5000')."""
    return await installer_run(
        fleet, target,
        dest_volume=dest_volume, machine="X5000",
        iso_filename=iso_filename, sources_dir=sources_dir,
        dry_run=dry_run, confirm=confirm,
    )


# ===================================================================
# Staging + verification
# ===================================================================


from ..installer._amiga_fs import amiga_makedir  # noqa: E402
from ..upload import chunked_upload  # noqa: E402


class StagedFile(BaseModel):
    role: str            # "iso" / "update" / "enhancer" / "extra" / "bootstrap"
    src: str
    dst: str
    bytes_total: int
    bytes_sent_compressed: int
    elapsed_s: float
    compression_ratio: float


class InstallerStageResult(BaseModel):
    target: str
    dest_volume: str
    machine: str
    iso_filename: str
    staging_dir: str
    files_staged: list[StagedFile]
    total_bytes: int
    total_compressed: int
    total_elapsed_s: float
    skipped: list[str]


def _install_support_root() -> Path | None:
    """Resolve the host-side install-support root (a directory that
    holds AmiDock prefs XML and any other small files a user wants to
    override the bundled defaults with).

    Lookup order:
      1. $AMIGA_INSTALL_SUPPORT_DIR if set and exists.
      2. ./install-support relative to cwd.
      3. ~/install-support in the user's home directory.

    Set the env var when running from anywhere other than a checkout
    that has the support tree alongside it.
    """
    import os
    env = os.environ.get("AMIGA_INSTALL_SUPPORT_DIR")
    if env:
        p = Path(env).expanduser().resolve()
        if p.is_dir():
            return p
    for cand in (Path.cwd() / "install-support",
                 Path.home() / "install-support"):
        if cand.is_dir():
            return cand
    return None


def _candidate_install_support_dirs() -> list[Path]:
    """Where to look for AmiDock.amiga.com.xml + other host-side
    install-support files. Each install copies them to <dest>:tmp/
    for the sequence steps to use."""
    out: list[Path] = []
    root = _install_support_root()
    if root is not None:
        out.append(root)
    out.extend([
        Path.cwd(),
        Path.home(),
    ])
    # Bundled package resources ship a default AmiDock.amiga.com.xml
    # so the install pipeline doesn't depend on the user staging a copy
    # by hand. Other resources can sit alongside it.
    out.append(Path(__file__).resolve().parent.parent
               / "installer" / "resources")
    return out


def _find_in_sources_or_support(filename: str,
                                sources_dir: Path) -> Path | None:
    """Look for `filename` in sources_dir first, then in the host-side
    install-support directories (including bundled package resources)."""
    cand = sources_dir / filename
    if cand.is_file():
        return cand
    for d in _candidate_install_support_dirs():
        cand = d / filename
        if cand.is_file():
            return cand
    return None


def _resolve_mcpd_binary(sources_dir: Path) -> Path | None:
    """Resolve the MCPd binary. Looks in:
      1. <sources_dir>/MCPd
      2. <repo>/mcpd/MCPd  (this repo's freshly-built daemon)
      3. The host-side install-support tree.
    Returns the first hit, or None if nothing matches."""
    cand = sources_dir / "MCPd"
    if cand.is_file():
        return cand
    # Repo-local fresh build (most common for development).
    for repo_root in (Path.cwd(), Path(__file__).resolve().parents[3]):
        cand = repo_root / "mcpd" / "MCPd"
        if cand.is_file():
            return cand
    for d in _candidate_install_support_dirs():
        cand = d / "MCPd"
        if cand.is_file():
            return cand
    return None


# What the preflight + stage need to find before allowing an install.
# Each entry has: name, friendly description, and a resolver function
# returning a Path or None.
def _mandatory_binaries(sources_dir: Path) -> list[dict]:
    """Build the list of mandatory host-side binaries for an install
    with each resolved location (or None if missing).

    Currently MCPd is the only host-side binary required: AOS 4.1's
    diskimage tools (MountDiskImage / diskimage.device / CDFileSystem)
    are sourced from the running AmigaOS at install time and from the
    install ISO into the dest drive via copy_base_os, so they don't
    need to be staged from the host.

    This list MUST be all-found before installer_stage runs. preflight
    cross-checks the same list and reports them in `missing_files`."""
    mcpd = _resolve_mcpd_binary(sources_dir)
    return [
        {"name": "MCPd",
         "desc": "MCPd daemon binary (auto-starts on first boot)",
         "path": mcpd},
    ]


async def installer_stage(
    fleet: Fleet, target: str, *,
    dest_volume: str,
    sources_dir: str,
    machine: str,
    iso_filename: str | None = None,
    iso_lha_path: str | None = None,
    confirm: bool = False,
) -> InstallerStageResult:
    """Upload ISO + Update LHAs + Enhancer + extras + MCPd + AmiDock
    prefs into `<dest>:tmp/`.

    Mutating; requires `confirm=True` (a multi-GB upload is not
    something to fire by accident).

    The AOS 4.1 diskimage tools (MountDiskImage / diskimage.device /
    CDFileSystem) are NOT staged from the host -- the running
    AmigaOS uses its own copies at mount time, and the dest drive
    receives them via copy_base_os from the install ISO's System/
    tree. No host-side `diskimage-bootstrap/` directory is needed.

    `iso_filename` may be omitted; we detect from `sources_dir` via
    scan_sources.

    `iso_lha_path` (optional): path to a `<iso>.iso.lha` file (an LHA
    archive containing the bare .iso file as a single member). When
    set, we upload the LHA instead of the raw ISO — typically saves
    ~60% bandwidth since ISOs compress well. If the user's
    sources_dir already has `<iso>.lha` next to the ISO and this arg
    is omitted, we auto-detect it.

    The on-target sequence's `extract_iso_lha` step recovers the .iso
    from the .lha at install time. Either form is sufficient — caller
    needs to provide one.
    """
    if not confirm:
        raise InvalidParams(
            "installer.stage requires confirm=True (multi-GB upload)",
        )

    canonical = resolve_machine_alias(machine)
    if canonical is None:
        raise InvalidParams(
            f"unknown machine {machine!r}; "
            f"supported: {', '.join(SUPPORTED_MACHINES)}",
        )

    src = Path(sources_dir).expanduser().resolve()
    if not src.is_dir():
        raise InvalidParams(f"sources_dir not found: {sources_dir!r}")

    scan = scan_sources(src)
    if iso_filename is None:
        candidates = scan["iso_files"].get(canonical, [])
        lha_candidates = scan["iso_lha_files"].get(canonical, [])
        if len(candidates) == 1:
            iso_filename = candidates[0]
        elif len(lha_candidates) == 1:
            # Derive the bare ISO name from the .iso.lha. The LHA
            # itself will be auto-detected as the upload form below.
            iso_filename = lha_candidates[0][:-len(".lha")]
        else:
            raise InvalidParams(
                f"could not auto-detect iso_filename for {canonical}: "
                f"{len(candidates)} bare ISO + {len(lha_candidates)} "
                f"iso.lha candidates in sources_dir",
                data={"iso_candidates": candidates,
                      "iso_lha_candidates": lha_candidates},
            )
    elif (not (src / iso_filename).is_file()
          and not (src / (iso_filename + ".lha")).is_file()
          and iso_lha_path is None):
        raise InvalidParams(
            f"neither {iso_filename!r} nor {iso_filename + '.lha'!r} "
            f"found in sources_dir, and no iso_lha_path given",
        )

    # Mandatory-binaries check: just MCPd. Fail loudly if missing --
    # previously this was silently skipped, which left installs
    # without auto-starting MCPd.
    mand = _mandatory_binaries(src)
    missing_mand = [m for m in mand if m["path"] is None]
    if missing_mand:
        raise InvalidParams(
            "installer.stage cannot proceed: mandatory file(s) missing.\n"
            + "\n".join(
                f"  - {m['name']}: {m['desc']}" for m in missing_mand
            ) + "\nPut MCPd in sources_dir or build it with "
            "`make -C mcpd docker-build`.",
            data={"missing": [m["name"] for m in missing_mand]},
        )

    dest_norm = _normalize_volume(dest_volume)
    staging = dest_norm + "tmp/"
    files_staged: list[StagedFile] = []
    skipped: list[str] = []
    total_bytes = 0
    total_compressed = 0
    import time
    t0 = time.monotonic()

    # Build the upload list: [(role, src_path, dst_path), ...]
    items: list[tuple[str, Path, str]] = []

    # ISO transfer: prefer the LHA-of-ISO form when available.
    # Sources for the LHA, in priority order:
    #   1. explicit iso_lha_path arg
    #   2. <iso_filename>.lha next to the iso in sources_dir
    iso_lha_local: Path | None = None
    if iso_lha_path:
        cand = Path(iso_lha_path).expanduser().resolve()
        if cand.is_file():
            iso_lha_local = cand
        else:
            raise InvalidParams(
                f"iso_lha_path not found: {iso_lha_path!r}",
            )
    else:
        cand = src / (iso_filename + ".lha")
        if cand.is_file():
            iso_lha_local = cand

    if iso_lha_local is not None:
        # Upload the LHA; the on-target extract_iso_lha step recovers
        # the .iso file. Remote name MUST match `<iso_filename>.lha`
        # so the step finds it.
        items.append((
            "iso_lha",
            iso_lha_local,
            staging + iso_filename + ".lha",
        ))
    else:
        items.append(("iso", src / iso_filename, staging + iso_filename))

    manifest = required_files(canonical)
    for entry in manifest:
        if entry["role"] == "iso":
            continue
        local = src / entry["filename"]
        if not local.is_file():
            skipped.append(f"{entry['role']}: {entry['filename']} not in sources_dir")
            continue
        items.append((entry["role"], local, staging + entry["filename"]))

    # MCPd binary (mandatory; presence already verified above).
    mcpd_local = _resolve_mcpd_binary(src)
    assert mcpd_local is not None  # _mandatory_binaries enforced this
    items.append(("mcpd_binary", mcpd_local, staging + "MCPd"))

    # SerialShell binary (optional). Useful only for users who run
    # qemu-runner / AmigaQemuTests workflows on the resulting install;
    # MCPd itself doesn't need it. The install_serialshell sequence
    # step skips silently when this isn't staged.
    ss_local = _find_in_sources_or_support("SerialShell", src)
    if ss_local is not None:
        items.append(("serialshell_binary", ss_local,
                      staging + "SerialShell"))
    else:
        skipped.append(
            "serialshell_binary: SerialShell not found in sources_dir; "
            "installed AmigaOS will not have SerialShell available "
            "(only relevant for qemu-runner workflows)."
        )

    # AmiDock prefs XML (universal). Replaces the stock dock layout
    # with one that points at the pre-installed Filer/Ranger/IBrowse/
    # DiskImageGUI entries.
    amidock_local = _find_in_sources_or_support(
        "AmiDock.amiga.com.xml", src
    )
    if amidock_local is not None:
        items.append(("amidock_prefs", amidock_local,
                      staging + "AmiDock.amiga.com.xml"))
    else:
        skipped.append(
            "AmiDock.amiga.com.xml not found -- patch_amidock_prefs "
            "step will be a no-op (dock will use stock layout)"
        )

    # Ensure tmp/ exists on the target
    mcpd = fleet.mcpd(target)
    await amiga_makedir(mcpd, staging)

    # 24 MiB raw + base64 (1.33x) lands at ~32 MiB which exceeds the
    # MCPD_FRAME_MAX_PAYLOAD cap once envelope overhead is included.
    # 16 MiB raw -> ~21 MiB on the wire, comfortably under the cap
    # for incompressible ISO data where zlib gives no help.
    SAFE_CHUNK_RAW = 16 * 1024 * 1024
    for role, local, remote in items:
        stats = await chunked_upload(
            fleet, target, str(local), remote,
            chunk_size=SAFE_CHUNK_RAW,
            compression="auto", retries=2,
        )
        files_staged.append(StagedFile(
            role=role, src=str(local), dst=remote,
            bytes_total=stats.bytes_total,
            bytes_sent_compressed=stats.bytes_sent_compressed,
            elapsed_s=stats.elapsed_s,
            compression_ratio=stats.compression_ratio,
        ))
        total_bytes += stats.bytes_total
        total_compressed += stats.bytes_sent_compressed

    return InstallerStageResult(
        target=target,
        dest_volume=dest_norm,
        machine=canonical,
        iso_filename=iso_filename,
        staging_dir=staging,
        files_staged=files_staged,
        total_bytes=total_bytes,
        total_compressed=total_compressed,
        total_elapsed_s=time.monotonic() - t0,
        skipped=skipped,
    )


# ---- verify -------------------------------------------------------


# Per-machine post-install presence manifest. These are the files we
# expect to find on a complete install. Extending beyond presence to
# sha256 verification requires an oracle run; the `expected_sha256`
# field is reserved for that.
_VERIFY_MANIFEST: dict[str, list[dict[str, str | None]]] = {
    "X5000": [
        # Base OS expected after copy_base_os
        {"path": "S/Startup-Sequence", "expected_sha256": None},
        {"path": "S/User-Startup", "expected_sha256": None},
        {"path": "Devs/Monitors", "expected_sha256": None},
        {"path": "C/", "expected_sha256": None},
        {"path": "L/", "expected_sha256": None},
        {"path": "Libs/", "expected_sha256": None},
        {"path": "Kickstart/", "expected_sha256": None},
        {"path": "Kickstart/Kicklayout", "expected_sha256": None},
        # Enhancer
        {"path": "Kickstart/RadeonRX.chip", "expected_sha256": None},
        {"path": "Kickstart/RadeonHD.chip", "expected_sha256": None},
        {"path": "Kickstart/SmartFilesystem", "expected_sha256": None},
        {"path": "Kickstart/diskcache.library.kmod", "expected_sha256": None},
        {"path": "Devs/Networks/p50x0_eth.device", "expected_sha256": None},
        # NGF (X5000 only)
        {"path": "Kickstart/NGFileSystem", "expected_sha256": None},
        {"path": "C/NGFCheck", "expected_sha256": None},
    ],
    "PegasosII": [
        {"path": "S/Startup-Sequence", "expected_sha256": None},
        {"path": "Kickstart/Kicklayout", "expected_sha256": None},
        {"path": "Kickstart/RadeonRX.chip", "expected_sha256": None},
        {"path": "Kickstart/SmartFilesystem", "expected_sha256": None},
        {"path": "amigaboot.of", "expected_sha256": None},
    ],
    "AmigaOne": [
        {"path": "S/Startup-Sequence", "expected_sha256": None},
        {"path": "Kickstart/Kicklayout", "expected_sha256": None},
        {"path": "Kickstart/RadeonRX.chip", "expected_sha256": None},
        {"path": "Kickstart/SmartFilesystem", "expected_sha256": None},
        {"path": "amigaboot.of", "expected_sha256": None},
    ],
    "Sam460": [
        {"path": "S/Startup-Sequence", "expected_sha256": None},
        {"path": "Kickstart/Kicklayout", "expected_sha256": None},
        {"path": "Kickstart/RadeonRX.chip", "expected_sha256": None},
        {"path": "Kickstart/SmartFilesystem", "expected_sha256": None},
        {"path": "amigaboot.of", "expected_sha256": None},
    ],
    "A1222": [
        # Same shape as X5000 minus the X5000-specific bits; A1222
        # has its own ethernet driver (A1222eth.device) and skips
        # bootloader (SD-card-based loader, like X5000).
        {"path": "S/Startup-Sequence", "expected_sha256": None},
        {"path": "S/User-Startup", "expected_sha256": None},
        {"path": "Devs/Monitors", "expected_sha256": None},
        {"path": "C/", "expected_sha256": None},
        {"path": "L/", "expected_sha256": None},
        {"path": "Libs/", "expected_sha256": None},
        {"path": "Kickstart/", "expected_sha256": None},
        {"path": "Kickstart/Kicklayout", "expected_sha256": None},
        {"path": "Kickstart/RadeonRX.chip", "expected_sha256": None},
        {"path": "Kickstart/RadeonHD.chip", "expected_sha256": None},
        {"path": "Kickstart/SmartFilesystem", "expected_sha256": None},
        {"path": "Kickstart/diskcache.library.kmod", "expected_sha256": None},
        {"path": "Devs/Networks/A1222eth.device", "expected_sha256": None},
    ],
}


class VerifyEntry(BaseModel):
    path: str
    present: bool
    sha256: str | None = None
    expected_sha256: str | None = None
    sha256_ok: bool | None = None  # None when no expected hash
    error: str | None = None


class InstallerVerifyResult(BaseModel):
    target: str
    dest_volume: str
    machine: str
    overall: str  # "ok" / "fail"
    total: int
    present: int
    missing: int
    sha256_mismatch: int
    entries: list[VerifyEntry]
    summary: str


async def installer_verify(
    fleet: Fleet, target: str, *,
    dest_volume: str,
    machine: str,
    extra_paths: list[str] | None = None,
    check_sha256: bool = False,
) -> InstallerVerifyResult:
    """Walk the per-machine post-install manifest and verify each
    entry exists on the target. Optional sha256 check (only fires for
    entries with a populated expected_sha256 — currently none, until
    an oracle run produces them).

    `extra_paths` are added to the standard manifest with no sha256
    expectation — useful for ad-hoc spot checks.
    """
    canonical = resolve_machine_alias(machine)
    if canonical is None or canonical not in _VERIFY_MANIFEST:
        raise InvalidParams(
            f"no verify manifest for machine {machine!r}; "
            f"available: {sorted(_VERIFY_MANIFEST)}",
        )

    dest_norm = _normalize_volume(dest_volume)
    manifest = list(_VERIFY_MANIFEST[canonical])
    for p in extra_paths or []:
        manifest.append({"path": p, "expected_sha256": None})

    entries: list[VerifyEntry] = []
    present_count = 0
    missing_count = 0
    sha256_mismatch = 0
    mcpd = fleet.mcpd(target)

    for spec in manifest:
        rel = str(spec["path"])
        full = dest_norm + rel
        try:
            await mcpd.request("fs.stat", {"path": full})
            sha256 = None
            sha256_ok: bool | None = None
            if check_sha256 and spec.get("expected_sha256"):
                try:
                    raw = await mcpd.request("fs.hash", {
                        "path": full, "algo": "sha256",
                    })
                    sha256 = raw.get("hash")
                    sha256_ok = (sha256 == spec["expected_sha256"])
                    if not sha256_ok:
                        sha256_mismatch += 1
                except Exception as e:
                    entries.append(VerifyEntry(
                        path=rel, present=True,
                        expected_sha256=spec.get("expected_sha256"),
                        error=f"hash failed: {e}",
                    ))
                    continue
            entries.append(VerifyEntry(
                path=rel, present=True,
                sha256=sha256,
                expected_sha256=spec.get("expected_sha256"),
                sha256_ok=sha256_ok,
            ))
            present_count += 1
        except Exception as e:
            missing_count += 1
            entries.append(VerifyEntry(
                path=rel, present=False,
                expected_sha256=spec.get("expected_sha256"),
                error=str(e)[:200],
            ))

    overall = "ok" if (missing_count == 0 and sha256_mismatch == 0) else "fail"
    summary = (f"verify {overall}: present={present_count}/"
               f"{len(manifest)} missing={missing_count} "
               f"sha256_mismatch={sha256_mismatch}")
    return InstallerVerifyResult(
        target=target,
        dest_volume=dest_norm,
        machine=canonical,
        overall=overall,
        total=len(manifest),
        present=present_count,
        missing=missing_count,
        sha256_mismatch=sha256_mismatch,
        entries=entries,
        summary=summary,
    )
