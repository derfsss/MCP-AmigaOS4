"""Archive retention, and keeping payloads out of the audit log.

Nothing pruned the archive or the QEMU serial logs, so both grew for
as long as the project was used: 3.2 GB on one development machine,
2.8 GB of it serial logs with single files over 150 MB, and run files
reaching 41 MB because base64 payloads were recorded in full.

These tests exist because this code deletes things. The bounds it
respects matter more than the space it reclaims.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from amiga_fleet_mcp.archive.store import (
    MAX_LOGGED_STR,
    Archive,
    _scrub,
    prune_logs,
    prune_runs,
)

# ---- payload sketching ---------------------------------------------


def test_large_base64_string_is_sketched_not_stored() -> None:
    """The case that made run files enormous: content_b64 arrives as
    `str`, so the bytes branch never saw it."""
    payload = "A" * (MAX_LOGGED_STR + 1)
    out = _scrub({"path": "SYS:x", "content_b64": payload})
    assert out["path"] == "SYS:x"          # small values untouched
    sketch = out["content_b64"]
    assert sketch["$str"] is True
    assert sketch["len"] == len(payload)
    assert len(sketch["head"]) == 256      # enough to identify it


def test_ordinary_strings_survive_intact() -> None:
    """An audit log that mangles its own contents is worse than a big
    one, so the threshold has to leave real arguments alone."""
    params = {"command": "Echo hello", "path": "SYS:System/MCPd/MCPd",
              "text": "x" * (MAX_LOGGED_STR - 1)}
    assert _scrub(params) == params


def test_bytes_are_still_sketched() -> None:
    out = _scrub({"blob": b"\x00\xff" * 500})
    assert out["blob"]["$bytes"] is True
    assert out["blob"]["len"] == 1000


def test_archive_round_trips_a_call(tmp_path: Path) -> None:
    a = Archive(tmp_path, keep_runs=None)
    a.log_call("fs.write", "x5000", {"content_b64": "B" * 99999},
               result={"ok": True}, duration_s=0.5)
    lines = (a.run_dir / "tool-calls.ndjson").read_text().splitlines()
    entry = json.loads(lines[0])
    assert entry["tool"] == "fs.write"
    assert entry["params"]["content_b64"]["$str"] is True
    assert entry["duration_ms"] == 500


# ---- run pruning ----------------------------------------------------


def _make_runs(root: Path, names: list[str]) -> None:
    for n in names:
        (root / n).mkdir(parents=True)
        (root / n / "tool-calls.ndjson").write_text("{}\n")


def test_prune_keeps_the_newest_runs(tmp_path: Path) -> None:
    names = [f"2026090{i}T120000Z" for i in range(1, 8)]
    _make_runs(tmp_path, names)
    removed = prune_runs(tmp_path, keep=3)
    left = sorted(d.name for d in tmp_path.iterdir())
    assert left == names[-3:]
    assert len(removed) == 4


def test_prune_is_a_noop_below_the_limit(tmp_path: Path) -> None:
    names = ["20260901T120000Z", "20260902T120000Z"]
    _make_runs(tmp_path, names)
    assert prune_runs(tmp_path, keep=10) == []
    assert sorted(d.name for d in tmp_path.iterdir()) == names


def test_prune_ignores_directories_it_does_not_recognise(
    tmp_path: Path,
) -> None:
    """Only run directories are ours to delete. Anything else under
    archive_root -- serial-logs, someone's notes -- stays."""
    _make_runs(tmp_path, ["20260901T120000Z", "20260902T120000Z",
                          "20260903T120000Z"])
    (tmp_path / "serial-logs").mkdir()
    (tmp_path / "serial-logs" / "keep.log").write_text("x")
    (tmp_path / "notes").mkdir()
    prune_runs(tmp_path, keep=1)
    assert (tmp_path / "serial-logs" / "keep.log").exists()
    assert (tmp_path / "notes").is_dir()


def test_archive_prunes_on_construction(tmp_path: Path) -> None:
    """`keep_runs` is a ceiling on what exists, and the run being
    started counts towards it -- so keep=2 leaves the newest old run
    plus the new one, not two old ones plus a third."""
    _make_runs(tmp_path, [f"2026080{i}T120000Z" for i in range(1, 6)])
    a = Archive(tmp_path, keep_runs=2)
    left = sorted(d.name for d in tmp_path.iterdir())
    assert len(left) == 2
    assert a.run_dir.name in left
    assert "20260805T120000Z" in left      # the newest survivor
    assert "20260801T120000Z" not in left  # the oldest went


