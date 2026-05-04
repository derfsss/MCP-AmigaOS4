"""Parameterised step builders shared across machine sequences.

Per-machine sequence files (x5000.py, pegasos2.py, ...) call these
helpers with their `MachineConfig` dict to get back ready-to-run
`Step` objects. This keeps the per-machine files thin (~20 LOC each).
"""

from __future__ import annotations

import base64

from .._amiga_fs import amiga_makedir
from ..machines import UPDATE_LHA_NAME
from ._machine_config import MachineConfig
from ._runner import Step

# Constants shared across all machines.
ISO_VOLUME = "AmigaOS 4.1 Final Edition:"
COMBI_DEVICE = "COMBI:"
COMBI_UNIT = 50
MUI_PER_UPDATE = {1: "MUI2016R1", 2: "MUI2020R3", 3: "MUI5"}


# ---- bootstrap + mount --------------------------------------------


_AOS_MONTHS = {"Jan":1,"Feb":2,"Mar":3,"Apr":4,"May":5,"Jun":6,"Jul":7,
               "Aug":8,"Sep":9,"Oct":10,"Nov":11,"Dec":12}
_MON_BY_NUM = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]


def _aos_modified_to_setdate(modified: str) -> str | None:
    """Convert AOS fs.stat 'modified' string ('YY-Mon-DD HH:MM:SS') to
    the form AmigaDOS SetDate expects: 'DD-Mon-YY HH:MM:SS'.

    Returns None if the input doesn't match the expected format -- the
    caller should then skip the SetDate call rather than emit a bogus
    one.
    """
    if not modified:
        return None
    parts = modified.split()
    if len(parts) != 2:
        return None
    date_part, time_part = parts
    bits = date_part.split("-")
    if len(bits) != 3:
        return None
    yy, mon, dd = bits
    if mon not in _AOS_MONTHS:
        return None
    if not (yy.isdigit() and dd.isdigit()):
        return None
    return f"{dd}-{mon}-{yy} {time_part}"


async def _capture_mtime(mcpd, path: str) -> str | None:
    """Stat `path` and return its 'modified' string, or None if the
    path doesn't exist or stat fails. Used right before an fs.write
    that overwrites an existing file we want to keep dated as-was."""
    try:
        s = await mcpd.request("fs.stat", {"path": path}, timeout_s=10.0)
    except Exception:
        return None
    return s.get("modified") if isinstance(s, dict) else None


async def _restore_mtime(mcpd, path: str, original: str | None) -> None:
    """Run AmigaDOS `SetDate` on `path` to restore the timestamp we
    captured before an fs.write. No-op when original is None (caller
    didn't have one) or when the date can't be parsed.

    AmigaDOS won't error if the file is missing; we still suppress
    failures to be defensive."""
    if not original:
        return
    setdate = _aos_modified_to_setdate(original)
    if not setdate:
        return
    try:
        await mcpd.request("exec.cmd", {
            "command": f'SetDate "{path}" {setdate}',
            "timeout_ms": 10000,
        }, timeout_s=15.0)
    except Exception:
        pass


async def _maybe_rename(mcpd, src_path: str, dst_path: str) -> None:
    """If `src_path` exists, rename it to `dst_path` (deleting any
    existing file at `dst_path` first). No-op if `src_path` doesn't
    exist. Used by apply_one_update for XAD-mangled-name renames."""
    try:
        await mcpd.request("fs.stat", {"path": src_path})
    except Exception:
        return
    try:
        await mcpd.request("fs.stat", {"path": dst_path})
        try:
            await mcpd.request("exec.cmd", {
                "command": f'Delete *>NIL: "{dst_path}"',
                "timeout_ms": 10000,
            }, timeout_s=15.0)
        except Exception:
            pass
    except Exception:
        pass
    try:
        await mcpd.request("exec.cmd", {
            "command": f'Rename *>NIL: "{src_path}" "{dst_path}"',
            "timeout_ms": 10000,
        }, timeout_s=15.0)
    except Exception:
        pass


def stage_diskimage_tools() -> Step:
    """Copy MountDiskImage / diskimage.device / CDFileSystem onto the
    running system + write COMBI: DOSDriver mountfile. Idempotent."""
    async def fn(ctx):
        mcpd = ctx["mcpd"]
        dest = ctx["dest_volume"]
        bootstrap = f"{dest}tmp/diskimage-bootstrap/"
        for src_name, sys_path in (
            ("MountDiskImage", "C:MountDiskImage"),
            ("diskimage.device", "DEVS:diskimage.device"),
            ("CDFileSystem", "L:CDFileSystem"),
        ):
            try:
                await mcpd.request("fs.stat", {"path": sys_path})
                continue
            except Exception:
                pass
            await mcpd.request("fs.copy", {
                "src": bootstrap + src_name, "dst": sys_path,
            })
        drv_path = "DEVS:DOSDrivers/COMBI"
        try:
            await mcpd.request("fs.stat", {"path": drv_path})
        except Exception:
            await amiga_makedir(mcpd, "DEVS:DOSDrivers/")
            mountfile = (
                "/* COMBI: DOSDriver - written by amiga-fleet-mcp installer */\n"
                "FileSystem     = L:CDFileSystem\n"
                "Device         = diskimage.device\n"
                f"Unit           = {COMBI_UNIT}\n"
                "Flags          = 0\n"
                "BlocksPerTrack = 351000\n"
                "BlockSize      = 2048\n"
                "Mask           = 0x7ffffffe\n"
                "MaxTransfer    = 0x1000000\n"
                "Reserved       = 0\n"
                "Interleave     = 0\n"
                "LowCyl         = 0\n"
                "HighCyl        = 0\n"
                "Surfaces       = 1\n"
                "Buffers        = 64\n"
                "BufMemType     = 1\n"
                "BootPri        = 2\n"
                "GlobVec        = -1\n"
                "Mount          = 1\n"
                "Priority       = 10\n"
                "DosType        = 0x43643031\n"
                "StackSize      = 3000\n"
            )
            await mcpd.request("fs.write", {
                "path": drv_path,
                "content_b64": base64.b64encode(
                    mountfile.encode("ascii")
                ).decode("ascii"),
            })
        return {"installed": ["C:MountDiskImage", "DEVS:diskimage.device",
                              "L:CDFileSystem", drv_path]}
    return Step(
        name="stage_diskimage_tools",
        doc="Copy MountDiskImage/diskimage.device/CDFileSystem onto "
            "the running system + write COMBI: DOSDriver mountfile.",
        fn=fn,
    )


