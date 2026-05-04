"""MCP resources for browsing the per-run NDJSON archive.

URIs:
    amiga://runs/                      - list of runs (timestamps)
    amiga://runs/{run_id}              - metadata for one run
    amiga://runs/{run_id}/calls        - NDJSON contents (truncated
                                          if larger than the cap)

Resources are read-only views; they don't dispatch tool calls. The
backing store lives at `Config.server.archive_root`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Cap a single resource read at this many bytes to keep replies sane.
# Larger archives are paged by appending the byte offset as a query
# (not implemented yet - phase-11-polish).
_RESOURCE_BYTE_CAP = 1024 * 1024  # 1 MiB


def list_runs(archive_root: Path) -> list[dict[str, Any]]:
    """Return [{run_id, calls_path, calls_size_bytes}] for each run dir."""
    out: list[dict[str, Any]] = []
    if not archive_root.is_dir():
        return out
    for entry in sorted(archive_root.iterdir()):
        if not entry.is_dir():
            continue
        ndjson = entry / "tool-calls.ndjson"
        out.append({
            "run_id": entry.name,
            "calls_path": str(ndjson),
            "calls_size_bytes": ndjson.stat().st_size if ndjson.exists() else 0,
        })
    return out


def run_metadata(archive_root: Path, run_id: str) -> dict[str, Any]:
    """Summarise one run: count of tool calls, set of tools used,
    total wall-clock duration sum, first / last timestamp."""
    run_dir = archive_root / run_id
    if not run_dir.is_dir():
        return {"run_id": run_id, "exists": False}
    ndjson = run_dir / "tool-calls.ndjson"
    if not ndjson.exists():
        return {"run_id": run_id, "exists": True, "calls": 0}

    n = 0
    tools: set[str] = set()
    total_ms = 0
    first_ts: str | None = None
    last_ts: str | None = None
    err_count = 0
    with ndjson.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            n += 1
            if "tool" in row:
                tools.add(str(row["tool"]))
            if "duration_ms" in row:
                try:
                    total_ms += int(row["duration_ms"])
                except (TypeError, ValueError):
                    pass
            if "error" in row:
                err_count += 1
            ts = row.get("ts")
            if isinstance(ts, str):
                if first_ts is None:
                    first_ts = ts
                last_ts = ts

    return {
        "run_id": run_id,
        "exists": True,
        "calls": n,
        "errors": err_count,
        "tools": sorted(tools),
        "total_duration_ms": total_ms,
        "first_ts": first_ts,
        "last_ts": last_ts,
        "calls_path": str(ndjson),
    }


def run_calls_text(archive_root: Path, run_id: str) -> str:
    """Return the NDJSON content of one run, capped at
    _RESOURCE_BYTE_CAP. If the archive is larger, the prefix is
    returned plus a trailing notice line."""
    ndjson = archive_root / run_id / "tool-calls.ndjson"
    if not ndjson.exists():
        return ""
    sz = ndjson.stat().st_size
    if sz <= _RESOURCE_BYTE_CAP:
        return ndjson.read_text(encoding="utf-8")
    with ndjson.open("rb") as fh:
        head = fh.read(_RESOURCE_BYTE_CAP)
    notice = (
        "\n# truncated: archive is "
        f"{sz} bytes; showed first {_RESOURCE_BYTE_CAP}\n"
    )
    return head.decode("utf-8", errors="replace") + notice
