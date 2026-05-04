"""Archive / NDJSON store tests."""

from __future__ import annotations

import json
from pathlib import Path

from amiga_fleet_mcp.archive import Archive, Timer


def test_archive_writes_ndjson(tmp_path: Path) -> None:
    a = Archive(tmp_path / "ar")
    a.log_call("fs.list", "qemu-pegasos2",
               {"path": "SYS:"},
               result=[{"name": "C", "type": "dir"}],
               duration_s=0.123)
    out = (a.run_dir / "tool-calls.ndjson").read_text()
    rows = [json.loads(line) for line in out.splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["tool"] == "fs.list"
    assert rows[0]["target"] == "qemu-pegasos2"
    assert rows[0]["params"] == {"path": "SYS:"}
    assert rows[0]["duration_ms"] == 123


def test_archive_scrubs_bytes_payload(tmp_path: Path) -> None:
    a = Archive(tmp_path / "ar")
    a.log_call("fs.write", "x", {"data": b"\x00\x01\x02" * 1000},
               result={"ok": True})
    rows = [
        json.loads(line)
        for line in (a.run_dir / "tool-calls.ndjson").read_text().splitlines()
        if line.strip()
    ]
    p = rows[0]["params"]["data"]
    assert p["$bytes"] is True
    assert p["len"] == 3000
    assert "head_b64" in p


def test_archive_logs_error(tmp_path: Path) -> None:
    a = Archive(tmp_path / "ar")
    a.log_call("fs.delete", "x", {"path": "SYS:nope"},
               error={"code": -32001, "message": "Object not found"},
               duration_s=0.01)
    rows = [
        json.loads(line)
        for line in (a.run_dir / "tool-calls.ndjson").read_text().splitlines()
        if line.strip()
    ]
    assert "result" not in rows[0]
    assert rows[0]["error"]["code"] == -32001


def test_timer() -> None:
    with Timer() as t:
        pass
    assert t.elapsed >= 0.0