def test_keep_none_disables_pruning(tmp_path: Path) -> None:
    _make_runs(tmp_path, [f"2026080{i}T120000Z" for i in range(1, 6)])
    Archive(tmp_path, keep_runs=None)
    assert len(list(tmp_path.iterdir())) == 6


# ---- serial log pruning ---------------------------------------------


def test_prune_logs_keeps_the_newest(tmp_path: Path) -> None:
    for i in range(6):
        f = tmp_path / f"{1700000000 + i}.log"
        f.write_text("boot output")
        # mtime order is what decides, so make it unambiguous.
        import os
        os.utime(f, (1700000000 + i, 1700000000 + i))
    removed = prune_logs(tmp_path, keep=2)
    assert len(removed) == 4
    left = sorted(f.name for f in tmp_path.glob("*.log"))
    assert left == ["1700000004.log", "1700000005.log"]


def test_prune_logs_leaves_other_files_alone(tmp_path: Path) -> None:
    (tmp_path / "a.log").write_text("x")
    (tmp_path / "notes.txt").write_text("keep me")
    time.sleep(0.01)
    (tmp_path / "b.log").write_text("y")
    prune_logs(tmp_path, keep=1)
    assert (tmp_path / "notes.txt").exists()


def test_prune_logs_handles_a_missing_directory(tmp_path: Path) -> None:
    assert prune_logs(tmp_path / "nope", keep=3) == []


# ---- serial log size budget -----------------------------------------
#
# A count is not a bound on the disk. Twenty logs of 150 MB is 3 GB,
# which is how the 2.8 GB in the module docstring accumulated under a
# retention policy that was already "working".


def _make_logs(root: Path, sizes: list[int]) -> list[Path]:
    """Oldest first, one byte of content per unit of `sizes`."""
    import os
    out = []
    for i, n in enumerate(sizes):
        f = root / f"{1700000000 + i}.log"
        f.write_bytes(b"x" * n)
        os.utime(f, (1700000000 + i, 1700000000 + i))
        out.append(f)
    return out


def test_size_budget_deletes_oldest_until_it_fits(tmp_path: Path) -> None:
    _make_logs(tmp_path, [100, 100, 100, 100])
    prune_logs(tmp_path, keep=10, max_total_bytes=250)
    left = sorted(f.name for f in tmp_path.glob("*.log"))
    assert left == ["1700000002.log", "1700000003.log"]


def test_the_newest_log_is_never_deleted(tmp_path: Path) -> None:
    """It is the one a running guest is probably still writing to, and
    on Windows the unlink would fail anyway -- better to be explicit
    than to rely on the OS refusing."""
    _make_logs(tmp_path, [10_000])
    prune_logs(tmp_path, keep=10, max_total_bytes=1)
    assert [f.name for f in tmp_path.glob("*.log")] == ["1700000000.log"]


def test_a_single_oversized_log_survives_alongside_the_newest(
    tmp_path: Path,
) -> None:
    _make_logs(tmp_path, [500, 500])
    prune_logs(tmp_path, keep=10, max_total_bytes=100)
    # The older one goes; the newest stays whatever its size.
    assert [f.name for f in tmp_path.glob("*.log")] == ["1700000001.log"]


def test_budget_and_count_compose(tmp_path: Path) -> None:
    """Count trims first, then the budget trims what is left -- and a
    file must not be deleted twice or counted after removal."""
    _make_logs(tmp_path, [100] * 6)
    removed = prune_logs(tmp_path, keep=4, max_total_bytes=250)
    assert len(removed) == len(set(removed)) == 4
    left = sorted(f.name for f in tmp_path.glob("*.log"))
    assert left == ["1700000004.log", "1700000005.log"]


def test_no_budget_leaves_count_behaviour_unchanged(tmp_path: Path) -> None:
    _make_logs(tmp_path, [10_000] * 3)
    assert prune_logs(tmp_path, keep=3) == []
    assert len(list(tmp_path.glob("*.log"))) == 3


def test_budget_alone_works_with_keep_disabled(tmp_path: Path) -> None:
    _make_logs(tmp_path, [100] * 4)
    prune_logs(tmp_path, keep=0, max_total_bytes=150)
    left = sorted(f.name for f in tmp_path.glob("*.log"))
    assert left == ["1700000003.log"]
