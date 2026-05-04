"""Kicklayout text-patching engine (host-side, pure Python).

Pure text transforms — no file I/O. Tool callers fs.read the file,
run patches here, then fs.write the result back.

The format we're editing: AmigaOS Kicklayout, blank-line-separated
config blocks. A block is "config" (eligible for MODULE injection)
if it contains a LABEL or EXEC keyword; pure-comment blocks are
left alone. Append-only — existing MODULE lines are never removed.
"""

from __future__ import annotations

import re


def _block_is_config(block_lines: list[str]) -> bool:
    """A block is 'config' (eligible for MODULE injection) if it has
    a non-comment LABEL or EXEC keyword. Comment-only or blank-line
    blocks are left alone."""
    for ln in block_lines:
        s = ln.lstrip()
        if s.startswith(";") or s == "":
            continue
        up = s.upper()
        if up.startswith("LABEL") or up.startswith("EXEC"):
            return True
    return False


def _block_has_module(block_lines: list[str], module_name: str) -> bool:
    """Match by basename so 'Kickstart/foo' and bare 'foo' both count."""
    tail = module_name.split("/")[-1]
    patt = re.compile(r"^\s*MODULE\b.*" + re.escape(tail) + r"\b")
    return any(patt.search(ln) for ln in block_lines)


def add_modules(text: str, modules: list[str], label: str) -> str:
    """Inject `MODULE <name>` lines into every config block of `text`
    that doesn't already contain them. Idempotent.

    `label` is written as a comment header above the injected lines so
    the source of the edit is recoverable from the file alone (e.g.
    "Update 1 modules", "Enhancer 2.2 modules").
    """
    if not modules:
        return text
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    block_start = 0

    def flush_block(start: int, end: int) -> None:
        block = lines[start:end]
        out.extend(block)
        if not _block_is_config(block):
            return
        missing = [m for m in modules if not _block_has_module(block, m)]
        if not missing:
            return
        out.append(";\n")
        out.append(f"; {label}\n")
        for m in missing:
            out.append(f"MODULE {m}\n")

    i = 0
    while i < len(lines):
        if lines[i].strip() == "":
            if block_start < i:
                flush_block(block_start, i)
            out.append(lines[i])
            block_start = i + 1
        i += 1
    if block_start < len(lines):
        flush_block(block_start, len(lines))

    new_text = "".join(out)
    return new_text


def replace_text(text: str, old: str, new: str) -> str:
    """Verbatim substring replacement. Mirrors
    rewrite_x5000_sata_kicklayout's pattern (replacing
    p5020sata.device.kmod with p50x0sata.device.kmod everywhere)."""
    return text.replace(old, new)


def apply_patches(
    text: str, *,
    add_modules_patches: list[dict] | None = None,
    replace_text_patches: list[dict] | None = None,
) -> tuple[str, dict]:
    """Compose patches in order: add_modules first, then replace_text.

    `add_modules_patches`: list of {"modules": [str], "label": str}
    `replace_text_patches`: list of {"old": str, "new": str}

    Returns (new_text, summary) where summary describes what changed.
    """
    summary: dict = {
        "modules_added": 0,
        "labels_applied": [],
        "replacements_done": 0,
        "replacements": [],
        "changed": False,
    }

    new_text = text
    for p in add_modules_patches or []:
        before = new_text
        new_text = add_modules(new_text, p["modules"], p["label"])
        if new_text != before:
            summary["labels_applied"].append(p["label"])
            # crude count: number of new MODULE lines added
            summary["modules_added"] += new_text.count(
                "\nMODULE "
            ) - before.count("\nMODULE ")

    for p in replace_text_patches or []:
        before = new_text
        new_text = replace_text(new_text, p["old"], p["new"])
        if new_text != before:
            n = before.count(p["old"])
            summary["replacements_done"] += n
            summary["replacements"].append({
                "old": p["old"], "new": p["new"], "count": n,
            })

    summary["changed"] = (new_text != text)
    return new_text, summary