def extract_iso_lha(iso_filename_key: str = "iso_filename") -> Step:
    """Extract `<dest>:tmp/<iso>.lha` to recover the bare `.iso` file.

    No-op when:
      - the bare `.iso` already exists in tmp/ (uploaded directly), OR
      - no `.iso.lha` is present.

    When the LHA is present and the .iso isn't, runs `LhA x` on the
    target. The Hyperion-shipped LHA carries the `.iso` file as a
    single member with AmigaOS protection bits intact, so the
    extracted file is exactly what mount_iso expects. This saves
    ~60% of the ISO upload bandwidth (334 MiB LHA vs 855 MiB raw).
    """
    async def fn(ctx):
        from ...tools import installer as it
        mcpd = ctx["mcpd"]
        dest = ctx["dest_volume"]
        iso_filename = ctx[iso_filename_key]
        iso_path = f"{dest}tmp/{iso_filename}"
        lha_path = iso_path + ".lha"

        # If the bare ISO is already present, skip extract entirely.
        try:
            await mcpd.request("fs.stat", {"path": iso_path})
            return {"action": "skipped", "reason": "iso already present"}
        except Exception:
            pass

        # If the LHA isn't present either, nothing we can do here —
        # mount_iso will fail and surface the real problem.
        try:
            await mcpd.request("fs.stat", {"path": lha_path})
        except Exception:
            return {"action": "skipped",
                    "reason": "neither iso nor iso.lha in tmp/"}

        # Extract: LhA emits the .iso alongside the .lha. Default tmp
        # is the dest dir of the lha, which is what we want.
        await it.installer_apply_lha(
            ctx["fleet"], ctx["target"],
            archive=lha_path, dest=f"{dest}tmp",
            confirm=True, timeout_s=600.0,
        )

        # Confirm the .iso is now present.
        await mcpd.request("fs.stat", {"path": iso_path})
        # Mark for the cleanup step at end-of-install. We don't need
        # both the .lha AND the extracted .iso lying in tmp/ taking
        # ~1 GiB combined.
        ctx["iso_was_extracted_from_lha"] = True
        return {"action": "extracted",
                "from": lha_path, "to": iso_path}
    return Step(
        name="extract_iso_lha",
        doc=("If <dest>:tmp/<iso>.lha is staged but the bare .iso "
             "isn't, extract it via LhA x. No-op otherwise."),
        fn=fn,
    )


def mount_iso(iso_filename_key: str = "iso_filename") -> Step:
    """Mount <dest>:tmp/<iso_filename> as a virtual CD."""
    async def fn(ctx):
        from ...tools import installer as it
        dest = ctx["dest_volume"]
        iso_filename = ctx[iso_filename_key]
        iso_path = f"{dest}tmp/{iso_filename}"
        res = await it.installer_mount_iso(
            ctx["fleet"], ctx["target"],
            iso_path=iso_path,
            dos_device=COMBI_DEVICE, unit=COMBI_UNIT,
            expected_volume=ISO_VOLUME,
            timeout_s=30.0, settle_s=3.0,
        )
        if not res.mounted:
            raise RuntimeError(
                f"ISO did not mount at {ISO_VOLUME} within timeout"
            )
        ctx["iso_volume"] = res.detected_volume
        return res.model_dump()
    return Step(
        name="mount_iso",
        doc=f"Mount the install ISO into {COMBI_DEVICE} unit "
            f"{COMBI_UNIT}; expect volume {ISO_VOLUME}.",
        fn=fn,
    )


def verify_cd_signature(config: MachineConfig) -> Step:
    sig = config.get("cd_signature")

    async def fn(ctx):
        if not sig:
            return {"skipped": "no signature for this machine"}
        path = ctx["iso_volume"] + "CD-Version.txt"
        raw = await ctx["mcpd"].request("fs.read", {"path": path})
        text = base64.b64decode(raw.get("content_b64", "")).decode(
            "ascii", errors="replace",
        )
        if sig not in text:
            raise RuntimeError(
                f"CD-Version.txt does not contain {sig!r} -- "
                f"wrong ISO for {config['machine_id']}?"
            )
        return {"signature": sig, "matched": True}

    label = sig if sig else "skipped (Sam460 ISO has no signature)"
    return Step(
        name="verify_cd_signature",
        doc=f"Read <ISO>:CD-Version.txt and verify signature: {label}",
        fn=fn,
    )


def copy_base_os() -> Step:
    """Copy <ISO>:System/ -> <dest>: + Protect +rwed ALL."""
    async def fn(ctx):
        from ...tools import installer as it
        src = ctx["iso_volume"] + "System/"
        dest = ctx["dest_volume"]
        copy = await it.installer_copy_tree(
            ctx["fleet"], ctx["target"],
            src=src, dst=dest,
            confirm=True, timeout_s=1800.0,
        )
        await ctx["mcpd"].request("exec.cmd", {
            "command": f'Protect "{dest}" +rwed ALL QUIET',
            "timeout_ms": 60000,
        }, timeout_s=70.0)
        return {"exit_code": copy.exit_code, "duration_s": copy.duration_s}
    return Step(
        name="copy_base_os",
        doc="Recursive Copy of <ISO>:System/ -> <dest>:, then "
            "Protect +rwed ALL QUIET to clear CDFS read-only bits.",
        fn=fn,
    )


