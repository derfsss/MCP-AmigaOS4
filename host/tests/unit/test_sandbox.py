"""sandbox.* unit tests via fake transport.

Covers Phase A surface: probe (hit / miss / broken / Pegasos2),
deploy (cache invalidation), run_guest (happy path / trap exit /
config-driven defaults / per-target deny merge / per-target lock).
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from unittest import mock

import pytest

from amiga_fleet_mcp.config import (
    Config,
    McpdChannel,
    SandboxTargetConfig,
    TargetChannels,
    TargetConfig,
)
from amiga_fleet_mcp.errors import InvalidParams, NotCapable, TargetError
from amiga_fleet_mcp.fleet import Fleet
from amiga_fleet_mcp.tools import sandbox as sb

# ---- fake transport ------------------------------------------------


class FakeMcpd:
    """Records calls and serves canned responses keyed by (method,
    matcher). The matchers are tiny lambdas over the params dict so
    individual tests can stage exact behaviour."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.existing_paths: set[str] = set()
        self.file_contents: dict[str, bytes] = {}
        self.exec_responses: list[tuple[
            object, dict[str, object]
        ]] = []
        self.exec_default: dict[str, object] = {
            "output": "", "exit_code": 0,
        }

    def add_path(self, p: str, content: bytes = b"") -> None:
        self.existing_paths.add(p)
        if content:
            self.file_contents[p] = content

    def queue_exec(self, matcher, response) -> None:
        """Add a one-shot canned response for the next exec.cmd call
        whose params satisfy `matcher` (callable taking the params
        dict, returns bool). `response` is either a dict (returned
        verbatim) or an Exception (raised when the matcher fires)."""
        self.exec_responses.append((matcher, response))

    async def request(self, method, params=None, timeout_s=30.0):
        params = params or {}
        self.calls.append((method, params))
        if method == "fs.stat":
            if params["path"] in self.existing_paths:
                return {"type": "file", "size": 1024}
            raise TargetError(
                "not found", data={"path": params["path"]},
            )
        if method == "fs.read":
            path = params["path"]
            if path not in self.existing_paths:
                raise TargetError("not found", data={"path": path})
            content = self.file_contents.get(path, b"")
            return {
                "path": path,
                "size": len(content),
                "content_b64": base64.b64encode(content).decode("ascii"),
            }
        if method == "fs.delete":
            self.existing_paths.discard(params["path"])
            self.file_contents.pop(params["path"], None)
            return {"path": params["path"], "ok": True}
        if method == "fs.hash":
            return {"hash": "abc123" * 10 + "ab", "algo": "sha256"}
        if method == "exec.cmd":
            for i, (matcher, resp) in enumerate(self.exec_responses):
                if matcher(params):
                    self.exec_responses.pop(i)
                    if isinstance(resp, Exception):
                        raise resp
                    return resp
            return self.exec_default
        # Tests should never reach this; surface clearly.
        raise AssertionError(f"unexpected method: {method!r}")


def _make_config(machine: str | None = None,
                 tags: list[str] | None = None,
                 sandbox_cfg: SandboxTargetConfig | None = None,
                 sandboxvm_path: Path | None = None) -> Config:
    cfg = Config(targets={
        "tgt": TargetConfig(
            type="remote",
            machine=machine,
            tags=tags or [],
            sandbox=sandbox_cfg,
            channels=TargetChannels(
                mcpd=McpdChannel(endpoint="192.168.0.99:4322"),
            ),
        ),
    })
    if sandboxvm_path is not None:
        cfg.paths.sandboxvm = sandboxvm_path
    return cfg


@pytest.fixture(autouse=True)
def _reset_state():
    sb._reset_module_state_for_tests()
    yield
    sb._reset_module_state_for_tests()


@pytest.fixture
def fleet_with_fake():
    fleet = Fleet(_make_config())
    fake = FakeMcpd()
    fleet._mcpd["tgt"] = fake  # type: ignore[assignment]
    return fleet, fake


# ---- probe ---------------------------------------------------------


