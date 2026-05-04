"""Per-machine required-files manifest + host-side source-dir scan.

Builds the list of files we expect to find under a sources directory
before an install can run.
"""

from __future__ import annotations

from pathlib import Path

from .machines import (
    ISO_PREFIX_TO_MACHINE,
    SUPPORTED_MACHINES,
    UPDATE_LHA_NAME,
    UPDATES_TO_APPLY,
    detect_machine_from_iso,
    is_unsupported_iso,
)


def required_files(machine: str) -> list[dict[str, str]]:
    """Return the list of files we must find in `sources_dir` for an
    install of `machine`. Each entry has:

      role     — "iso" / "update" / "enhancer" / "extra"
      filename — exact filename to look for (or, for "iso", a prefix
                 pattern; the actual ISO is matched by prefix)

    For A1222 the "enhancer" role is replaced by an external RescueV2
    USB volume — see machines.A1222_BACKUP_VOLS — so it's not in the
    file manifest. Callers who need that should check separately.
    """
    if machine not in SUPPORTED_MACHINES:
        raise ValueError(
            f"unknown machine {machine!r}; "
            f"supported: {', '.join(SUPPORTED_MACHINES)}"
        )

    out: list[dict[str, str]] = []
    # ISO is matched by prefix, not exact filename. We expose the
    # prefix so callers can scan for a matching file.
    iso_prefix = next(
        prefix for prefix, info in ISO_PREFIX_TO_MACHINE if info[0] == machine
    )
    out.append({
        "role": "iso",
        "filename": f"{iso_prefix}*.iso",
        "iso_prefix": iso_prefix,
    })

    for n in UPDATES_TO_APPLY.get(machine, []):
        out.append({
            "role": "update",
            "filename": UPDATE_LHA_NAME[n],
            "update_index": str(n),
        })

    # A1222 used to require RescueV2 USB instead of an enhancer LHA;
    # the simplified pipeline (2026-05-02) drops that special case
    # and uses the standard Enhancer LHA for every machine.
    out.append({
        "role": "enhancer",
        "filename": "enhancer_software_2.2.lha",
    })

    if machine == "X5000":
        out.append({"role": "extra", "filename": "NGFCheck.lha"})
        out.append({"role": "extra", "filename": "NGFS.lha"})

    return out


def scan_sources(sources_dir: str | Path) -> dict:
    """Walk a host-side sources directory and report what's there.

    Returns a dict with:
      sources_dir  — absolute path of the input
      exists       — does the directory exist?
      iso_files    — dict[machine_id, [filenames]] of detected raw ISOs
      iso_lha_files — dict[machine_id, [filenames]] of detected
                      `<iso>.iso.lha` archives (LHA-of-ISO Layout A
                      from Hyperion). Either form is sufficient for
                      install — installer.stage uses LHA when present
                      to save bandwidth.
      unsupported_isos — list of ISOs matching known-unsupported prefixes
      lha_files    — list of *.lha files present (filename only)
      detected_machine — machine_id if exactly one supported ISO
                         (in EITHER form) found, else None
      ambiguous_machines — list[machine_id] if multiple supported ISOs

    All filenames are relative to sources_dir. No I/O on the file
    contents — only directory listing.
    """
    p = Path(sources_dir).expanduser().resolve()
    out: dict = {
        "sources_dir": str(p),
        "exists": p.exists() and p.is_dir(),
        "iso_files": {},
        "iso_lha_files": {},
        "unsupported_isos": [],
        "lha_files": [],
        "detected_machine": None,
        "ambiguous_machines": [],
    }
    if not out["exists"]:
        return out

    iso_files: dict[str, list[str]] = {}
    iso_lha_files: dict[str, list[str]] = {}
    unsupported: list[str] = []
    lha_files: list[str] = []

    for entry in sorted(p.iterdir()):
        if not entry.is_file():
            continue
        name = entry.name
        lower = name.lower()
        if lower.endswith(".iso"):
            info = detect_machine_from_iso(name)
            if info is not None:
                machine_id = info[0]
                iso_files.setdefault(machine_id, []).append(name)
            elif is_unsupported_iso(name):
                unsupported.append(name)
        elif lower.endswith(".iso.lha"):
            # Hyperion-style: <iso_filename>.lha containing the .iso.
            iso_basename = name[:-len(".lha")]
            info = detect_machine_from_iso(iso_basename)
            if info is not None:
                machine_id = info[0]
                iso_lha_files.setdefault(machine_id, []).append(name)
            else:
                lha_files.append(name)
        elif lower.endswith(".lha"):
            lha_files.append(name)

    out["iso_files"] = iso_files
    out["iso_lha_files"] = iso_lha_files
    out["unsupported_isos"] = unsupported
    out["lha_files"] = lha_files

    # detected_machine: machine present in either iso_files or
    # iso_lha_files exactly once.
    seen = set(iso_files) | set(iso_lha_files)
    if len(seen) == 1:
        out["detected_machine"] = next(iter(seen))
    elif len(seen) > 1:
        out["ambiguous_machines"] = sorted(seen)

    return out