def copy_installation_extras() -> Step:
    """S/Startup-Sequence + S/User-Startup + Env-Archive prefs."""
    async def fn(ctx):
        mcpd = ctx["mcpd"]
        iso = ctx["iso_volume"]
        dest = ctx["dest_volume"]
        inst = iso + "Installation-Files/"

        await amiga_makedir(mcpd, dest + "S/")
        for f in ("Startup-Sequence", "User-Startup"):
            try:
                await mcpd.request("fs.copy", {
                    "src": inst + f, "dst": dest + "S/" + f,
                })
            except Exception:
                pass

        # Volume drawer icon -- without this the dest volume opens with
        # a default "no icon" appearance in Workbench rather than the
        # canonical disk icon.
        try:
            await mcpd.request("fs.copy", {
                "src": inst + "Disk.info",
                "dst": dest + "Disk.info",
            })
        except Exception:
            pass

        # Generic RTG monitor driver + icon to Storage/Monitors so
        # the user can drag-into-DEVS-Monitors and configure per-board
        # post-install. Building per-board icons with custom tooltypes
        # would need target-side icon.library access; just having the
        # files in Storage/Monitors lets the user pick them up via
        # Workbench.
        await amiga_makedir(mcpd, dest + "Storage/Monitors")
        for f in ("RTG", "RTG.info"):
            try:
                await mcpd.request("fs.copy", {
                    "src": inst + f,
                    "dst": dest + "Storage/Monitors/" + f,
                })
            except Exception:
                pass

        # First-boot wizard (Post-Install) + its CDPostInstall payload.
        # AOS4.1's wbstartup.prefs launches Storage/Post-Install on
        # first boot to run keymap / locale / sound / network / Extras
        # setup. Without this the user gets a blank first-boot
        # experience.
        storage_dst = dest + "Storage/"
        await amiga_makedir(mcpd, storage_dst)
        for f in ("Post-Install", "Post-Install.info"):
            try:
                await mcpd.request("fs.copy", {
                    "src": inst + f, "dst": storage_dst + f,
                })
            except Exception:
                pass

        # CDPostInstall payload (binary + assets) lives one level up
        # in <ISO>:Installation-Support/. Recursive copy via
        # exec.cmd `Copy ALL CLONE QUIET` because the contents are a
        # whole drawer.
        cdpost_dst = dest + "Storage/CDPostInstall"
        cdpost_src = iso + "Installation-Support/CDPostInstall"
        try:
            # Wipe any leftover CDPostInstall dir from a previous run
            # so the new contents land cleanly.
            await mcpd.request("fs.delete", {
                "path": cdpost_dst, "recursive": True,
            })
        except Exception:
            pass
        await amiga_makedir(mcpd, cdpost_dst)
        try:
            from ...tools import installer as it
            await it.installer_copy_tree(
                ctx["fleet"], ctx["target"],
                src=cdpost_src + "/", dst=cdpost_dst + "/",
                confirm=True, timeout_s=300.0,
            )
        except Exception:
            pass

        # Patch Post-Install: the stock script hardcodes
        # libz.so.1.2.3 (Update 1 era) but Update 3 replaces it
        # with libz.so.1.2.13. Without this the first-boot wizard
        # logs "makelink failed" warnings to RAM:boot_errors.txt.
        # Preserve original mtime around the patch.
        pi_path = storage_dst + "Post-Install"
        try:
            orig_mtime = await _capture_mtime(mcpd, pi_path)
            rd = await mcpd.request("fs.read", {"path": pi_path})
            existing = base64.b64decode(
                (rd or {}).get("content_b64", "")
            ).decode("ascii", errors="replace")
            patched = existing.replace(
                "libz.so.1.2.3", "libz.so.1.2.13"
            )
            if patched != existing:
                await mcpd.request("fs.write", {
                    "path": pi_path,
                    "content_b64": base64.b64encode(
                        patched.encode("ascii")
                    ).decode("ascii"),
                }, timeout_s=30.0)
                await _restore_mtime(mcpd, pi_path, orig_mtime)
        except Exception:
            pass

        envarc = dest + "Prefs/Env-Archive/Sys/"
        await amiga_makedir(mcpd, envarc)
        for f in ("wbpattern.prefs", "wbstartup.prefs",
                  "AmiDock.amiga.com.xml"):
            try:
                await mcpd.request("fs.copy", {
                    "src": inst + f, "dst": envarc + f,
                })
            except Exception:
                pass

        presets = (iso + "System/Prefs/Presets/Default/1024x768.preset/")
        common = iso + "System/Prefs/Presets/Default/Common/"
        for f in ("GUI.prefs", "Font.prefs", "WBPattern.prefs"):
            try:
                await mcpd.request("fs.copy", {
                    "src": presets + f, "dst": envarc + f,
                })
            except Exception:
                pass
        for f in ("Palette.prefs", "startup.prefs"):
            try:
                await mcpd.request("fs.copy", {
                    "src": common + f, "dst": envarc + f,
                })
            except Exception:
                pass

        for f in ("locale.prefs", "input.prefs"):
            try:
                await mcpd.request("fs.copy", {
                    "src": "ENV:Sys/" + f, "dst": envarc + f,
                })
            except Exception:
                pass

        try:
            await mcpd.request("fs.copy", {
                "src": inst + "load-time-blacklist",
                "dst": dest + "Devs/load-time-blacklist",
            })
        except Exception:
            pass

        return {"copied": "installation extras"}
    return Step(
        name="copy_installation_extras",
        doc="Copy S/, Prefs/Env-Archive/Sys/, and locale/input prefs "
            "from <ISO>:Installation-Files/ to dest.",
        fn=fn,
    )


def install_bootloader(config: MachineConfig) -> Step:
    """Copy <ISO>:amigaboot.of (or whatever the config names) to dest.
    No-op for machines in MACHINES_NO_BOOTLOADER_FILE (X5000, A1222)."""
    async def fn(ctx):
        if config.get("skip_bootloader"):
            return {"skipped": (f"{config['machine_id']} boot loader is "
                                "system-wide on a separate SD card")}
        mcpd = ctx["mcpd"]
        src = ctx["iso_volume"] + config["bootloader_filename"]
        dest = ctx["dest_volume"] + config["bootloader_filename"]
        try:
            await mcpd.request("fs.copy", {"src": src, "dst": dest})
            return {"copied": config["bootloader_filename"]}
        except Exception as e:
            return {"warning": f"{src} not on ISO: {e}"}
    return Step(
        name="install_bootloader",
        doc=("Copy bootloader from ISO to dest, or skip for X5000/A1222 "
             "which use a system-wide SD-card bootloader."),
        fn=fn,
    )


# ---- updates ------------------------------------------------------