@pytest.mark.asyncio
async def test_probe_finds_default_path_and_runs_banner(fleet_with_fake):
    fleet, fake = fleet_with_fake
    fake.add_path("SYS:Tools/sandboxvm")
    fake.queue_exec(
        lambda p: "sandboxvm" in p["command"],
        {"output": "sandboxvm — usage banner\n   args: -m -w ...\n",
         "exit_code": 5},
    )

    res = await sb.sandbox_probe(fleet, "tgt")

    assert res.available is True
    assert res.code is None
    assert res.path == "SYS:Tools/sandboxvm"
    assert res.version_banner == "sandboxvm — usage banner"
    # All three default search candidates surfaced (transparency).
    assert res.searched == list(sb.DEFAULT_AOS_SEARCH)


@pytest.mark.asyncio
async def test_probe_per_target_path_takes_precedence(fleet_with_fake):
    fleet, fake = fleet_with_fake
    # Override the target's per-target sandbox path. Re-build the
    # fleet so the new TargetConfig is in place.
    fleet._config.targets["tgt"].sandbox = SandboxTargetConfig(
        path="Tools:sandboxvm",
    )
    fake.add_path("Tools:sandboxvm")
    fake.queue_exec(lambda p: True, {"output": "banner", "exit_code": 5})

    res = await sb.sandbox_probe(fleet, "tgt")

    assert res.available is True
    assert res.path == "Tools:sandboxvm"
    # Per-target override shows up first in the search order.
    assert res.searched[0] == "Tools:sandboxvm"


@pytest.mark.asyncio
async def test_probe_missing_returns_typed_code(fleet_with_fake):
    fleet, _fake = fleet_with_fake

    res = await sb.sandbox_probe(fleet, "tgt")

    assert res.available is False
    assert res.code == "SANDBOXVM_MISSING"
    assert res.path is None
    assert "sandbox.deploy" in (res.hint or "")
    # GitHub URL surfaced so a fresh user has a build target
    # without grepping docs.
    assert sb.SANDBOXVM_UPSTREAM_URL in (res.hint or "")
    # Negative result NOT cached -- a follow-up deploy must work
    # without an explicit invalidate.
    assert "tgt" not in sb._PROBE_CACHE


@pytest.mark.asyncio
async def test_probe_broken_binary_returns_typed_code(fleet_with_fake):
    fleet, fake = fleet_with_fake
    fake.add_path("SYS:Tools/sandboxvm")
    # Make exec.cmd against the binary raise — that's what
    # "binary won't start" looks like to the probe (vs empty
    # captured output, which is benign and just means the
    # daemon's stdout pipe didn't catch anything).
    fake.queue_exec(
        lambda p: '"SYS:Tools/sandboxvm"' == p["command"],
        Exception("simulated load failure"),
    )

    res = await sb.sandbox_probe(fleet, "tgt")

    assert res.available is False
    assert res.code == "SANDBOXVM_BROKEN"
    assert res.path == "SYS:Tools/sandboxvm"
    assert "Re-deploy" in (res.hint or "")


@pytest.mark.asyncio
async def test_probe_empty_banner_still_available(fleet_with_fake):
    """Reaching exec.cmd with a structured exit code is sufficient
    proof the binary works — the daemon's stdout pipe sometimes
    captures nothing for clib4/newlib-linked binaries (buffering
    quirk) and that should NOT mark the binary as broken."""
    fleet, fake = fleet_with_fake
    fake.add_path("SYS:Tools/sandboxvm")
    # Default exec response: empty output, exit_code 0.
    res = await sb.sandbox_probe(fleet, "tgt")

    assert res.available is True
    assert res.code is None
    assert res.path == "SYS:Tools/sandboxvm"
    # Empty banner is acceptable.
    assert res.version_banner is None


@pytest.mark.asyncio
async def test_probe_pegasos2_refused_via_machine_field():
    fleet = Fleet(_make_config(machine="pegasos2"))
    fleet._mcpd["tgt"] = FakeMcpd()  # type: ignore[assignment]

    res = await sb.sandbox_probe(fleet, "tgt")

    assert res.available is False
    assert res.code == "SANDBOXVM_INCOMPATIBLE_TARGET"
    assert res.machine == "pegasos2"
    assert res.machine_ok is False


@pytest.mark.asyncio
async def test_probe_pegasos2_refused_via_tag():
    fleet = Fleet(_make_config(tags=["qemu", "pegasos2"]))
    fleet._mcpd["tgt"] = FakeMcpd()  # type: ignore[assignment]

    res = await sb.sandbox_probe(fleet, "tgt")

    assert res.available is False
    assert res.code == "SANDBOXVM_INCOMPATIBLE_TARGET"


