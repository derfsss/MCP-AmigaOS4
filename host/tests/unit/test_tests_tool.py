"""Unit tests for tests.* tool surface (phase 3)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from amiga_fleet_mcp.config import Config, PathsConfig
from amiga_fleet_mcp.errors import InvalidParams
from amiga_fleet_mcp.fleet import Fleet
from amiga_fleet_mcp.tools import tests as tests_tool


@pytest.fixture
def fake_aqt(tmp_path: Path) -> Path:
    """Build a minimal AmigaQemuTests-shaped tree under tmp_path."""
    root = tmp_path / "aqt"
    (root / "config" / "projects").mkdir(parents=True)
    (root / "lib").mkdir(parents=True)

    (root / "config" / "projects" / "FakeProject.json").write_text(json.dumps({
        "name": "FakeProject",
        "project_dir": "C:/projects/fake",
        "supported_machines": ["pegasos2", "amigaone"],
        "tests": [{"name": "noop", "command": "echo hi"}],
    }))

    # Tiny stand-in for AmigaQemuTests/lib/results.py - just enough that
    # tests_parse_output can exercise the import path.
    (root / "lib" / "results.py").write_text(
        "import re\n"
        "def parse_passfail_output(text):\n"
        "    out=[]\n"
        "    for line in text.splitlines():\n"
        "        m=re.match(r'\\[(PASS|FAIL)\\]\\s+(.*)', line.strip())\n"
        "        if m: out.append((m.group(2), m.group(1)=='PASS'))\n"
        "    return out\n"
    )
    return root


def _fleet(aqt: Path) -> Fleet:
    return Fleet(Config(paths=PathsConfig(amiga_qemu_tests=aqt)))


@pytest.mark.asyncio
async def test_list_suites_finds_project(fake_aqt: Path) -> None:
    fleet = _fleet(fake_aqt)
    out = await tests_tool.tests_list_suites(fleet)
    assert len(out.suites) == 1
    assert out.suites[0].name == "FakeProject"
    assert out.suites[0].supported_machines == ["pegasos2", "amigaone"]
    assert out.suites[0].test_count == 1


@pytest.mark.asyncio
async def test_list_suites_missing_path() -> None:
    fleet = Fleet(Config())
    with pytest.raises(InvalidParams):
        await tests_tool.tests_list_suites(fleet)


@pytest.mark.asyncio
async def test_parse_output_round_trips(fake_aqt: Path) -> None:
    # Reset the cached results module (other tests may have loaded
    # the real one via path side effect).
    tests_tool._LIB_RESULTS = None  # type: ignore[attr-defined]

    fleet = _fleet(fake_aqt)
    text = "[PASS] one\n[FAIL] two\n[PASS] three"
    pr = await tests_tool.tests_parse_output(
        fleet, base64.b64encode(text.encode()).decode()
    )
    assert pr.pass_count == 2
    assert pr.fail_count == 1
    assert [(e.name, e.passed) for e in pr.entries] == [
        ("one", True), ("two", False), ("three", True)
    ]


@pytest.mark.asyncio
async def test_parse_output_invalid_b64(fake_aqt: Path) -> None:
    fleet = _fleet(fake_aqt)
    with pytest.raises(InvalidParams):
        await tests_tool.tests_parse_output(fleet, "not!base64!")


@pytest.mark.asyncio
async def test_run_suite_unknown(fake_aqt: Path) -> None:
    fleet = _fleet(fake_aqt)
    with pytest.raises(InvalidParams):
        await tests_tool.tests_run_suite(fleet, "nope", "DoesNotExist")