def apply_one_update(config: MachineConfig, update_num: int) -> Step:
    kick_sub = config.get("kick_sub")
    wb_sub = config.get("wb_sub")
    klr = list(config.get("kicklayout_replacements", []))

    async def fn(ctx):
        from ...tools import installer as it
        mcpd = ctx["mcpd"]
        target = ctx["target"]
        fleet = ctx["fleet"]
        dest = ctx["dest_volume"]

        archive = f"{dest}tmp/{UPDATE_LHA_NAME[update_num]}"
        scratch = f"{dest}tmp/scratch/upd{update_num}"
        await amiga_makedir(mcpd, scratch)

        await it.installer_apply_lha(
            fleet, target,
            archive=archive, dest=scratch,
            confirm=True, timeout_s=900.0,
        )

        content = (f'{scratch}/AmigaOS 4.1 Final Edition Update '
                   f'{update_num}/Content')

        # Update 2 backs up Startup-Sequence + Kicklayout
        if update_num == 2:
            for src_rel, dst_rel in (
                ("S/Startup-Sequence", "S/Startup-Sequence-preUpd2"),
                ("Kickstart/Kicklayout", "Kickstart/Kicklayout-preUpd2"),
            ):
                try:
                    await mcpd.request("fs.copy", {
                        "src": dest + src_rel, "dst": dest + dst_rel,
                    })
                except Exception:
                    pass

        # Generic Workbench + Kickstart subdirs
        for sub, dst_rel in (("Workbench", ""), ("Kickstart", "Kickstart/")):
            try:
                await it.installer_copy_tree(
                    fleet, target,
                    src=content + "/" + sub + "/",
                    dst=dest + dst_rel,
                    confirm=True, timeout_s=600.0,
                )
            except Exception:
                pass

        # Per-machine packages
        if kick_sub:
            try:
                await it.installer_copy_tree(
                    fleet, target,
                    src=f"{content}/{kick_sub}/",
                    dst=dest + "Kickstart/",
                    confirm=True, timeout_s=300.0,
                )
            except Exception:
                pass
        if wb_sub:
            try:
                await it.installer_copy_tree(
                    fleet, target,
                    src=f"{content}/{wb_sub}/",
                    dst=dest,
                    confirm=True, timeout_s=300.0,
                )
            except Exception:
                pass

        # MUI for this update
        mui_sub = MUI_PER_UPDATE.get(update_num)
        if mui_sub:
            try:
                await it.installer_copy_tree(
                    fleet, target,
                    src=f"{content}/{mui_sub}/",
                    dst=dest,
                    confirm=True, timeout_s=300.0,
                )
            except Exception:
                pass

        # Per-machine Kicklayout patches (e.g. X5000 SATA rename in U2)
        if update_num == 2 and klr:
            await it.installer_patch_kicklayout(
                fleet, target, dest_volume=dest,
                replace_text=klr,
                confirm=True, backup_suffix=None,
            )

        # Update 1 — add mounter.library to Kicklayout
        if update_num == 1:
            await it.installer_patch_kicklayout(
                fleet, target, dest_volume=dest,
                add_modules=[{
                    "modules": ["Kickstart/mounter.library"],
                    "label": "Update 1",
                }],
                confirm=True, backup_suffix=None,
            )

        # SObjs symlink fix-ups (mirrors stock installExitHandler).
        # Per-update list of (link_name, target_name) tuples. Each:
        #   delete the existing link (if any), then makelink SOFT.
        sobjs = dest + "SObjs/"
        if update_num == 1:
            symlinks = [
                ("libz.so",          "libz.so.1.2.3"),
                ("libz.so.1",        "libz.so.1.2.3"),
                ("libz.so.1.2",      "libz.so.1.2.3"),
                ("libpng.so",        "libpng12.so"),
                ("libbz2.so",        "libbz2.so.1.0.4"),
                ("libbz2.so.1.0",    "libbz2.so.1.0.4"),
                ("libfontconfig.so", "libfontconfig.so.2.12.1"),
                ("libharfbuzz.so",   "libharfbuzz.so.0.9.28"),
            ]
        elif update_num == 2:
            symlinks = [
                ("libz.so",     "libz.so.1.2.3"),
                ("libz.so.1",   "libz.so.1.2.3"),
                ("libz.so.1.2", "libz.so.1.2.3"),
                ("libpng.so",   "libpng12.so"),
            ]
        else:
            symlinks = []
        for link, link_target in symlinks:
            link_path = sobjs + link
            try:
                await mcpd.request("exec.cmd", {
                    "command": f'Delete *>NIL: QUIET "{link_path}"',
                    "timeout_ms": 10000,
                }, timeout_s=15.0)
            except Exception:
                pass
            try:
                await mcpd.request("exec.cmd", {
                    "command": (f'MakeLink *>NIL: "{link_path}" '
                                f'{link_target} SOFT FORCE'),
                    "timeout_ms": 10000,
                }, timeout_s=15.0)
            except Exception:
                pass

        # Update 3 - additional libz->1.2.13 symlink set when the
        # newer file is present.
        if update_num == 3:
            try:
                await mcpd.request("fs.stat",
                    {"path": sobjs + "libz.so.1.2.13"})
                # libz.so.1.2.13 exists -> redirect generic links
                for old_link in ("libz.so", "libz.so.1", "libz.so.1.2",
                                 "libz.so.1.2.3", "libz.so.1.2.11"):
                    try:
                        await mcpd.request("exec.cmd", {
                            "command": (f'Delete *>NIL: QUIET '
                                        f'"{sobjs}{old_link}"'),
                            "timeout_ms": 10000,
                        }, timeout_s=15.0)
                    except Exception:
                        pass
                for new_link in ("libz.so", "libz.so.1", "libz.so.1.2"):
                    try:
                        await mcpd.request("exec.cmd", {
                            "command": (f'MakeLink *>NIL: '
                                        f'"{sobjs}{new_link}" '
                                        f'libz.so.1.2.13 SOFT FORCE'),
                            "timeout_ms": 10000,
                        }, timeout_s=15.0)
                    except Exception:
                        pass
            except Exception:
                pass  # libz.so.1.2.13 not present yet

        # Old XAD-mangled-name renames (post-each-update): undo the
        # LhA-XAD case where spaces in filenames got mangled to
        # underscores.
        util = dest + "Utilities/"
        await _maybe_rename(mcpd,
                            util + "Installation_Utility",
                            util + "Installation Utility")
        sysd = dest + "System/"
        for new_name, old_name in (
            ("Media_Toolbox",      "Media Toolbox"),
            ("Media_Toolbox.info", "Media Toolbox.info"),
        ):
            await _maybe_rename(mcpd, sysd + new_name, sysd + old_name)
        try:
            await mcpd.request("exec.cmd", {
                "command": f'Delete *>NIL: "{util}CatComp"',
                "timeout_ms": 10000,
            }, timeout_s=15.0)
        except Exception:
            pass

        return {"update": update_num, "scratch": scratch,
                "symlinks_applied": len(symlinks)}

    desc_pkgs = []
    if kick_sub:
        desc_pkgs.append(kick_sub)
    if wb_sub:
        desc_pkgs.append(wb_sub)
    pkg_label = "+".join(desc_pkgs) if desc_pkgs else "(no machine pkgs)"

    return Step(
        name=f"apply_update_{update_num}",
        doc=(f"Extract + apply Update {update_num} "
             f"({UPDATE_LHA_NAME[update_num]}). Per-machine packages: "
             f"{pkg_label}; MUI: {MUI_PER_UPDATE.get(update_num, 'none')}."),
        fn=fn,
    )


# ---- enhancer + a1222 ---------------------------------------------