@pytest.mark.asyncio
async def test_probe_caches_positive_result(fleet_with_fake):
    fleet, fake = fleet_with_fake
    fake.add_path("SYS:Tools/sandboxvm")
    fake.queue_exec(lambda p: True, {"output": "banner", "exit_code": 5})

    first = await sb.sandbox_probe(fleet, "tgt")
    assert first.available is True
    assert "tgt" in sb._PROBE_CACHE

    # Second call via the cached path: no new exec.cmd should fire
    # because _ensure_available short-circuits on cached available.
    pre_exec_count = sum(1 for m, _ in fake.calls if m == "exec.cmd")
    await sb._ensure_available(fleet, "tgt")
    post_exec_count = sum(1 for m, _ in fake.calls if m == "exec.cmd")
    assert post_exec_count == pre_exec_count


# ---- deploy --------------------------------------------------------


@pytest.mark.asyncio
async def test_deploy_requires_confirm(fleet_with_fake, tmp_path):
    fleet, _fake = fleet_with_fake
    src = tmp_path / "sandboxvm"
    src.write_bytes(b"\x7fELF" + b"x" * 256)

    with pytest.raises(InvalidParams, match="confirm=True"):
        await sb.sandbox_deploy(
            fleet, "tgt", source=str(src), confirm=False,
        )


@pytest.mark.asyncio
async def test_deploy_explicit_source_and_dest(fleet_with_fake, tmp_path):
    fleet, fake = fleet_with_fake
    src = tmp_path / "sandboxvm"
    src.write_bytes(b"\x7fELF" + b"x" * 1024)

    # Pre-stage probe to populate cache; verify deploy clears it.
    fake.add_path("SYS:Tools/sandboxvm")
    fake.queue_exec(lambda p: True, {"output": "banner", "exit_code": 5})
    await sb.sandbox_probe(fleet, "tgt")
    assert "tgt" in sb._PROBE_CACHE

    # Stub fs_upload so we don't depend on the chunked upload path
    # (covered by its own tests).
    async def fake_upload(fleet_, target, *, local_path, remote_path,
                          **kwargs):
        from amiga_fleet_mcp.tools.fs import UploadResult
        # Mark the destination as existing so the post-deploy probe
        # finds it.
        fake.add_path(remote_path)
        return UploadResult(
            target=target, local_path=local_path,
            remote_path=remote_path,
            bytes_total=Path(local_path).stat().st_size,
            bytes_sent_compressed=Path(local_path).stat().st_size,
            chunks=1, compressed_chunks=0,
            elapsed_s=0.05, speed_mib_s=20.0,
            compression_ratio=1.0,
            sha256_verified=True,
            sha256="deadbeef" * 8,
        )

    # Re-arm exec banner for the post-deploy probe call.
    fake.queue_exec(lambda p: True, {"output": "banner", "exit_code": 5})
    with mock.patch.object(sb.fs_tool, "fs_upload", fake_upload):
        res = await sb.sandbox_deploy(
            fleet, "tgt",
            source=str(src),
            dest_path="Tools:sandboxvm",
            confirm=True,
        )

    assert res.dest_path == "Tools:sandboxvm"
    assert res.bytes_total == src.stat().st_size
    assert res.sha256 is not None
    # Post-deploy probe ran + reports available.
    assert res.probe_after.available is True
    assert res.probe_after.path == "Tools:sandboxvm"


@pytest.mark.asyncio
async def test_deploy_falls_back_to_paths_config(fleet_with_fake, tmp_path):
    src = tmp_path / "sandboxvm"
    src.write_bytes(b"\x7fELF" + b"x" * 32)

    fleet = Fleet(_make_config(sandboxvm_path=src))
    fake = FakeMcpd()
    fleet._mcpd["tgt"] = fake  # type: ignore[assignment]

    async def fake_upload(fleet_, target, *, local_path, remote_path,
                          **kwargs):
        from amiga_fleet_mcp.tools.fs import UploadResult
        fake.add_path(remote_path)
        return UploadResult(
            target=target, local_path=local_path,
            remote_path=remote_path,
            bytes_total=Path(local_path).stat().st_size,
            bytes_sent_compressed=Path(local_path).stat().st_size,
            chunks=1, compressed_chunks=0,
            elapsed_s=0.01, speed_mib_s=1.0,
            compression_ratio=1.0,
        )

    fake.queue_exec(lambda p: True, {"output": "banner", "exit_code": 5})
    with mock.patch.object(sb.fs_tool, "fs_upload", fake_upload):
        res = await sb.sandbox_deploy(fleet, "tgt", confirm=True)

    # Default dest path used when neither arg nor per-target override
    # exists.
    assert res.dest_path == sb.DEFAULT_DEPLOY_PATH
    # Source resolved from [paths] sandboxvm.
    assert res.source == str(src)


