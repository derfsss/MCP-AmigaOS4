"""Kicklayout text-patching engine tests.

Pure-text transforms — no I/O, no transport. Validates module
injection and the X5000 SATA module rewrite.
"""

from __future__ import annotations

from amiga_fleet_mcp.installer.kicklayout import (
    add_modules,
    apply_patches,
    replace_text,
)

SAMPLE_KICKLAYOUT = """\
LABEL Default
EXEC Kickstart/kernel
MODULE Kickstart/exec.library
MODULE Kickstart/dos.library

LABEL Backup
EXEC Kickstart/kernel
MODULE Kickstart/exec.library
"""


SAMPLE_WITH_X5000_SATA = """\
LABEL Default
EXEC Kickstart/kernel
MODULE Kickstart/p5020sata.device.kmod
MODULE Kickstart/dos.library

LABEL Other
MODULE Kickstart/p5020sata.device.kmod
"""


def test_add_modules_injects_into_every_config_block():
    out = add_modules(SAMPLE_KICKLAYOUT,
                       ["Kickstart/mounter.library"], "Update 1")
    # Should appear in BOTH the Default and Backup blocks.
    assert out.count("MODULE Kickstart/mounter.library") == 2
    assert "; Update 1" in out


def test_add_modules_idempotent():
    once = add_modules(SAMPLE_KICKLAYOUT,
                        ["Kickstart/mounter.library"], "U1")
    twice = add_modules(once,
                         ["Kickstart/mounter.library"], "U1")
    assert once == twice
    assert twice.count("MODULE Kickstart/mounter.library") == 2


def test_add_modules_skips_blocks_already_having():
    text = (
        "LABEL Default\nEXEC Kickstart/kernel\n"
        "MODULE Kickstart/mounter.library\n\n"
        "LABEL Other\nEXEC Kickstart/kernel\n"
    )
    out = add_modules(text, ["Kickstart/mounter.library"], "U1")
    # Already in first block; gets added to second.
    assert out.count("MODULE Kickstart/mounter.library") == 2


def test_add_modules_recognises_basename_match():
    """Block has 'Kickstart/foo' — adding bare 'foo' should be a no-op."""
    text = "LABEL X\nEXEC Kickstart/kernel\nMODULE Kickstart/foo\n"
    out = add_modules(text, ["foo"], "L")
    assert out.count("MODULE") == 1
    out2 = add_modules(text, ["Kickstart/foo"], "L")
    assert out2.count("MODULE") == 1


def test_add_modules_skips_comment_only_blocks():
    text = "; just a comment\n; another comment\n\nLABEL X\nEXEC Kickstart/kernel\n"
    out = add_modules(text, ["Kickstart/foo"], "L")
    # Only the LABEL block should get the injection.
    assert out.count("MODULE Kickstart/foo") == 1


def test_add_modules_no_modules_is_noop():
    assert add_modules(SAMPLE_KICKLAYOUT, [], "L") == SAMPLE_KICKLAYOUT


def test_add_modules_empty_text():
    assert add_modules("", ["Kickstart/foo"], "L") == ""


def test_replace_text_x5000_sata():
    out = replace_text(SAMPLE_WITH_X5000_SATA,
                        "p5020sata.device.kmod",
                        "p50x0sata.device.kmod")
    assert "p5020sata" not in out
    assert out.count("p50x0sata.device.kmod") == 2


def test_replace_text_no_match_unchanged():
    out = replace_text(SAMPLE_KICKLAYOUT, "nonexistent", "replaced")
    assert out == SAMPLE_KICKLAYOUT


def test_apply_patches_combined():
    new, summary = apply_patches(
        SAMPLE_WITH_X5000_SATA,
        add_modules_patches=[
            {"modules": ["Kickstart/mounter.library"], "label": "Update 1"},
        ],
        replace_text_patches=[
            {"old": "p5020sata.device.kmod", "new": "p50x0sata.device.kmod"},
        ],
    )
    assert summary["changed"] is True
    assert summary["replacements_done"] == 2
    assert "Update 1" in summary["labels_applied"]
    assert "p5020sata" not in new
    assert new.count("MODULE Kickstart/mounter.library") == 2


def test_apply_patches_no_change():
    new, summary = apply_patches(
        SAMPLE_KICKLAYOUT,
        add_modules_patches=None,
        replace_text_patches=[
            {"old": "p5020sata.device.kmod", "new": "p50x0sata.device.kmod"},
        ],
    )
    assert summary["changed"] is False
    assert new == SAMPLE_KICKLAYOUT


def test_apply_patches_idempotent():
    once, _ = apply_patches(
        SAMPLE_WITH_X5000_SATA,
        add_modules_patches=[
            {"modules": ["Kickstart/mounter.library"], "label": "U1"},
        ],
        replace_text_patches=[
            {"old": "p5020sata.device.kmod", "new": "p50x0sata.device.kmod"},
        ],
    )
    twice, summary = apply_patches(
        once,
        add_modules_patches=[
            {"modules": ["Kickstart/mounter.library"], "label": "U1"},
        ],
        replace_text_patches=[
            {"old": "p5020sata.device.kmod", "new": "p50x0sata.device.kmod"},
        ],
    )
    assert once == twice
    assert summary["changed"] is False