def install_enhancer(config: MachineConfig) -> Step:
    """Extract Enhancer 2.2 + copy components + Kicklayout patches."""
    eth_driver = config.get("ethernet_driver")

    async def fn(ctx):
        from ...tools import installer as it
        mcpd = ctx["mcpd"]
        target = ctx["target"]
        fleet = ctx["fleet"]
        dest = ctx["dest_volume"]

        scratch = f"{dest}tmp/scratch/enhancer"
        await amiga_makedir(mcpd, scratch)
        await it.installer_apply_lha(
            fleet, target,
            archive=f"{dest}tmp/enhancer_software_2.2.lha",
            dest=scratch,
            confirm=True, timeout_s=600.0,
        )

        enh_root = f"{scratch}/EnhancerSoftware2.2/Installation_Files/"
        dst_kick = dest + "Kickstart/"

        for f in (
            "RadeonHD.chip", "RadeonHD.chip.debug",
            "RadeonRX.chip", "RadeonRX.chip.debug",
            "SmartFilesystem", "diskcache.library.kmod",
        ):
            try:
                await mcpd.request("fs.copy", {
                    "src": f"{enh_root}Kickstart/{f}",
                    "dst": dst_kick + f,
                })
            except Exception:
                pass

        if eth_driver:
            net_dst = dest + "Devs/Networks/"
            await amiga_makedir(mcpd, net_dst)
            try:
                await mcpd.request("fs.copy", {
                    "src": f"{enh_root}Devs/Networks/{eth_driver}",
                    "dst": net_dst + eth_driver,
                })
            except Exception:
                pass

        extras = (
            ("Firmware/amdgpu/polaris10_uvd.bin", "Firmware/amdgpu/"),
            ("Firmware/amdgpu/polaris11_uvd.bin", "Firmware/amdgpu/"),
            ("Firmware/amdgpu/polaris12_uvd.bin", "Firmware/amdgpu/"),
            ("Firmware/radeon/TAHITI_uvd.bin", "Firmware/radeon/"),
            ("Libs/va.library", "Libs/"),
            ("Libs/VA/RadeonHD_drv_video.library", "Libs/VA/"),
            ("Libs/VA/RadeonRX_drv_video.library", "Libs/VA/"),
            ("Libs/Warp3DNova.library", "Libs/"),
            ("Libs/Warp3DNova/W3DN_GCN.library", "Libs/Warp3DNova/"),
            ("Libs/Warp3DNova/W3DN_SI.library", "Libs/Warp3DNova/"),
            ("Libs/sensormaster.library", "Libs/"),
            ("Libs/hm_sensors/ct-mcu.library", "Libs/hm_sensors/"),
        )
        for src_rel, dst_rel in extras:
            await amiga_makedir(mcpd, dest + dst_rel)
            try:
                fname = src_rel.rsplit("/", 1)[-1]
                await mcpd.request("fs.copy", {
                    "src": enh_root + src_rel,
                    "dst": dest + dst_rel + fname,
                })
            except Exception:
                pass

        await it.installer_patch_kicklayout(
            fleet, target, dest_volume=dest,
            add_modules=[{
                "modules": [
                    "Kickstart/SmartFilesystem",
                    "Kickstart/RadeonRX.chip",
                    "Kickstart/RadeonHD.chip",
                    "Kickstart/diskcache.library.kmod",
                ],
                "label": "Enhancer 2.2",
            }],
            confirm=True, backup_suffix=None,
        )
        return {"enhancer": "installed"}

    eth_label = eth_driver if eth_driver else "none"
    return Step(
        name="install_enhancer",
        doc=("Extract Enhancer 2.2 LHA, copy RadeonHD/RX/SFS/diskcache "
             f"to Kickstart/, eth driver: {eth_label}, GPU firmware + "
             "Warp3DNova libs, then patch Kicklayout."),
        fn=fn,
    )


def install_ngf() -> Step:
    """X5000-only: install NGFileSystem + NGFCheck."""
    async def fn(ctx):
        from ...tools import installer as it
        mcpd = ctx["mcpd"]
        target = ctx["target"]
        fleet = ctx["fleet"]
        dest = ctx["dest_volume"]

        ngfs_dir = f"{dest}tmp/scratch/ngfs"
        ngfck_dir = f"{dest}tmp/scratch/ngfcheck"
        await amiga_makedir(mcpd, ngfs_dir)
        await amiga_makedir(mcpd, ngfck_dir)
        await it.installer_apply_lha(
            fleet, target,
            archive=f"{dest}tmp/NGFS.lha", dest=ngfs_dir,
            confirm=True, timeout_s=300.0,
        )
        await it.installer_apply_lha(
            fleet, target,
            archive=f"{dest}tmp/NGFCheck.lha", dest=ngfck_dir,
            confirm=True, timeout_s=300.0,
        )

        try:
            await mcpd.request("fs.copy", {
                "src": ngfs_dir + "/NGFileSystem",
                "dst": dest + "Kickstart/NGFileSystem",
            })
        except Exception:
            pass
        for f in ("NGFCheck", "NGFCheck.info"):
            try:
                await mcpd.request("fs.copy", {
                    "src": f"{ngfck_dir}/{f}",
                    "dst": f"{dest}C/{f}",
                })
            except Exception:
                pass
        return {"ngf": "installed"}
    return Step(
        name="install_ngf",
        doc="Extract NGFS.lha and NGFCheck.lha, copy NGFileSystem to "
            "Kickstart/, NGFCheck + .info to C/.",
        fn=fn,
    )


def unmount_iso() -> Step:
    async def fn(ctx):
        from ...tools import installer as it
        res = await it.installer_unmount_iso(
            ctx["fleet"], ctx["target"], unit=COMBI_UNIT,
        )
        return res.model_dump()
    return Step(
        name="unmount_iso",
        doc="Eject the install ISO from the COMBI: device.",
        fn=fn,
    )


def install_extras() -> Step:
    """Pre-install Filer, Ranger, IBrowse, DiskImage from
    `<ISO>:Installation-Files/Extras/`.

    Each extras drawer has a layout: dest dir + sibling .info icon
    so Workbench shows the drawer with the right icon. DiskImage
    overlays onto dest root (no separate drawer icon).
    Idempotent — re-running overwrites and is benign.
    """
    # (drawer-on-CD, target-drawer-on-dest, target-icon-rel-or-None)
    EXTRAS = (
        ("Filer",     "Utilities/Filer",   "Utilities/Filer.info"),
        ("Ranger",    "Utilities/Ranger",  "Utilities/Ranger.info"),
        ("IBrowse",   "Internet/IBrowse",  "Internet/IBrowse.info"),
        # DiskImage drawer overlays System/, C/, Devs/, Storage/ etc
        # onto matching system dirs - including System/DiskImageGUI -
        # so target is dest root and there's no separate drawer icon.
        ("DiskImage", "",                   None),
    )

    async def fn(ctx):
        from ...tools import installer as it
        mcpd = ctx["mcpd"]
        iso = ctx["iso_volume"]
        dest = ctx["dest_volume"]
        extras_root = iso + "Installation-Files/Extras/"

        results: list[dict] = []
        for ex_name, dst_rel, icon_rel in EXTRAS:
            src = f"{extras_root}{ex_name}/"
            try:
                await mcpd.request("fs.stat",
                    {"path": f"{extras_root}{ex_name}"})
            except Exception:
                results.append({"extra": ex_name, "skipped": "not on ISO"})
                continue

            if dst_rel:
                dst = dest + dst_rel + "/"
                await amiga_makedir(mcpd, dst)
            else:
                dst = dest

            try:
                await it.installer_copy_tree(
                    ctx["fleet"], ctx["target"],
                    src=src, dst=dst,
                    confirm=True, timeout_s=300.0,
                )
                results.append({"extra": ex_name, "copied_to": dst})
            except Exception as e:
                results.append({"extra": ex_name, "error": str(e)[:100]})
                continue

            # Strip Install* scripts the stock GUI installer wouldn't
            # have copied. exec.cmd Delete with pattern, ignore errors.
            try:
                await mcpd.request("exec.cmd", {
                    "command": f'Delete "{dst}Install#?" QUIET',
                    "timeout_ms": 30000,
                }, timeout_s=35.0)
            except Exception:
                pass

            if icon_rel:
                icon_src = f"{extras_root}{ex_name}.info"
                icon_dst = dest + icon_rel
                try:
                    await mcpd.request("fs.copy", {
                        "src": icon_src, "dst": icon_dst,
                    })
                except Exception:
                    pass

        return {"results": results}
    return Step(
        name="install_extras",
        doc=("Pre-install Filer, Ranger, IBrowse, DiskImage from "
             "<ISO>:Installation-Files/Extras/ so they appear in "
             "the new install's Utilities/Internet drawers."),
        fn=fn,
    )