@pytest.mark.asyncio
async def test_deploy_source_missing_raises(fleet_with_fake):
    fleet, _ = fleet_with_fake
    with pytest.raises(InvalidParams, match="not set"):
        await sb.sandbox_deploy(fleet, "tgt", confirm=True)


# ---- run_guest -----------------------------------------------------


def _make_available_fleet(deny_libs=None,
                          extmem_mb=None, window_mb=None):
    """A fleet whose probe will succeed."""
    sandbox_cfg = None
    if any(v is not None for v in (deny_libs, extmem_mb, window_mb)):
        sandbox_cfg = SandboxTargetConfig(
            path="SYS:Tools/sandboxvm",
            deny_libs=list(deny_libs or []),
            default_extmem_mb=extmem_mb if extmem_mb is not None else 1024,
            default_window_mb=window_mb if window_mb is not None else 256,
        )
    fleet = Fleet(_make_config(sandbox_cfg=sandbox_cfg))
    fake = FakeMcpd()
    fleet._mcpd["tgt"] = fake  # type: ignore[assignment]
    fake.add_path("SYS:Tools/sandboxvm")
    return fleet, fake


@pytest.mark.asyncio
async def test_run_guest_clean_exit():
    fleet, fake = _make_available_fleet()
    # Probe banner, then guest run.
    fake.queue_exec(
        lambda p: '"SYS:Tools/sandboxvm"' == p["command"],
        {"output": "banner", "exit_code": 5},
    )
    fake.queue_exec(
        lambda p: "clib4hello" in p["command"],
        {"output": "[guest.0] hello\n", "exit_code": 0},
    )
    fake.add_path("T:sandboxvm-clib4hello.out", b"hello\nworld\n")

    res = await sb.sandbox_run_guest(
        fleet, "tgt", guest="Tools:tests/clib4hello",
    )

    assert res.exit_code == 0
    assert res.trap_kind is None
    assert res.trap_fingerprint is None
    assert res.name == "clib4hello"
    assert "hello\nworld\n" == res.stdout
    assert "T:sandboxvm-clib4hello.out" in res.capture_paths
    # Capture files cleaned up.
    assert "T:sandboxvm-clib4hello.out" not in fake.existing_paths


@pytest.mark.asyncio
async def test_run_guest_dsi_trap_classified():
    fleet, fake = _make_available_fleet()
    fake.queue_exec(
        lambda p: p["command"] == '"SYS:Tools/sandboxvm"',
        {"output": "banner", "exit_code": 5},
    )
    fake.queue_exec(
        lambda p: "crashy" in p["command"],
        {"output": "guest [0] returned -768\n", "exit_code": -768},
    )

    res = await sb.sandbox_run_guest(
        fleet, "tgt", guest="Tools:tests/crashy",
    )

    assert res.exit_code == -768
    assert res.trap_kind == "DSI"


@pytest.mark.asyncio
async def test_run_guest_kmod_libcall_fingerprint_in_stderr():
    fleet, fake = _make_available_fleet()
    fake.queue_exec(
        lambda p: p["command"] == '"SYS:Tools/sandboxvm"',
        {"output": "banner", "exit_code": 5},
    )
    fake.queue_exec(
        lambda p: "scsi" in p["command"],
        {"output": "", "exit_code": -768},
    )
    err_blob = (
        "Trap caught\n"
        "Traptype=0x300 dar=0x4 dsisr=0x00800000\n"
    )
    fake.add_path("T:sandboxvm-scsi.err", err_blob.encode("utf-8"))

    res = await sb.sandbox_run_guest(
        fleet, "tgt", guest="Tools:scsi", name="scsi",
    )

    assert res.trap_kind == "DSI"
    assert res.trap_fingerprint == "kmod_libcall_null4"


