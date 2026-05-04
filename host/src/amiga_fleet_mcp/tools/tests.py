"""tests.* - test orchestration.

Implements the standard DOS test sequence on top of the MCPd fs/exec
primitives. Reuses AmigaQemuTests's `lib/results.py:parse_passfail_output`
for tests.parse_output (a pure string parser, imported via importlib).
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

from pydantic import BaseModel

from ..errors import InternalError, InvalidParams, JsonRpcError, TargetError
from ..fleet import Fleet
from . import exec as exec_tool
from . import fs as fs_tool

# ---------- AmigaQemuTests module loading -------------------------

_LIB_RESULTS: ModuleType | None = None


def _load_aqt_module(aqt_root: Path | None, rel_path: str,
                     module_alias: str) -> ModuleType:
    if aqt_root is None:
        raise InternalError("tests.* needs paths.amiga_qemu_tests in config.toml")
    src = Path(aqt_root) / rel_path
    if not src.exists():
        raise InternalError(f"AmigaQemuTests module missing: {src}")
    spec = importlib.util.spec_from_file_location(module_alias, src)
    if spec is None or spec.loader is None:
        raise InternalError(f"failed to load {src}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _aqt_results(fleet: Fleet) -> ModuleType:
    global _LIB_RESULTS
    if _LIB_RESULTS is None:
        _LIB_RESULTS = _load_aqt_module(
            fleet.config.paths.amiga_qemu_tests,
            "lib/results.py", "_aqt_lib_results",
        )
    return _LIB_RESULTS


# ---------- list_suites -------------------------------------------


class SuiteSummary(BaseModel):
    name: str
    project_dir: str | None = None
    supported_machines: list[str]
    test_count: int


class SuitesResult(BaseModel):
    suites: list[SuiteSummary]


async def tests_list_suites(fleet: Fleet) -> SuitesResult:
    aqt = fleet.config.paths.amiga_qemu_tests
    if aqt is None:
        raise InvalidParams("paths.amiga_qemu_tests not set in config")
    proj_dir = Path(aqt) / "config" / "projects"
    if not proj_dir.is_dir():
        raise InternalError(f"AmigaQemuTests projects dir missing: {proj_dir}")

    out: list[SuiteSummary] = []
    for jf in sorted(proj_dir.glob("*.json")):
        with jf.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        out.append(SuiteSummary(
            name=data.get("name", jf.stem),
            project_dir=data.get("project_dir"),
            supported_machines=list(data.get("supported_machines", [])),
            test_count=len(data.get("tests", [])),
        ))
    return SuitesResult(suites=out)


# ---------- parse_output ------------------------------------------


class ParseEntry(BaseModel):
    name: str
    passed: bool


class ParseResult(BaseModel):
    entries: list[ParseEntry]
    pass_count: int
    fail_count: int


async def tests_parse_output(
    fleet: Fleet, output_b64: str
) -> ParseResult:
    try:
        text = base64.b64decode(output_b64, validate=True).decode(
            "utf-8", errors="replace"
        )
    except Exception as e:
        raise InvalidParams(f"output_b64 not valid base64: {e}") from e

    results_mod = _aqt_results(fleet)
    pairs: list[tuple[str, bool]] = results_mod.parse_passfail_output(text)
    entries = [ParseEntry(name=n, passed=p) for n, p in pairs]
    return ParseResult(
        entries=entries,
        pass_count=sum(1 for e in entries if e.passed),
        fail_count=sum(1 for e in entries if not e.passed),
    )


# ---------- run_standard_dos_tests --------------------------------


class StandardDosResult(BaseModel):
    target: str
    passed: int
    total: int
    entries: list[ParseEntry]


_TEST_DIR = "RAM:_aqt_test"


async def _safe(awaitable: Any) -> tuple[bool, Any]:
    try:
        return True, await awaitable
    except (JsonRpcError, Exception) as e:
        return False, e


async def tests_run_standard_dos_tests(
    fleet: Fleet, target: str
) -> StandardDosResult:
    """Standard DOS regression: list / makedir / write / stat / read /
    delete / version. Reimplemented via MCPd primitives so we don't
    need the SerialShell shim AmigaQemuTests/tests/standard/dos_basic.py
    relied on."""
    entries: list[ParseEntry] = []

    async def _add(name: str, passed: bool) -> None:
        entries.append(ParseEntry(name=name, passed=passed))

    # 1. List RAM:
    ok, _r = await _safe(fs_tool.fs_list(fleet, target, "RAM:"))
    await _add("DOS: List RAM:", ok)

    # 2. Pre-clean
    try:
        await fs_tool.fs_delete(fleet, target, _TEST_DIR)
    except (JsonRpcError, Exception):
        pass

    # 3. MakeDir
    ok, _r = await _safe(fs_tool.fs_makedir(fleet, target, _TEST_DIR))
    await _add("DOS: MakeDir _aqt_test", ok)

    # 4. Write a file
    payload = b"AmigaQemuTests marker 2026 \x00\x01\x02\x03\xff\n"
    ok, w = await _safe(fs_tool.fs_write(
        fleet, target, f"{_TEST_DIR}/marker.txt",
        base64.b64encode(payload).decode("ascii"),
    ))
    write_ok = ok and getattr(w, "size", -1) == len(payload)
    await _add("DOS: Write file (binary clean)", write_ok)

    # 5. Stat
    ok, st = await _safe(fs_tool.fs_stat(
        fleet, target, f"{_TEST_DIR}/marker.txt"
    ))
    stat_ok = ok and getattr(st, "type", None) == "file" and \
        getattr(st, "size", -1) == len(payload)
    await _add("DOS: Stat uploaded file", stat_ok)

    # 6. Read + verify
    ok, rd = await _safe(fs_tool.fs_read(
        fleet, target, f"{_TEST_DIR}/marker.txt"
    ))
    read_ok = ok and base64.b64decode(getattr(rd, "content_b64", "")) == payload
    await _add("DOS: Read + verify content integrity", read_ok)

    # 7. List shows the file
    ok, lst = await _safe(fs_tool.fs_list(fleet, target, _TEST_DIR))
    list_ok = ok and any(getattr(e, "name", "") == "marker.txt" for e in lst)
    await _add("DOS: List shows uploaded file", list_ok)

    # 8. exec.cmd Copy
    ok, _ec = await _safe(exec_tool.exec_cmd(
        fleet, target,
        f'Copy "{_TEST_DIR}/marker.txt" TO "{_TEST_DIR}/marker_copy.txt" CLONE',
    ))
    ok2, lst = await _safe(fs_tool.fs_list(fleet, target, _TEST_DIR))
    copy_ok = ok and ok2 and any(
        getattr(e, "name", "") == "marker_copy.txt" for e in lst
    )
    await _add("DOS: exec.cmd Copy", copy_ok)

    # 9. Delete file
    ok, _r = await _safe(fs_tool.fs_delete(
        fleet, target, f"{_TEST_DIR}/marker.txt"
    ))
    await _add("DOS: Delete file", ok)

    # 10. Delete directory recursively (Delete ALL via exec.cmd)
    await _safe(fs_tool.fs_delete(
        fleet, target, f"{_TEST_DIR}/marker_copy.txt"
    ))
    ok, _r = await _safe(fs_tool.fs_delete(fleet, target, _TEST_DIR))
    await _add("DOS: Delete directory", ok)

    # 11. sys.version sanity (proves exec.cmd-via-Version still works)
    from . import sys as sys_tool
    ok, v = await _safe(sys_tool.sys_version(fleet, target))
    ver_ok = ok and (
        getattr(v, "kickstart", None) is not None
        or "Kickstart" in getattr(v, "raw", "")
    )
    await _add("DOS: sys.version", ver_ok)

    passed_n = sum(1 for e in entries if e.passed)
    return StandardDosResult(
        target=target,
        passed=passed_n,
        total=len(entries),
        entries=entries,
    )


# ---------- run_suite ---------------------------------------------


class SuiteEntry(BaseModel):
    name: str
    passed: bool
    detail: str | None = None
    duration_s: float = 0.0
    output_excerpt: str | None = None


class SuiteRunResult(BaseModel):
    suite: str
    target: str
    boot_ok: bool
    passed: int
    total: int
    entries: list[SuiteEntry]


def _load_suite(fleet: Fleet, name: str) -> dict[str, Any]:
    aqt = fleet.config.paths.amiga_qemu_tests
    if aqt is None:
        raise InvalidParams("paths.amiga_qemu_tests not set")
    p = Path(aqt) / "config" / "projects" / f"{name}.json"
    if not p.exists():
        raise InvalidParams(
            f"unknown suite: {name!r}",
            data={"file": str(p)},
        )
    with p.open("r", encoding="utf-8") as fh:
        data: dict[str, Any] = json.load(fh)
    return data


async def tests_run_suite(
    fleet: Fleet, target: str, suite: str,
    *,
    skip_uploads: bool = False,
) -> SuiteRunResult:
    data = _load_suite(fleet, suite)
    entries: list[SuiteEntry] = []

    if not skip_uploads:
        for upload in data.get("upload_binaries", []):
            src = Path(upload["src"])
            if not src.is_absolute():
                proj = data.get("project_dir")
                if proj:
                    src = Path(proj) / src
            dst = upload["guest_dst"]
            if not src.exists():
                entries.append(SuiteEntry(
                    name=f"upload {dst}", passed=False,
                    detail=f"host file missing: {src}",
                ))
                continue
            try:
                content = src.read_bytes()
                await fs_tool.fs_write(
                    fleet, target, dst,
                    base64.b64encode(content).decode("ascii"),
                )
                entries.append(SuiteEntry(
                    name=f"upload {dst}", passed=True,
                    detail=f"{len(content)} bytes",
                ))
            except TargetError as e:
                entries.append(SuiteEntry(
                    name=f"upload {dst}", passed=False, detail=e.message,
                ))

    results_mod = _aqt_results(fleet)
    for spec in data.get("tests", []):
        name: str = spec.get("name", spec.get("command", "test"))
        cmd: str = spec["command"]
        timeout: float = float(spec.get("timeout", 30))
        markers: list[str] = list(spec.get("pass_markers", []))
        parse_pf: bool = bool(spec.get("parse_passfail", False))

        t0 = asyncio.get_event_loop().time()
        try:
            r = await exec_tool.exec_cmd(fleet, target, cmd, timeout_s=timeout)
            output = r.output
            err: str | None = None
        except TargetError as e:
            output = ""
            err = e.message
        duration = asyncio.get_event_loop().time() - t0

        if err is not None:
            entries.append(SuiteEntry(
                name=name, passed=False, detail=err, duration_s=duration,
            ))
            continue

        all_markers_ok = all(m in output for m in markers) if markers else True
        passfail_ok = True
        if parse_pf:
            pairs = results_mod.parse_passfail_output(output)
            n_pass = sum(1 for _, ok in pairs if ok)
            n_fail = sum(1 for _, ok in pairs if not ok)
            passfail_ok = n_fail == 0 and n_pass > 0
        passed = all_markers_ok and passfail_ok

        entries.append(SuiteEntry(
            name=name, passed=passed, duration_s=duration,
            output_excerpt=output[:600] if output else None,
        ))

    passed_n = sum(1 for e in entries if e.passed)
    return SuiteRunResult(
        suite=data.get("name", suite),
        target=target,
        boot_ok=True,
        passed=passed_n,
        total=len(entries),
        entries=entries,
    )