def patch_amidock_prefs() -> Step:
    """Overwrite `<dest>:Prefs/Env-Archive/Sys/AmiDock.amiga.com.xml`
    with the bundled prepared XML (staged into `<dest>:tmp/`).

    The bundled XML replaces the stock dock's Extras-Installer entry
    with SubDock pointers + an Extras subdock for Filer/Ranger/
    DiskImageGUI + an Internet subdock for IBrowse.

    Skips silently if the bundled file isn't staged.
    """
    async def fn(ctx):
        mcpd = ctx["mcpd"]
        dest = ctx["dest_volume"]
        src = f"{dest}tmp/AmiDock.amiga.com.xml"
        dst = f"{dest}Prefs/Env-Archive/Sys/AmiDock.amiga.com.xml"

        try:
            await mcpd.request("fs.stat", {"path": src})
        except Exception:
            return {"action": "skipped",
                    "reason": "AmiDock.amiga.com.xml not staged"}

        await amiga_makedir(mcpd, dest + "Prefs/Env-Archive/Sys")
        try:
            await mcpd.request("fs.copy", {"src": src, "dst": dst})
        except Exception:
            # Fallback via exec.cmd Copy
            await mcpd.request("exec.cmd", {
                "command": f'Copy "{src}" "{dst}" CLONE',
                "timeout_ms": 30000,
            }, timeout_s=35.0)
        return {"action": "installed",
                "from": src, "to": dst}
    return Step(
        name="patch_amidock_prefs",
        doc=("Overlay AmiDock.amiga.com.xml so the dock points at "
             "Filer / Ranger / IBrowse / DiskImageGUI in the new "
             "install's Extras / Internet subdocks."),
        fn=fn,
    )


def install_network_config(machine_id: str) -> Step:
    """Write `<dest>:S/Network-Startup` + per-machine
    `<dest>:Devs/NetInterfaces/<iface>`.

    Per-machine ethernet device file:
      X5000 → P50X0_ETH (p50x0_eth.device)
      A1222 → eth0 (A1222eth.device)
      others → no built-in eth, user configures via Prefs/Network
    """
    NET_INTERFACE = {
        "X5000": ("P50X0_ETH",
                  "# DEVS:NetInterfaces/P50X0_ETH\n"
                  "# File generated by amiga-fleet-mcp installer\n"
                  "device=p50x0_eth.device\n"
                  "unit=0\n"
                  "configure=dhcp\n"),
        "A1222": ("eth0",
                  "# DEVS:NetInterfaces/eth0\n"
                  "# File generated by amiga-fleet-mcp installer\n"
                  "device=A1222eth.device\n"
                  "hardwaretype=Ethernet\n"
                  "configure=dhcp\n"
                  "mtu=0\n"),
    }
    NETWORK_STARTUP = (
        "; $VER: Network-Startup 53.2 (01.06.2011)\n"
        "\n"
        "AddNetInterface QUIET DEVS:NetInterfaces/~(#?.info)\n"
        "\n"
        "; Add below this line applications that need a running network\n"
    )

    async def fn(ctx):
        import base64
        mcpd = ctx["mcpd"]
        dest = ctx["dest_volume"]

        # 1. baseline Network-Startup
        await amiga_makedir(mcpd, dest + "S")
        await mcpd.request("fs.write", {
            "path": dest + "S/Network-Startup",
            "content_b64": base64.b64encode(
                NETWORK_STARTUP.encode("ascii")
            ).decode("ascii"),
        }, timeout_s=30.0)

        # 2. per-machine NetInterface file
        iface = NET_INTERFACE.get(machine_id)
        if iface is None:
            return {"action": "network-startup written; no NetInterface",
                    "reason": f"no canonical NetInterface for {machine_id}"}
        ifname, content = iface
        ni_dir = dest + "Devs/NetInterfaces"
        await amiga_makedir(mcpd, ni_dir)
        ni_path = f"{ni_dir}/{ifname}"
        await mcpd.request("fs.write", {
            "path": ni_path,
            "content_b64": base64.b64encode(
                content.encode("ascii")
            ).decode("ascii"),
        }, timeout_s=30.0)

        # Workbench .info icon for the NetInterface so it appears in
        # Prefs/Network's GUI (as if it were generated by Dialer).
        # Without it the interface still loads at boot via
        # AddNetInterface, but is invisible in the GUI.
        #
        # Lookup chain (first hit wins):
        #   1. <dest>:tmp/NetInterface.info (staged template, if any)
        #   2. <ISO>:Installation-Files/NetInterface.info
        #   3. <dest>:Devs/NetInterfaces/*.info  (a sibling icon
        #      from a pre-existing interface, e.g. one Prefs/Dialer
        #      already wrote)
        # If none found, skip silently — config still works.
        icon_dst = ni_path + ".info"
        icon_sources = [
            f"{dest}tmp/NetInterface.info",
            ctx.get("iso_volume", "") + "Installation-Files/NetInterface.info",
        ]
        icon_copied_from: str | None = None
        for src in icon_sources:
            if not src:
                continue
            try:
                await mcpd.request("fs.stat", {"path": src})
            except Exception:
                continue
            try:
                await mcpd.request("fs.copy",
                    {"src": src, "dst": icon_dst})
                icon_copied_from = src
                break
            except Exception:
                continue

        if icon_copied_from is None:
            # Fall back: scan existing NetInterfaces dir for any .info
            # we can clone (Dialer-generated icons live here).
            try:
                listing = await mcpd.request("fs.list", {"path": ni_dir})
                entries = listing if isinstance(listing, list) else (
                    listing.get("entries", [])
                    if isinstance(listing, dict) else []
                )
                for e in entries:
                    if e.get("type") != "file":
                        continue
                    name = e.get("name", "")
                    # skip our own newly-written files
                    if not name.endswith(".info"):
                        continue
                    if name == f"{ifname}.info":
                        continue  # already exists
                    src = f"{ni_dir}/{name}"
                    try:
                        await mcpd.request("fs.copy",
                            {"src": src, "dst": icon_dst})
                        icon_copied_from = src
                        break
                    except Exception:
                        continue
            except Exception:
                pass

        return {"action": "installed",
                "machine": machine_id,
                "interface": ifname,
                "interface_path": ni_path,
                "icon_copied_from": icon_copied_from}
    iface_descr = (
        f"iface {NET_INTERFACE[machine_id][0]}"
        if machine_id in NET_INTERFACE else "none"
    )
    return Step(
        name="install_network_config",
        doc=(f"Write S:Network-Startup + per-machine NetInterface "
             f"({machine_id}: {iface_descr})."),
        fn=fn,
    )