@pytest.mark.asyncio
async def test_run_guest_uses_per_target_defaults():
    fleet, fake = _make_available_fleet(
        deny_libs=["intuition.library"],
        extmem_mb=512, window_mb=128,
    )
    fake.queue_exec(
        lambda p: p["command"] == '"SYS:Tools/sandboxvm"',
        {"output": "banner", "exit_code": 5},
    )
    captured: dict[str, str] = {}

    def capture_run(p):
        if "hello" in p["command"]:
            captured["cmd"] = p["command"]
            return True
        return False

    fake.queue_exec(capture_run, {"output": "", "exit_code": 0})

    await sb.sandbox_run_guest(
        fleet, "tgt", guest="Tools:hello", deny_libs=["graphics.library"],
    )

    cmd = captured["cmd"]
    # Per-target default applied.
    assert "-m 512" in cmd
    assert "-w 128" in cmd
    # Per-target deny + per-call deny merged (target's first).
    assert "-x intuition.library" in cmd
    assert "-x graphics.library" in cmd
    # Quoted guest path survived intact.
    assert '"Tools:hello"' in cmd


@pytest.mark.asyncio
async def test_run_guest_args_appended_after_double_dash():
    fleet, fake = _make_available_fleet()
    fake.queue_exec(
        lambda p: p["command"] == '"SYS:Tools/sandboxvm"',
        {"output": "banner", "exit_code": 5},
    )
    captured: dict[str, str] = {}

    def cap(p):
        if "hello" in p["command"]:
            captured["cmd"] = p["command"]
            return True
        return False

    fake.queue_exec(cap, {"output": "", "exit_code": 0})

    await sb.sandbox_run_guest(
        fleet, "tgt", guest="Tools:hello",
        args=["one", "two with spaces", "three"],
    )

    cmd = captured["cmd"]
    # Order: <sandboxvm> ... <guest> -- one "two with spaces" three
    after_dd = cmd.split(" -- ", 1)[1]
    assert after_dd.startswith("one ")
    assert '"two with spaces"' in after_dd
    assert after_dd.endswith(" three")


@pytest.mark.asyncio
async def test_run_guest_pegasos2_raises_not_capable():
    fleet = Fleet(_make_config(machine="pegasos2"))
    fleet._mcpd["tgt"] = FakeMcpd()  # type: ignore[assignment]

    with pytest.raises(NotCapable):
        await sb.sandbox_run_guest(
            fleet, "tgt", guest="Tools:hello",
        )


@pytest.mark.asyncio
async def test_run_guest_missing_binary_raises_invalid_params(
    fleet_with_fake,
):
    fleet, _ = fleet_with_fake
    with pytest.raises(InvalidParams):
        await sb.sandbox_run_guest(
            fleet, "tgt", guest="Tools:hello",
        )


@pytest.mark.asyncio
async def test_run_guest_serialises_per_target():
    """Two concurrent run_guest calls on the same target should
    serialise: the per-target lock means the second waits for the
    first to finish."""
    fleet, fake = _make_available_fleet()
    fake.queue_exec(
        lambda p: p["command"] == '"SYS:Tools/sandboxvm"',
        {"output": "banner", "exit_code": 5},
    )

    started: list[float] = []
    finished: list[float] = []

    async def slow_request(method, params=None, timeout_s=30.0):
        # Patched for the actual guest exec only; everything else
        # falls through to the fake's normal handler.
        params = params or {}
        if method == "exec.cmd" and "guest" in params.get("command", ""):
            started.append(asyncio.get_event_loop().time())
            await asyncio.sleep(0.1)
            finished.append(asyncio.get_event_loop().time())
            return {"output": "", "exit_code": 0}
        return await orig_request(method, params, timeout_s)

    orig_request = fake.request
    fake.request = slow_request  # type: ignore[method-assign]

    a = sb.sandbox_run_guest(fleet, "tgt", guest="Tools:guestA",
                             name="guestA")
    b = sb.sandbox_run_guest(fleet, "tgt", guest="Tools:guestB",
                             name="guestB")
    await asyncio.gather(a, b)

    assert len(started) == 2
    assert len(finished) == 2
    # Second guest only starts after first finishes — proves the
    # per-target lock did its job.
    assert started[1] >= finished[0] - 1e-6
