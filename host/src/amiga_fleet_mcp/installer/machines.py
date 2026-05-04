"""Per-machine static data.

ISO-prefix matching, machine aliases, CD-version signatures, the
list of updates to apply per machine, etc. The values pinned here
are exercised by tests/unit/test_installer_machines.py.
"""

from __future__ import annotations

# Filename prefix -> (machine_id, friendly_name).
# Order matters: longer/more-specific prefixes first so X5000 / A1222
# beat AmigaOne.
#
# X5000 official ISO is `AmigaOneX5000InstallCD-<rev>.iso` (Hyperion
# release naming). The "New..." variant once listed here was an
# unofficial rename / mislabel and never shipped officially.
ISO_PREFIX_TO_MACHINE: list[tuple[str, tuple[str, str]]] = [
    ("AmigaOneX5000InstallCD",    ("X5000",     "AmigaOne X5000")),
    ("AmigaOneA1222InstallCD",    ("A1222",     "AmigaOne A1222")),
    ("AmigaOneInstallCD",         ("AmigaOne",  "AmigaOne (XE/SE)")),
    ("Pegasos2InstallCD",         ("PegasosII", "Pegasos II")),
    ("Sam460InstallCD",           ("Sam460",    "Sam460ex")),
]

UNSUPPORTED_ISO_PREFIXES: tuple[str, ...] = (
    "ClassicInstallCD",
    "Sam440InstallCD",
    "AmigaOneX1000InstallCD",
)

# Canonical machine_ids extracted in the order the user is most likely
# to encounter them.
SUPPORTED_MACHINES: tuple[str, ...] = (
    "X5000", "A1222", "AmigaOne", "PegasosII", "Sam460",
)


# User-typed MACHINE= alias -> canonical machine_id. Lookup is
# case-insensitive (we lowercase keys before lookup). The canonical
# short forms are also valid.
MACHINE_ALIASES: dict[str, str] = {
    "x5000":          "X5000",
    "amigaonex5000":  "X5000",
    "amigaone x5000": "X5000",
    "a1222":          "A1222",
    "amigaonea1222":  "A1222",
    "amigaone a1222": "A1222",
    "sam460":         "Sam460",
    "sam460ex":       "Sam460",
    "pegasos2":       "PegasosII",
    "pegasosii":      "PegasosII",
    "pegasos ii":     "PegasosII",
    "peg2":           "PegasosII",
    "amigaone":       "AmigaOne",
    "amigaonexe":     "AmigaOne",
    "amigaone xe":    "AmigaOne",
    "a1xe":           "AmigaOne",
    "a1se":           "AmigaOne",
}


# Substring expected in <ISO>:CD-Version.txt for each machine.
# Sam460's ISO ships without CD-Version.txt so it has no signature.
CD_VERSION_SIGNATURES: dict[str, str] = {
    "AmigaOne":  "AmigaOS-AmigaOne",
    "X5000":     "AmigaOS-X5000",
    "PegasosII": "AmigaOS-Pegasos2",
    "A1222":     "AmigaOS-A1222",
}


# Which Update LHAs to apply per machine. Other updates are skipped
# because their content is already baked into the install ISO at a
# newer revision.
UPDATES_TO_APPLY: dict[str, list[int]] = {
    "AmigaOne":  [1, 2, 3],
    "PegasosII": [1, 2, 3],
    "Sam460":    [1, 2, 3],
    "X5000":     [2, 3],
    "A1222":     [3],
}


UPDATE_LHA_NAME: dict[int, str] = {
    1: "AmigaOS4.1FinalEditionUpdate1.lha",
    2: "AmigaOS4.1FinalEditionUpdate2-53.14.lha",
    3: "AmigaOS4.1FinalEditionUpdate3-53.34.lha",
}


# Machines whose boot loader image lives on a separate SD card written
# via UpdateAmigaBoot (system-wide, not per-partition). The installer
# never touches that.
MACHINES_NO_BOOTLOADER_FILE: tuple[str, ...] = ("X5000", "A1222")


# A1222 sources its Enhancer-equivalent files from the user's RescueV2
# USB drive rather than from a packed LHA. Two volume names are common.
A1222_BACKUP_VOLS: tuple[str, ...] = ("RESCUEV2:", "AAATECH0:")
A1222_BACKUP_SIGNATURE: str = "Devs/Networks/A1222eth.device"


# Refuse to install if any of these already exist on the destination
# volume — they indicate a previous AOS install we'd clobber.
FORBIDDEN_DEST_ENTRIES: tuple[str, ...] = (
    "S/Startup-Sequence",
    "System",
    "Kickstart",
    "Devs/Monitors",
    "Prefs/Env-Archive",
)


def resolve_machine_alias(name: str) -> str | None:
    """Resolve a user-typed machine name to a canonical machine_id.

    Accepts canonical IDs ("X5000") and aliases ("x5000",
    "amigaone x5000", etc). Case-insensitive. Returns None if no match.
    """
    if not name:
        return None
    if name in SUPPORTED_MACHINES:
        return name
    return MACHINE_ALIASES.get(name.strip().lower())


def detect_machine_from_iso(iso_filename: str) -> tuple[str, str] | None:
    """Detect (machine_id, friendly_name) from an ISO filename.

    Tries supported prefixes in declaration order so the longer
    X5000/A1222 prefixes match before the bare AmigaOne one. Returns
    None if no prefix matches.
    """
    for prefix, info in ISO_PREFIX_TO_MACHINE:
        if iso_filename.startswith(prefix):
            return info
    return None


def is_unsupported_iso(iso_filename: str) -> bool:
    """True if `iso_filename` matches a known-unsupported prefix."""
    return any(iso_filename.startswith(p) for p in UNSUPPORTED_ISO_PREFIXES)