def install_serialshell() -> Step:
    """Install SerialShell from `<dest>:tmp/SerialShell` so it
    auto-starts on first boot.

    Steps:
      1. Copy <dest>:tmp/SerialShell -> <dest>:C/SerialShell, +rwed
      2. Write <dest>:S/SerialShell-Startup containing `C:SerialShell`
      3. Append `;BEGIN SerialShell ... ;END SerialShell` block to
         <dest>:S/User-Startup if not already present (idempotent)

    Skips silently if `<dest>:tmp/SerialShell` isn't staged.
    """
    async def fn(ctx):
        import base64
        mcpd = ctx["mcpd"]
        dest = ctx["dest_volume"]
        binary_src = dest + "tmp/SerialShell"
        binary_dst = dest + "C/SerialShell"

        try:
            await mcpd.request("fs.stat", {"path": binary_src})
        except Exception:
            return {"action": "skipped",
                    "reason": "SerialShell binary not staged at tmp/"}

        await amiga_makedir(mcpd, dest + "C")
        try:
            await mcpd.request("fs.copy", {
                "src": binary_src, "dst": binary_dst,
            })
        except Exception:
            await mcpd.request("exec.cmd", {
                "command": f'Copy "{binary_src}" "{binary_dst}" CLONE',
                "timeout_ms": 30000,
            }, timeout_s=35.0)
        await mcpd.request("exec.cmd", {
            "command": f'Protect "{binary_dst}" +rwed',
            "timeout_ms": 30000,
        }, timeout_s=35.0)

        # S:SerialShell-Startup
        startup_path = dest + "S/SerialShell-Startup"
        await amiga_makedir(mcpd, dest + "S")
        await mcpd.request("fs.write", {
            "path": startup_path,
            "content_b64": base64.b64encode(
                b"C:SerialShell\n"
            ).decode("ascii"),
        }, timeout_s=30.0)

        # S:User-Startup append (idempotent: skip if marker present).
        # Capture mtime first so the rewrite preserves the file's
        # original date (it's from copy_base_os = ISO).
        user_startup_path = dest + "S/User-Startup"
        orig_us_mtime = await _capture_mtime(mcpd, user_startup_path)
        try:
            raw = await mcpd.request("fs.read",
                {"path": user_startup_path})
            existing = base64.b64decode(
                raw.get("content_b64", "")
            ).decode("ascii", errors="replace")
        except Exception:
            existing = ""

        marker = ";BEGIN SerialShell"
        if marker in existing:
            return {"action": "binary copied; User-Startup unchanged",
                    "binary": binary_dst,
                    "marker_already_present": True}

        block = (
            "\n"
            ";BEGIN SerialShell\n"
            'NewShell "CON:0/400/640/200/SerialShell/AUTO/CLOSE"'
            ' FROM S:SerialShell-Startup\n'
            ";END SerialShell\n"
        )
        new_us = existing
        if new_us and not new_us.endswith("\n"):
            new_us += "\n"
        new_us += block

        await mcpd.request("fs.write", {
            "path": user_startup_path,
            "content_b64": base64.b64encode(
                new_us.encode("ascii")
            ).decode("ascii"),
        }, timeout_s=30.0)
        await _restore_mtime(mcpd, user_startup_path, orig_us_mtime)
        return {"action": "installed",
                "binary": binary_dst,
                "user_startup": user_startup_path}
    return Step(
        name="install_serialshell",
        doc=("Install SerialShell binary to C:, write "
             "S:SerialShell-Startup, and append a NewShell auto-start "
             "block to S:User-Startup."),
        fn=fn,
    )


def install_mcpd() -> Step:
    """Install MCPd onto the destination so it auto-starts on first
    boot. Universal step (every AOS4 install gets it).

    Mirrors `mcpd/install/MCPd-Install`:
      1. Copy <dest>:tmp/MCPd -> <dest>:System/MCPd/MCPd, +rwed.
      2. If <dest>:S/Network-Startup doesn't already reference MCPd:
         - Backup to S/Network-Startup.before-mcpd (idempotent).
         - Append `Run >NIL: <NIL: SYS:System/MCPd/MCPd` line.

    Idempotent — re-running on a partially-installed dest is safe.
    Skips quietly if <dest>:tmp/MCPd is missing (caller should have
    staged it via installer.stage).
    """
    async def fn(ctx):
        import base64

        from .._amiga_fs import amiga_makedir
        mcpd = ctx["mcpd"]
        dest = ctx["dest_volume"]
        binary_src = dest + "tmp/MCPd"
        binary_dst = dest + "System/MCPd/MCPd"
        netstart = dest + "S/Network-Startup"

        # Sanity: the binary must have been staged.
        try:
            await mcpd.request("fs.stat", {"path": binary_src})
        except Exception:
            return {"action": "skipped",
                    "reason": "MCPd binary not staged at tmp/MCPd"}

        # 1. copy + protect
        await amiga_makedir(mcpd, dest + "System/MCPd")
        try:
            await mcpd.request("fs.copy", {
                "src": binary_src, "dst": binary_dst,
            })
        except Exception:
            # Fallback via exec.cmd Copy CLONE
            await mcpd.request("exec.cmd", {
                "command": f'Copy "{binary_src}" "{binary_dst}" CLONE',
                "timeout_ms": 60000,
            }, timeout_s=70.0)
        await mcpd.request("exec.cmd", {
            "command": f'Protect "{binary_dst}" +rwed',
            "timeout_ms": 30000,
        }, timeout_s=35.0)

        # 2. Network-Startup: read, check for existing MCPd line, edit.
        # Capture mtime first so we can restore it after our append.
        orig_netstart_mtime = await _capture_mtime(mcpd, netstart)
        try:
            raw = await mcpd.request("fs.read", {"path": netstart})
            existing = base64.b64decode(
                raw.get("content_b64", "")
            ).decode("ascii", errors="replace")
        except Exception:
            existing = ""

        marker = "SYS:System/MCPd/MCPd"
        if marker in existing:
            return {"action": "binary copied; Network-Startup unchanged",
                    "binary": binary_dst,
                    "marker_already_present": True}

        # backup
        backup = netstart + ".before-mcpd"
        try:
            await mcpd.request("fs.stat", {"path": backup})
            backed_up = False  # backup already exists
        except Exception:
            try:
                await mcpd.request("fs.copy", {
                    "src": netstart, "dst": backup,
                })
                backed_up = True
            except Exception:
                # Network-Startup might not exist on the dest yet
                # (clean partition, no base OS Network-Startup file).
                # Treat as "create new file with just our line" below.
                backed_up = False
                existing = ""

        # append our line
        new_text = existing
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        new_text += "\n; MCPd - Model Context Protocol daemon\n"
        new_text += f"Run >NIL: <NIL: {marker}\n"

        await mcpd.request("fs.write", {
            "path": netstart,
            "content_b64": base64.b64encode(
                new_text.encode("ascii")
            ).decode("ascii"),
        }, timeout_s=60.0)
        # Restore the captured mtime so the file doesn't show as
        # "today" in Workbench post-install.
        await _restore_mtime(mcpd, netstart, orig_netstart_mtime)
        return {
            "action": "installed",
            "binary": binary_dst,
            "network_startup": netstart,
            "backed_up_to": backup if backed_up else None,
        }
    return Step(
        name="install_mcpd",
        doc=("Copy MCPd binary to SYS:System/MCPd/, +rwed, append "
             "launch line to S:Network-Startup so it auto-starts on "
             "first boot."),
        fn=fn,
    )


