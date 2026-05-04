"""Per-run tool-call archive (NDJSON).

One subdirectory per server start, named after the start timestamp.
Inside, `tool-calls.ndjson` accumulates one JSON object per tool call
with name, target, params, result-or-error, and wall-clock duration.

Retention is the user's problem (config.archive_root). The schema
is deliberately minimal so consumers can extend `_meta` for
correlation IDs, progress tokens, etc.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class Archive:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        self._run_dir = self._root / ts
        self._run_dir.mkdir(parents=True, exist_ok=True)
        self._path = self._run_dir / "tool-calls.ndjson"
        self._lock = threading.Lock()

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


def _scrub(obj: Any) -> Any:
    """Truncate large bytes payloads in archive entries.

    fs.read / fs.write parameters can be many MB; we don't want the
    archive to balloon. Replace bytes-like with a {len, head_b64} sketch.
    """
    import base64

    if isinstance(obj, bytes):
        return {"$bytes": True, "len": len(obj),
                "head_b64": base64.b64encode(obj[:256]).decode("ascii")}
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return obj


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
