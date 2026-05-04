"""Native AmigaOS 4.1 FE installer logic (host-side).

Composable MCP tools that drive an end-to-end install on an
AmigaOS 4 target: stage source media, mount the ISO, copy the base
OS, apply updates, install Enhancer software, write the bootloader,
register MCPd for auto-start, etc.

Package layout:

    machines.py   — supported machines, ISO prefix matching, aliases
    manifest.py   — per-machine required-files matrix
    kicklayout.py — Kicklayout patching rules
    sequences/    — per-machine install pipelines

Tool surface lives in `amiga_fleet_mcp.tools.installer`.
"""

from . import kicklayout
from .machines import (
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
from .manifest import required_files, scan_sources

__all__ = [
    "A1222_BACKUP_SIGNATURE",
    "A1222_BACKUP_VOLS",
    "CD_VERSION_SIGNATURES",
    "FORBIDDEN_DEST_ENTRIES",
    "ISO_PREFIX_TO_MACHINE",
    "MACHINES_NO_BOOTLOADER_FILE",
    "MACHINE_ALIASES",
    "SUPPORTED_MACHINES",
    "UNSUPPORTED_ISO_PREFIXES",
    "UPDATES_TO_APPLY",
    "UPDATE_LHA_NAME",
    "detect_machine_from_iso",
    "kicklayout",
    "required_files",
    "resolve_machine_alias",
    "scan_sources",
]