def quarantine_devs_monitors() -> Step:
    """Move every entry from `<dest>:DEVS/Monitors/` to
    `<dest>:Storage/Monitors/`. Mirrors the pattern Amiga power-users
    use to keep DEVS:Monitors/ empty so Workbench doesn't auto-load
    legacy monitor drivers — RadeonHD/RX (Enhancer 2.2) handles RTG
    natively, and the legacy monitors clutter Screens-mode lists +
    can crash on hardware they don't understand.

    Idempotent — entries already at Storage/Monitors are silently
    overwritten; missing source dir is a no-op.
    """
    async def fn(ctx):
        mcpd = ctx["mcpd"]
        dest = ctx["dest_volume"]
        src_dir = dest + "DEVS/Monitors"
        dst_dir = dest + "Storage/Monitors"

        # Source dir present?
        try:
            listing = await mcpd.request("fs.list", {"path": src_dir})
        except Exception:
            return {"action": "skipped",
                    "reason": "DEVS:Monitors not present"}
        # fs.list returns a list directly, not a dict.
        if isinstance(listing, list):
            entries = listing
        else:
            entries = (listing or {}).get("entries", [])
        if not entries:
            return {"action": "skipped",
                    "reason": "DEVS:Monitors is already empty"}

        await amiga_makedir(mcpd, dst_dir)

        moved: list[str] = []
        errors: list[dict] = []
        for entry in entries:
            name = entry.get("name", "")
            if not name:
                continue
            src_path = f"{src_dir}/{name}"
            dst_path = f"{dst_dir}/{name}"
            # Try fs.rename first (atomic, single round-trip).
            try:
                await mcpd.request("fs.rename", {
                    "src": src_path, "dst": dst_path,
                })
                moved.append(name)
                continue
            except Exception:
                pass
            # Fallback: copy + delete via fs primitives.
            try:
                await mcpd.request("fs.copy", {
                    "src": src_path, "dst": dst_path,
                })
                await mcpd.request("fs.delete", {
                    "path": src_path, "recursive": True,
                })
                moved.append(name)
            except Exception as e:
                errors.append({"name": name, "error": str(e)[:200]})

        return {"action": "moved",
                "from": src_dir,
                "to": dst_dir,
                "moved": moved,
                "errors": errors}
    return Step(
        name="quarantine_devs_monitors",
        doc=("Move all entries from DEVS:Monitors to "
             "Storage/Monitors -- keeps Workbench from auto-loading "
             "legacy monitor drivers when RadeonHD/RX handles RTG."),
        fn=fn,
    )


def quarantine_emu10kx() -> Step:
    """X5000 (and any board without a real SoundBlaster Live):
    `DEVS:AHI/emu10kx.audio` (the 2014 driver) crashes when AHI
    probes for non-existent SBLive hardware. Move it out of the
    AHI auto-load path into Storage/AHI/ so it's available if real
    SBLive ever shows up but doesn't get loaded by default.

    Mirrors the manual fix every X5000 user has to apply post-install.
    Memory: reference_emu10kx_x5000.md.

    Idempotent — no-op if the file is already moved or absent.
    """
    async def fn(ctx):
        mcpd = ctx["mcpd"]
        dest = ctx["dest_volume"]
        src_path = dest + "DEVS/AHI/emu10kx.audio"
        dst_path = dest + "Storage/AHI/emu10kx.audio"

        # Source not present? Nothing to do.
        try:
            await mcpd.request("fs.stat", {"path": src_path})
        except Exception:
            return {"action": "skipped",
                    "reason": "DEVS:AHI/emu10kx.audio not present"}

        # Make sure Storage/AHI/ exists.
        from .._amiga_fs import amiga_makedir
        await amiga_makedir(mcpd, dest + "Storage/AHI")

        # Move via fs.rename (single round-trip; atomic on the FS).
        try:
            await mcpd.request("fs.rename", {
                "src": src_path, "dst": dst_path,
            })
            return {"action": "moved",
                    "from": src_path, "to": dst_path}
        except Exception as e:
            # Fallback: copy + delete via fs.copy + fs.delete.
            try:
                await mcpd.request("fs.copy", {
                    "src": src_path, "dst": dst_path,
                })
                await mcpd.request("fs.delete", {
                    "path": src_path, "recursive": False,
                })
                return {"action": "moved (copy+delete)",
                        "from": src_path, "to": dst_path}
            except Exception as e2:
                return {"action": "failed",
                        "from": src_path, "to": dst_path,
                        "error": f"rename: {e}; copy+delete: {e2}"}
    return Step(
        name="quarantine_emu10kx",
        doc=("Move DEVS:AHI/emu10kx.audio to Storage/AHI/ -- the "
             "2014 SBLive driver bus-errors during AHI probe on "
             "X5000 hardware that has no SoundBlaster Live."),
        fn=fn,
    )


def cleanup_tmp() -> Step:
    """End-of-install tidy:
       - delete <dest>:tmp/scratch/ recursively (always)
       - delete the bare .iso if it was extracted from a .lha during
         this run (avoids keeping both forms in tmp/, ~1 GiB combined).

    The rest of <dest>:tmp/ is kept on purpose so the user has a
    self-contained re-installable bundle on the new drive.
    """
    async def fn(ctx):
        mcpd = ctx["mcpd"]
        dest = ctx["dest_volume"]

        out: dict = {"deleted": [], "skipped": [], "errors": []}

        # 1. scratch dir (LHA-extraction working directory)
        scratch = f"{dest}tmp/scratch"
        try:
            await mcpd.request("fs.stat", {"path": scratch})
            try:
                await mcpd.request("fs.delete", {
                    "path": scratch, "recursive": True,
                })
                out["deleted"].append(scratch)
            except Exception as e:
                out["errors"].append({"path": scratch, "error": str(e)[:200]})
        except Exception:
            out["skipped"].append(f"{scratch} (not present)")

        # 2. extracted ISO (only if extract_iso_lha set the flag)
        if ctx.get("iso_was_extracted_from_lha"):
            iso_path = f"{dest}tmp/{ctx['iso_filename']}"
            try:
                await mcpd.request("fs.delete", {
                    "path": iso_path, "recursive": False,
                })
                out["deleted"].append(iso_path)
            except Exception as e:
                out["errors"].append({"path": iso_path, "error": str(e)[:200]})
        else:
            out["skipped"].append("iso file (was uploaded directly, kept)")

        return out
    return Step(
        name="cleanup_tmp",
        doc=("Delete <dest>:tmp/scratch/ + the extracted .iso (only "
             "if it was unpacked from .lha during this run). The rest "
             "of <dest>:tmp/ is kept on purpose so re-installs work "
             "from local files."),
        fn=fn,
    )
