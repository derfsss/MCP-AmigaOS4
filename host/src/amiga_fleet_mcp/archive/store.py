"""Per-run tool-call archive (NDJSON).

One subdirectory per server start, named after the start timestamp.
Inside, `tool-calls.ndjson` accumulates one JSON object per tool call
with name, target, params, result-or-error, and wall-clock duration.

Retention is bounded by `[server] archive_keep_runs`; the schema
is deliberately minimal so consumers can extend `_meta` for
correlation IDs, progress tokens, etc.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class Archive:
    def __init__(self, root: Path, keep_runs: int | None = None) -> None:
        self._root = Path(root)
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self._run_dir = self._root / ts
        self._run_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._run_dir / "tool-calls.ndjson"
        self._lock = threading.Lock()
        if keep_runs is not None and keep_runs > 0:
            prune_runs(self._root, keep_runs)

    @property
    def run_dir(self) -> Path:
        return self._run_dir

    def log_call(
        self,
        tool: str,
        target: str | None,
        params: dict[str, Any],
        *,
        result: Any = None,
        error: dict[str, Any] | None = None,
        duration_s: float | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "tool": tool,
            "target": target,
            "params": _scrub(params),
        }
        if error is not None:
            entry["error"] = error
        else:
            entry["result"] = _scrub(result)
        if duration_s is not None:
            entry["duration_ms"] = int(duration_s * 1000)

        line = json.dumps(entry, ensure_ascii=False, default=_default_encoder) + "\n"
        with self._lock, self._path.open("a", encoding="utf-8") as fh:
            fh.write(line)


#: Longest string recorded verbatim. Base64 payloads arrive as `str`,
#: not `bytes`, so the bytes branch below never saw them and a single
#: upload could put tens of megabytes into the archive -- measured at
#: 41 MB for one run file. A sketch identifies the payload without
#: storing it; the file itself is on the target, which is the point.
MAX_LOGGED_STR = 4096


def _scrub(obj: Any) -> Any:
    """Keep oversized payloads out of archive entries.

    fs.read / fs.write carry many MB, in `bytes` for some callers and
    base64 `str` for others. Both are replaced with a length-and-head
    sketch: enough to see what a call was carrying, not so much that
    the audit log becomes a second copy of the data.
    """
    import base64

    if isinstance(obj, bytes):
        return {"$bytes": True, "len": len(obj),
                "head_b64": base64.b64encode(obj[:256]).decode("ascii")}
    if isinstance(obj, str) and len(obj) > MAX_LOGGED_STR:
        return {"$str": True, "len": len(obj), "head": obj[:256]}
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


def prune_runs(root: Path, keep: int) -> list[Path]:
    """Delete all but the newest `keep` run directories.

    Nothing here was ever removed, so an archive grows for as long as
    the project is used -- 685 run directories and 3.2 GB on one
    development machine. Run directories are named for their start
    time, so lexical order is chronological.

    Returns what was removed. Failures are ignored: a locked or
    vanished directory is not worth failing a server start over.
    """
    if keep <= 0 or not root.is_dir():
        return []
    runs = sorted(
        (d for d in root.iterdir()
         if d.is_dir() and len(d.name) == 16 and d.name.endswith("Z")),
        key=lambda d: d.name,
    )
    removed: list[Path] = []
    for d in runs[:-keep] if len(runs) > keep else []:
        try:
            shutil.rmtree(d)
            removed.append(d)
        except OSError:
            pass
    return removed


def prune_logs(
    directory: Path,
    keep: int,
    pattern: str = "*.log",
    max_total_bytes: int | None = None,
) -> list[Path]:
    """Bound the QEMU serial logs in `directory` by count and by size.

    QEMU writes its serial output straight to a file descriptor, one
    new file per launch, for the life of the guest. A chatty boot with
    kernel debug enabled produces well over 100 MB, and nothing ever
    removed the old ones: 2.8 GB of them on one machine.

    Capping an individual file would mean sitting between QEMU and the
    disk, which we don't; truncating one it holds open reclaims nothing
    and corrupts what is left. What can be done from outside the writer
    is to bound the collection, and a count on its own does not do that
    -- twenty logs of 150 MB is still 3 GB. So `keep` bounds how many
    and `max_total_bytes` bounds how much, oldest deleted first.

    The newest file is never deleted: it is the one a running guest is
    most likely still writing to.
    """
    if not directory.is_dir():
        return []
    if keep <= 0 and max_total_bytes is None:
        return []

    files = sorted(
        (f for f in directory.glob(pattern) if f.is_file()),
        key=lambda f: f.stat().st_mtime,
    )
    if not files:
        return []

    doomed: list[Path] = []
    if keep > 0 and len(files) > keep:
        doomed = files[:-keep]

    if max_total_bytes is not None:
        # Oldest first, stopping before the newest, until the survivors
        # fit the budget.
        survivors = [f for f in files if f not in doomed]
        total = sum(_size_or_zero(f) for f in survivors)
        for f in survivors[:-1]:
            if total <= max_total_bytes:
                break
            total -= _size_or_zero(f)
            doomed.append(f)

    removed: list[Path] = []
    for f in doomed:
        try:
            f.unlink()
            removed.append(f)
        except OSError:
            # Most likely the live log on Windows, where the running
            # QEMU holds it open. Leaving it is the correct outcome.
            pass
    return removed


def _size_or_zero(f: Path) -> int:
    try:
        return f.stat().st_size
    except OSError:
        return 0


def _default_encoder(o: Any) -> Any:
    if isinstance(o, Path):
        return str(o)
    raise TypeError(f"unserialisable: {type(o).__name__}")


class Timer:
    """Tiny context manager: `with Timer() as t: ...; print(t.elapsed)`."""

    def __init__(self) -> None:
        self.elapsed: float = 0.0
        self._t0: float = 0.0

    def __enter__(self) -> Timer:
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *_: object) -> None:
        self.elapsed = time.perf_counter() - self._t0
