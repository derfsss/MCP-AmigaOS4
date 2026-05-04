"""Unit tests for archive_resources (phase 11 polish)."""

from __future__ import annotations

import json
from pathlib import Path

from amiga_fleet_mcp.archive import Archive
from amiga_fleet_mcp.resources import archive_resources


def _make_run(tmp_path: Path, run_id: str = "manual") -> Path:
    run_dir = tmp_path / run_id
    run_dir.mkdir(parents=True)
    nd = run_dir / "tool-calls.ndjson"
    nd.write_text(
        json.dumps({"ts": "2026-04-29T08:00:00.000+00:00",
                    "tool": "fs.list", "target": "qemu-pegasos2",
                    "params": {"path": "RAM:"},
                    "result": [{"name": "C", "type": "dir"}],
                    "duration_ms": 12}) + "\n"
        + json.dumps({"ts": "2026-04-29T08:00:01.234+00:00",
                      "tool": "fs.read", "target": "qemu-pegasos2",
                      "params": {"path": "RAM:nope"},
                      "error": {"code": -32001, "message": "Object not found"},
                      "duration_ms": 5}) + "\n",
        encoding="utf-8",
    )
    return run_dir


def test_list_runs_empty(tmp_path: Path) -> None:
    assert archive_resources.list_runs(tmp_path / "missing") == []
    (tmp_path / "empty").mkdir()
    out = archive_resources.list_runs(tmp_path)
    assert out == [{"run_id": "empty", "calls_path": str(tmp_path / "empty" / "tool-calls.ndjson"),
                    "calls_size_bytes": 0}]


def test_list_runs_single(tmp_path: Path) -> None:
    _make_run(tmp_path, "run_A")
    out = archive_resources.list_runs(tmp_path)
    assert len(out) == 1
    assert out[0]["run_id"] == "run_A"
    assert out[0]["calls_size_bytes"] > 0


def test_run_metadata(tmp_path: Path) -> None:
    _make_run(tmp_path, "rid_xyz")
    md = archive_resources.run_metadata(tmp_path, "rid_xyz")
    assert md["exists"] is True
    assert md["calls"] == 2
    assert md["errors"] == 1
    assert md["tools"] == ["fs.list", "fs.read"]
    assert md["total_duration_ms"] == 17
    assert md["first_ts"] is not None
    assert md["last_ts"] is not None


def test_run_metadata_missing(tmp_path: Path) -> None:
    md = archive_resources.run_metadata(tmp_path, "no_such")
    assert md == {"run_id": "no_such", "exists": False}


def test_run_calls_text(tmp_path: Path) -> None:
    _make_run(tmp_path, "r1")
    text = archive_resources.run_calls_text(tmp_path, "r1")
    lines = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert len(lines) == 2
    assert lines[0]["tool"] == "fs.list"
    assert lines[1]["error"]["code"] == -32001


def test_run_calls_text_truncation(tmp_path: Path) -> None:
    run_dir = tmp_path / "big"
    run_dir.mkdir()
    nd = run_dir / "tool-calls.ndjson"
    huge = ("{\"x\":1}\n" * 200000).encode()  # ~1.6 MiB
    nd.write_bytes(huge)
    text = archive_resources.run_calls_text(tmp_path, "big")
    assert "truncated" in text


def test_real_archive_writes_then_resource_reads(tmp_path: Path) -> None:
    """End-to-end: Archive logs a call, archive_resources reads it back."""
    a = Archive(tmp_path)
    a.log_call("fs.list", "x", {"path": "RAM:"},
               result=[{"name": "C", "type": "dir"}], duration_s=0.012)
    runs = archive_resources.list_runs(tmp_path)
    assert len(runs) == 1
    md = archive_resources.run_metadata(tmp_path, runs[0]["run_id"])
    assert md["calls"] == 1
    assert md["tools"] == ["fs.list"]
