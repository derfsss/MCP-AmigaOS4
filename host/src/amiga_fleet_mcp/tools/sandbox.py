"""sandbox.* — drive SandboxVM on AmigaOS 4 targets.

Phase A surface (this file): probe, deploy, run_guest. Each call goes
through `_ensure_available` first so a missing / broken / Pegasos2
target fails fast with a typed error, never half-runs.

Surface contract:

    sandbox.probe(target=None) -> SandboxProbeResult
        Eager probe. Path resolution + executable check + machine
        compatibility. Writes to the in-process cache (60 s TTL).

    sandbox.deploy(target=None, source=None, dest_path=None,
                   confirm=False) -> DeployResult
        Convenience wrapper around fs.upload that places the host's
        bin/sandboxvm onto the target's resolved path. Invalidates
        the probe cache for that target on success.

    sandbox.run_guest(target=None, guest=None, args=None,
                      extmem_mb=None, window_mb=None, deny_libs=None,
                      name=None, timeout_s=120) -> GuestRunResult
        Run one guest ELF inside SandboxVM, slurp its T:capture
        files, decode SandboxVM's exit-code convention into a
        structured result (trap_kind / trap_fingerprint).

Cross-references:

  - For arbitrary AmigaDOS commands without the SandboxVM harness:
    use `exec.cmd`.
  - For JSON-config-driven multi-step test bundles without
    SandboxVM: use `tests.run_suite`.
  - For the source upload step in isolation: `fs.upload`.

The full kernel debug ring + SandboxVM trap-line filtering arrive
in Phase B (`sys.debug_ring` + `sandbox.last_trap`).
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import time
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel, Field

from ..errors import InvalidParams, NotCapable, TargetError
from ..fleet import Fleet
from . import fs as fs_tool
from . import sys as sys_tool

# ---------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------

PROBE_CACHE_TTL_S = 60.0
"""How long a successful probe stays cached per target."""

DEFAULT_AOS_SEARCH = (
    "SYS:Tools/sandboxvm",
    "SYS:Utilities/sandboxvm",
    "C:sandboxvm",
)
"""Where `sandbox.probe` looks for sandboxvm when no per-target path is
configured. First `fs.stat` hit wins."""

DEFAULT_DEPLOY_PATH = "SYS:Tools/sandboxvm"
"""Where `sandbox.deploy` writes when no explicit `dest_path` is set
and no per-target `[targets.<name>.sandbox.path]` is configured."""

SANDBOXVM_UPSTREAM_URL = "https://github.com/derfsss/SandboxVM"
"""Upstream repo for SandboxVM. Surfaced in the SANDBOXVM_MISSING
hint so a fresh user has somewhere to go without grepping docs."""

SANDBOXVM_RELEASES_URL = (
    "https://github.com/derfsss/SandboxVM/releases/latest"
)
"""Pre-built sandboxvm binary download. Mentioned in the
SANDBOXVM_MISSING hint as the easy alternative to a from-source
build. Always points at the latest tagged release."""

# Machine identifiers that explicitly lack ExtMem and therefore can't
# host SandboxVM. Both `targets.<name>.machine` (QEMU) and
# `targets.<name>.tags` (real-hw) are checked.
INCOMPATIBLE_MACHINES = frozenset({"pegasos2"})

# SandboxVM's exit-code convention: trap_type negated. See
# upstream PROGRESS.md / README.md.
TRAP_KIND_BY_EXIT_CODE: dict[int, str] = {
    -0x300: "DSI",
    -0x400: "ISI",
    -0x600: "alignment",
    -0x700: "program",
    -0x800: "fp_unavailable",
}

# Regexes used to fingerprint specific trap shapes in the (rare) cases
# where the exit-code alone isn't enough. Currently only the upstream-
# documented kmod-libcall NULL+4 signature; more arrive in Phase B
# when sandbox.last_trap lands.
KMOD_LIBCALL_NULL4_HINTS = (
    "Traptype=0x300",
    "dar=0x4",
    "dsisr=0x00800000",
)


# ---------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------


class SandboxProbeResult(BaseModel):
    """Return shape for `sandbox.probe`."""

    target: str
    available: bool
    code: str | None = None
    """Error code when ``available is False``. One of
    ``SANDBOXVM_MISSING``, ``SANDBOXVM_BROKEN``,
    ``SANDBOXVM_INCOMPATIBLE_TARGET``, ``SANDBOXVM_TARGET_UNREACHABLE``.
    ``None`` when available."""
    path: str | None = None
    """Resolved on-target AOS path when available."""
    searched: list[str] = Field(default_factory=list)
    """Paths actually consulted by `fs.stat` (in order). Useful when
    diagnosing a missing-binary hit."""
    version_banner: str | None = None
    """First line of sandboxvm's stderr/stdout when invoked with no
    args (its usage banner). Acts as a coarse identity check. ``None``
    when the binary failed to start or wasn't found."""
    machine: str | None = None
    """The target's machine identifier (e.g. ``"X5000"``,
    ``"pegasos2"``) or ``None`` when the target config doesn't
    specify one."""
    machine_ok: bool | None = None
    """``False`` only when the machine is on
    `INCOMPATIBLE_MACHINES`. ``None`` when machine is unknown."""
    hint: str | None = None
    """Human-readable next-step suggestion when ``available is
    False``."""


class DeployResult(BaseModel):
    """Return shape for `sandbox.deploy`."""

    target: str
    source: str
    dest_path: str
    bytes_total: int
    elapsed_s: float
    sha256: str | None = None
    probe_after: SandboxProbeResult


class GuestRunResult(BaseModel):
    """Return shape for `sandbox.run_guest`."""

    target: str
    guest: str
    name: str
    command: str
    exit_code: int
    trap_kind: str | None = None
    trap_fingerprint: str | None = None
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    capture_paths: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------

# Probe cache: target -> (monotonic-expiry-seconds, last-result).
_PROBE_CACHE: dict[str, tuple[float, SandboxProbeResult]] = {}

# Per-target serialisation locks. Two simultaneous run_guest calls on
# the same target would step on each other's T:capture files; we
# serialise to keep things deterministic. Phase D is the place to add
# parallelism if a real workload needs it.
_TARGET_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for(target: str) -> asyncio.Lock:
    if target not in _TARGET_LOCKS:
        _TARGET_LOCKS[target] = asyncio.Lock()
    return _TARGET_LOCKS[target]


def _invalidate_probe(target: str) -> None:
    _PROBE_CACHE.pop(target, None)


def _resolve_target_machine(fleet: Fleet, target: str) -> str | None:
    """Return the canonical machine name for `target` (lowercased) or
    ``None`` when the config doesn't pin one. We consult both the
    explicit ``machine`` field (QEMU targets) and the ``tags`` list
    (real-hw targets carry e.g. ``["real-hw", "x5000", "p5020"]``)."""
    cfg = fleet.target_config(target)
    if cfg.machine:
        return cfg.machine.lower()
    for tag in cfg.tags:
        low = tag.lower()
        if low in INCOMPATIBLE_MACHINES:
            return low
        # Common machine tags surface here for the human-readable
        # `machine` field in the result.
        if low in {"x5000", "x1000", "a1222", "amigaone", "sam460ex"}:
            return low
    return None


def _candidate_target_paths(fleet: Fleet, target: str) -> list[str]:
    """Where to look for sandboxvm on the target."""
    out: list[str] = []
    cfg = fleet.target_config(target)
    if cfg.sandbox and cfg.sandbox.path:
        out.append(cfg.sandbox.path)
    out.extend(p for p in DEFAULT_AOS_SEARCH if p not in out)
    return out


# ---------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------


async def _execute_banner_probe(fleet: Fleet, target: str,
                                path: str) -> tuple[bool, str | None]:
    """Run sandboxvm with no args and confirm the binary actually
    starts.

    Returns ``(ran, banner_first_line)``:

      - ``ran=True`` when exec.cmd returned successfully, meaning
        the binary loaded and exited (with whatever rc). Banner
        text is best-effort: it'll be the first line of stdout when
        captured, otherwise None.
      - ``ran=False`` when exec.cmd raised (target unreachable,
        path not executable, hard timeout etc) -- treated as
        ``SANDBOXVM_BROKEN``.

    We deliberately don't gate on banner content. SandboxVM's usage
    banner is emitted via stdio (printf) but the daemon's exec.cmd
    capture pipe sometimes returns empty + truncated=true for
    clib4/newlib-linked binaries (stdio buffering not flushed before
    the process detaches from its Output() handle). Pinning the
    probe to "exec.cmd returned a structured exit code" rather than
    "exec.cmd captured non-empty output" makes the probe robust
    across that quirk."""
    mcpd = fleet.mcpd(target)
    try:
        raw = await mcpd.request(
            "exec.cmd",
            {
                "command": f'"{path}"',
                "timeout_ms": 5000,
            },
            timeout_s=10.0,
        )
    except Exception:
        return False, None
    output = str(raw.get("output", "")).strip()
    banner = output.splitlines()[0] if output else None
    return True, banner


async def sandbox_probe(fleet: Fleet, target: str) -> SandboxProbeResult:
    """Eager probe — bypass the cache. Three checks, in order:

      1. Target machine is compatible (not Pegasos2 / not on
         `INCOMPATIBLE_MACHINES`).
      2. sandboxvm binary is reachable on the target (`fs.stat` walks
         the search list).
      3. Binary actually starts and produces a banner.

    The return shape is deliberately verbose so an MCP client can
    decide what to do without re-probing."""
    machine = _resolve_target_machine(fleet, target)
    if machine in INCOMPATIBLE_MACHINES:
        result = SandboxProbeResult(
            target=target, available=False,
            code="SANDBOXVM_INCOMPATIBLE_TARGET",
            machine=machine, machine_ok=False,
            hint=(f"target machine {machine!r} lacks ExtMem; SandboxVM "
                  "refuses to run on it. Use a different target "
                  "(X5000 / A1222 / QEMU AmigaOne)."),
        )
        # Machine compatibility never changes for a target, so it's
        # safe to keep this result in the cache for the full TTL.
        _PROBE_CACHE[target] = (
            time.monotonic() + PROBE_CACHE_TTL_S, result,
        )
        return result

    machine_ok = None if machine is None else True
    candidates = _candidate_target_paths(fleet, target)
    found: str | None = None
    mcpd = fleet.mcpd(target)
    transport_error: Exception | None = None
    for path in candidates:
        try:
            await mcpd.request("fs.stat", {"path": path})
            found = path
            break
        except TargetError:
            # Path doesn't exist (or isn't stat-able) on the target.
            # Try the next candidate.
            continue
        except Exception as e:
            # Anything that isn't a TargetError is a transport-level
            # failure (target unreachable, connection refused,
            # asyncio.TimeoutError, etc.). Stop probing and surface
            # it as a distinct error code -- a missing binary needs
            # `sandbox.deploy`, an unreachable target needs power /
            # network attention first.
            transport_error = e
            break

    if transport_error is not None:
        return SandboxProbeResult(
            target=target, available=False,
            code="SANDBOXVM_TARGET_UNREACHABLE",
            searched=candidates,
            machine=machine, machine_ok=machine_ok,
            hint=(f"target {target!r} did not respond to fs.stat "
                  f"({type(transport_error).__name__}: "
                  f"{str(transport_error)[:120]}). Confirm the "
                  "target is powered on and MCPd is reachable "
                  "(`fleet.target_status`) before deploying."),
        )

    if found is None:
        result = SandboxProbeResult(
            target=target, available=False,
            code="SANDBOXVM_MISSING",
            searched=candidates,
            machine=machine, machine_ok=machine_ok,
            hint=("sandboxvm binary not found on target. Get it "
                  f"from {SANDBOXVM_RELEASES_URL} (pre-built PPC "
                  "AOS4 binary -- the fast path), or build from "
                  f"source at {SANDBOXVM_UPSTREAM_URL} (see its "
                  "README.md for the docker-cc cross-compile flow). "
                  "Then run `sandbox.deploy` to upload to the "
                  "target -- or set `[targets.<target>.sandbox.path]` "
                  "in config.toml if it's already installed at a "
                  "non-default location."),
        )
        # Negative result deliberately NOT cached — a follow-up
        # `sandbox.deploy` would otherwise have to explicitly
        # invalidate before subsequent calls work.
        return result

    ran, banner = await _execute_banner_probe(fleet, target, found)
    if not ran:
        result = SandboxProbeResult(
            target=target, available=False,
            code="SANDBOXVM_BROKEN",
            path=found, searched=candidates,
            machine=machine, machine_ok=machine_ok,
            hint=(f"sandboxvm at {found!r} did not start. Re-deploy "
                  "with `sandbox.deploy` (a corrupt upload is the "
                  "most common cause)."),
        )
        return result

    result = SandboxProbeResult(
        target=target, available=True,
        path=found, searched=candidates,
        version_banner=banner,
        machine=machine, machine_ok=machine_ok,
    )
    _PROBE_CACHE[target] = (
        time.monotonic() + PROBE_CACHE_TTL_S, result,
    )
    return result


async def _probe_specific_path(fleet: Fleet, target: str,
                               path: str) -> SandboxProbeResult:
    """Like `sandbox_probe` but pinned to a specific AOS path.

    Used by `sandbox_deploy` to verify the freshly-uploaded binary
    rather than re-walking the default search list — which can find
    a legacy binary at a higher-priority default location and
    silently shadow the deploy."""
    machine = _resolve_target_machine(fleet, target)
    if machine in INCOMPATIBLE_MACHINES:
        return SandboxProbeResult(
            target=target, available=False,
            code="SANDBOXVM_INCOMPATIBLE_TARGET",
            machine=machine, machine_ok=False,
        )
    machine_ok = None if machine is None else True

    mcpd = fleet.mcpd(target)
    try:
        await mcpd.request("fs.stat", {"path": path})
    except TargetError:
        return SandboxProbeResult(
            target=target, available=False,
            code="SANDBOXVM_MISSING",
            searched=[path],
            machine=machine, machine_ok=machine_ok,
        )
    except Exception as e:
        return SandboxProbeResult(
            target=target, available=False,
            code="SANDBOXVM_TARGET_UNREACHABLE",
            searched=[path],
            machine=machine, machine_ok=machine_ok,
            hint=(f"target {target!r} did not respond to fs.stat "
                  f"({type(e).__name__}: {str(e)[:120]})."),
        )

    ran, banner = await _execute_banner_probe(fleet, target, path)
    if not ran:
        return SandboxProbeResult(
            target=target, available=False,
            code="SANDBOXVM_BROKEN",
            path=path, searched=[path],
            machine=machine, machine_ok=machine_ok,
        )

    return SandboxProbeResult(
        target=target, available=True,
        path=path, searched=[path],
        version_banner=banner,
        machine=machine, machine_ok=machine_ok,
    )


async def _ensure_available(fleet: Fleet, target: str) -> SandboxProbeResult:
    """Cached probe gate used by every public sandbox.* tool except
    `sandbox.probe` itself. Hits the in-process cache first; on miss
    or expiry runs the full probe.

    Raises a typed error when the target isn't usable, so callers can
    skip the "did probe pass?" boilerplate."""
    cached = _PROBE_CACHE.get(target)
    now = time.monotonic()
    if cached is not None and cached[0] > now and cached[1].available:
        return cached[1]
    res = await sandbox_probe(fleet, target)
    if not res.available:
        if res.code == "SANDBOXVM_INCOMPATIBLE_TARGET":
            raise NotCapable(
                res.hint or "target incompatible with SandboxVM",
                data={"code": res.code, "target": target,
                      "machine": res.machine},
            )
        if res.code == "SANDBOXVM_TARGET_UNREACHABLE":
            raise NotCapable(
                res.hint or "target unreachable",
                data={"code": res.code, "target": target},
            )
        raise InvalidParams(
            res.hint or "sandboxvm not available on target",
            data={"code": res.code, "target": target,
                  "searched": res.searched},
        )
    return res


# ---------------------------------------------------------------------
# Deploy
# ---------------------------------------------------------------------


def _resolve_source(source: str | None,
                    config_default: Path | None) -> Path:
    """Pick the host-side sandboxvm binary to upload.

    Order:
      1. explicit `source` arg
      2. `[paths] sandboxvm` in config.toml

    No magic search list — keep the resolution explicit and easy to
    audit. The user wires it once in config.toml or passes it on the
    call."""
    if source:
        p = Path(source).expanduser().resolve()
        if not p.is_file():
            raise InvalidParams(
                f"sandbox.deploy source not found: {source!r}",
            )
        return p
    if config_default is not None:
        p = Path(config_default).expanduser().resolve()
        if not p.is_file():
            raise InvalidParams(
                "sandbox.deploy: [paths] sandboxvm points at "
                f"{config_default!s} but no file is there. Update "
                "config.toml or pass `source` explicitly.",
            )
        return p
    raise InvalidParams(
        "sandbox.deploy: no source given and [paths] sandboxvm is "
        "not set. Pass `source=...` or add the path to config.toml.",
    )


def _resolve_dest_path(fleet: Fleet, target: str,
                       dest_path: str | None) -> str:
    if dest_path:
        return dest_path
    cfg = fleet.target_config(target)
    if cfg.sandbox and cfg.sandbox.path:
        return cfg.sandbox.path
    return DEFAULT_DEPLOY_PATH


async def sandbox_deploy(
    fleet: Fleet, target: str, *,
    source: str | None = None,
    dest_path: str | None = None,
    confirm: bool = False,
) -> DeployResult:
    """Upload sandboxvm to a target's tools dir.

    Convenience wrapper around `fs.upload`; the only material logic
    here is source/dest resolution and probe-cache invalidation.

    Mutating: requires ``confirm=True`` (overwrites the on-target
    binary). The probe cache for `target` is cleared on success so
    the next sandbox call re-validates against the freshly-deployed
    file."""
    if not confirm:
        raise InvalidParams(
            "sandbox.deploy requires confirm=True (overwrites the "
            "on-target sandboxvm binary).",
        )

    src = _resolve_source(source, fleet.config.paths.sandboxvm)
    dst = _resolve_dest_path(fleet, target, dest_path)

    # Make sure the parent dir exists. AmigaDOS Copy + fs.write_chunk
    # both refuse to create intermediate dirs, so do it ourselves
    # idempotently. Strip any trailing slash and split off the file
    # part. AOS paths use `/` as separator within an assign and `:`
    # for the assign itself; we only need to handle the in-assign
    # part.
    parent = _aos_parent_dir(dst)
    if parent:
        try:
            await fleet.mcpd(target).request(
                "exec.cmd",
                {"command": f'MakeDir >NIL: "{parent}"',
                 "timeout_ms": 5000},
                timeout_s=10.0,
            )
        except Exception:
            # Already exists or unwritable — let fs.upload surface the
            # real failure rather than blocking on the makedir.
            pass

    upload = await fs_tool.fs_upload(
        fleet, target,
        local_path=str(src),
        remote_path=dst,
        compression="auto",
        verify=True,
    )

    _invalidate_probe(target)
    probe_after = await _probe_specific_path(fleet, target, dst)
    # Cache the path-pinned result so the next sandbox.* call doesn't
    # re-walk the search list and risk picking up a legacy binary at
    # a higher-priority default location.
    if probe_after.available:
        _PROBE_CACHE[target] = (
            time.monotonic() + PROBE_CACHE_TTL_S, probe_after,
        )

    return DeployResult(
        target=target,
        source=str(src),
        dest_path=dst,
        bytes_total=upload.bytes_total,
        elapsed_s=upload.elapsed_s,
        sha256=upload.sha256,
        probe_after=probe_after,
    )


def _aos_parent_dir(path: str) -> str | None:
    """Return the parent directory of an AOS path, or None when
    `path` is just an assign root (e.g. ``"SYS:"``).

    Examples:
      ``"SYS:Tools/sandboxvm"`` -> ``"SYS:Tools"``
      ``"Tools:bin/foo"`` -> ``"Tools:bin"``
      ``"SYS:foo"`` -> ``None`` (parent is the assign root)
      ``"SYS:"`` -> ``None``
    """
    # Split off the assign prefix (everything up to and including ":").
    if ":" not in path:
        return None
    assign, _, rest = path.partition(":")
    if not rest:
        return None
    parts = rest.rstrip("/").split("/")
    if len(parts) <= 1:
        return None
    return f"{assign}:" + "/".join(parts[:-1])


# ---------------------------------------------------------------------
# Run guest
# ---------------------------------------------------------------------


def _derive_guest_name(guest: str) -> str:
    """Default `name` for a guest run: basename minus extension."""
    # Strip everything before the last `/` (AOS path) or `:` (assign).
    base = guest
    for sep in ("/", ":"):
        if sep in base:
            base = base.rsplit(sep, 1)[1]
    if "." in base:
        base = base.rsplit(".", 1)[0]
    return base or "guest"


def _build_command(sandboxvm_path: str, *,
                   guest: str,
                   args: Sequence[str] | None,
                   extmem_mb: int,
                   window_mb: int,
                   deny_libs: Sequence[str],
                   name: str,
                   resident_driver: bool = False,
                   test_elf: str | None = None) -> str:
    """Construct the AmigaDOS command line for one guest run.

    Plain guest mode:
        "<sandboxvm>" -m <MB> -w <MB> -n <name> [-x lib]... <guest> [-- args...]

    Resident-driver mode (`resident_driver=True`):
        "<sandboxvm>" -m <MB> -w <MB> -n <name> [-x lib]...
                      -r <guest> [-t <test>] [-- args...]

    `test_elf` is only meaningful with `resident_driver=True`; the
    caller is responsible for raising before this point if both
    aren't set together.

    Quoting note: AmigaDOS quoting is conservative. We wrap the
    sandboxvm path and the guest path in double quotes so spaces /
    colons survive; the daemon's `exec.cmd` handler is happy with
    that. We don't quote -m / -w / -n values because they're integers
    or simple identifiers."""
    parts: list[str] = [f'"{sandboxvm_path}"']
    parts.extend(["-m", str(extmem_mb)])
    parts.extend(["-w", str(window_mb)])
    parts.extend(["-n", _shell_safe(name)])
    for lib in deny_libs:
        parts.extend(["-x", _shell_safe(lib)])
    if resident_driver:
        parts.append("-r")
    if test_elf:
        parts.extend(["-t", f'"{test_elf}"'])
    parts.append(f'"{guest}"')
    if args:
        parts.append("--")
        parts.extend(_shell_safe(a) for a in args)
    return " ".join(parts)


def _shell_safe(token: str) -> str:
    """Wrap a token in double quotes when it contains whitespace or
    AmigaDOS metacharacters; otherwise return as-is. Daemon-side
    quoting handles the bytes correctly either way; this just keeps
    the command readable in logs."""
    if not token:
        return '""'
    needs_quoting = any(c in token for c in ' \t"<>|;')
    if needs_quoting:
        # Escape embedded quotes by AmigaDOS convention (`*"`).
        escaped = token.replace('"', '*"')
        return f'"{escaped}"'
    return token


def _classify_exit(exit_code: int,
                   stderr: str) -> tuple[str | None, str | None]:
    """Decode SandboxVM's exit code into a (trap_kind, fingerprint)
    pair. Either or both may be ``None``.

    Trap kinds come from `TRAP_KIND_BY_EXIT_CODE` (the upstream
    convention: trap_type negated and returned as the rc).

    Fingerprints are best-effort string-match against the trap
    output; the only one currently recognised is the documented
    kmod-libcall NULL+4 signature for `HD_SCSICMD via DoIO`. Phase B's
    `sandbox.last_trap` adds debug-ring-based fingerprints for
    cases where stderr alone doesn't carry the signature."""
    trap_kind = TRAP_KIND_BY_EXIT_CODE.get(exit_code)
    fingerprint: str | None = None
    if all(h in stderr for h in KMOD_LIBCALL_NULL4_HINTS):
        fingerprint = "kmod_libcall_null4"
    return trap_kind, fingerprint


async def _read_capture(fleet: Fleet, target: str,
                        path: str) -> tuple[str, bool]:
    """fs.read a T:capture file and decode to text. Returns
    ``(decoded, present)``: when the file doesn't exist we return
    ``("", False)`` so the caller can distinguish missing-on-purpose
    (guest never wrote anything) from genuine read failures."""
    try:
        raw = await fleet.mcpd(target).request("fs.read", {"path": path})
    except TargetError:
        return "", False
    content_b64 = raw.get("content_b64", "")
    if not content_b64:
        return "", True
    try:
        decoded = base64.b64decode(content_b64).decode(
            "utf-8", errors="replace",
        )
    except Exception:
        decoded = ""
    return decoded, True


async def _delete_capture(fleet: Fleet, target: str, path: str) -> None:
    """Best-effort cleanup of a T:capture file. Failures are
    swallowed — leaving stale capture files in T: is mildly ugly
    but never load-bearing."""
    try:
        await fleet.mcpd(target).request("fs.delete", {"path": path})
    except Exception:
        pass


async def _run_sandboxvm(
    fleet: Fleet, target: str, *,
    guest: str,
    args: Sequence[str] | None,
    extmem_mb: int | None,
    window_mb: int | None,
    deny_libs: Sequence[str] | None,
    name: str | None,
    timeout_s: float,
    resident_driver: bool = False,
    test_elf: str | None = None,
) -> GuestRunResult:
    """Shared core for `sandbox_run_guest` and `sandbox_run_driver`.

    Performs the probe gate, resolves config-driven defaults, builds
    the command, runs it under the per-target lock, and slurps +
    classifies the T:capture files."""
    probe = await _ensure_available(fleet, target)
    assert probe.path is not None  # guaranteed by _ensure_available

    # Resolve config-driven defaults.
    cfg = fleet.target_config(target).sandbox
    if extmem_mb is None:
        extmem_mb = cfg.default_extmem_mb if cfg else 1024
    if window_mb is None:
        window_mb = cfg.default_window_mb if cfg else 256

    merged_deny: list[str] = []
    if cfg and cfg.deny_libs:
        merged_deny.extend(cfg.deny_libs)
    if deny_libs:
        for lib in deny_libs:
            if lib not in merged_deny:
                merged_deny.append(lib)

    run_name = name or _derive_guest_name(guest)
    out_path = f"T:sandboxvm-{run_name}.out"
    err_path = f"T:sandboxvm-{run_name}.err"

    cmd = _build_command(
        probe.path,
        guest=guest,
        args=args,
        extmem_mb=extmem_mb,
        window_mb=window_mb,
        deny_libs=merged_deny,
        name=run_name,
        resident_driver=resident_driver,
        test_elf=test_elf,
    )

    # Per-target lock so concurrent run_guest calls on the same
    # target serialise instead of racing the T:capture files.
    lock = _lock_for(target)
    t0 = time.monotonic()
    async with lock:
        mcpd = fleet.mcpd(target)
        try:
            raw = await mcpd.request(
                "exec.cmd",
                {
                    "command": cmd,
                    "timeout_ms": int(timeout_s * 1000),
                },
                timeout_s=max(timeout_s + 5.0, 30.0),
            )
        except Exception:
            # Try to slurp captures anyway — guest may have written
            # something before the timeout fired.
            stdout, _ = await _read_capture(fleet, target, out_path)
            stderr, _ = await _read_capture(fleet, target, err_path)
            await _delete_capture(fleet, target, out_path)
            await _delete_capture(fleet, target, err_path)
            raise

        exit_code = int(raw.get("exit_code", 0))
        # Daemon-side `exec.cmd` returns its own captured stdout, but
        # SandboxVM's per-guest output goes via the T:files. Combine:
        # daemon-output is the SandboxVM host wrapper's chatter (and
        # any banners), the T:files carry the guest itself.
        wrapper_output = str(raw.get("output", ""))
        stdout, stdout_present = await _read_capture(
            fleet, target, out_path,
        )
        stderr, stderr_present = await _read_capture(
            fleet, target, err_path,
        )
        await _delete_capture(fleet, target, out_path)
        await _delete_capture(fleet, target, err_path)

    # Wrapper output lands in stdout when no T:.out captured anything,
    # in stderr when no T:.err did. Avoids losing useful diagnostics
    # like "guest [0] returned -768" emitted by SandboxVM itself.
    if wrapper_output and not (stdout_present or stderr_present):
        stdout = wrapper_output

    trap_kind, fingerprint = _classify_exit(exit_code, stderr)

    capture_paths: list[str] = []
    if stdout_present:
        capture_paths.append(out_path)
    if stderr_present:
        capture_paths.append(err_path)

    return GuestRunResult(
        target=target,
        guest=guest,
        name=run_name,
        command=cmd,
        exit_code=exit_code,
        trap_kind=trap_kind,
        trap_fingerprint=fingerprint,
        stdout=stdout,
        stderr=stderr,
        duration_s=time.monotonic() - t0,
        capture_paths=capture_paths,
    )


async def sandbox_run_guest(
    fleet: Fleet, target: str, *,
    guest: str,
    args: Sequence[str] | None = None,
    extmem_mb: int | None = None,
    window_mb: int | None = None,
    deny_libs: Sequence[str] | None = None,
    name: str | None = None,
    timeout_s: float = 120.0,
) -> GuestRunResult:
    """Run a single guest ELF inside SandboxVM on `target`.

    Probe-gated: a missing / broken / Pegasos2 target raises a typed
    error before any work happens.

    Args:
        guest: AOS path to the guest ELF (e.g.
            ``"Tools:tests/clib4hello"``).
        args: Optional list of strings appended via ``--``.
        extmem_mb / window_mb: SandboxVM ``-m`` / ``-w`` values.
            Defaults come from
            ``[targets.<target>.sandbox.default_*]`` (1024 / 256
            when unset).
        deny_libs: Per-call ``-x`` flags. Merged with any
            ``[targets.<target>.sandbox.deny_libs]`` from config.
        name: Used as ``-n`` (also drives the T:capture filenames).
            Defaults to the basename of ``guest`` minus extension.
        timeout_s: Host-side request timeout for the run."""
    return await _run_sandboxvm(
        fleet, target,
        guest=guest, args=args,
        extmem_mb=extmem_mb, window_mb=window_mb,
        deny_libs=deny_libs, name=name, timeout_s=timeout_s,
        resident_driver=False, test_elf=None,
    )


async def sandbox_run_driver(
    fleet: Fleet, target: str, *,
    driver: str,
    test: str | None = None,
    args: Sequence[str] | None = None,
    extmem_mb: int | None = None,
    window_mb: int | None = None,
    deny_libs: Sequence[str] | None = None,
    name: str | None = None,
    timeout_s: float = 120.0,
) -> GuestRunResult:
    """Load a driver via SandboxVM's resident-driver mode (`-r`).

    SandboxVM scans the driver's RTF_AUTOINIT Resident, applies PPC
    ELF relocations, and calls the CLT_InitFunc with the sandboxed
    IExec. When `test` is set, after a successful init SandboxVM
    runs `test` as a follow-on guest in the same Guest context so
    `OpenLibrary(<driver-name>)` from the test resolves via the
    resident-lib registry.

    Probe-gated, lock-serialised, T:-capture-slurped — same shape as
    `sandbox.run_guest`. The result's `guest` field carries the
    driver path (the thing being loaded).

    Args:
        driver: AOS path to the .device or .library being loaded.
        test: Optional AOS path to a follow-on guest ELF. Requires
            the driver init to succeed first; SandboxVM aborts if
            the resident-init returns non-zero.
        args: Optional list of strings appended via ``--`` (only
            meaningful with `test`).
        extmem_mb / window_mb / deny_libs / name / timeout_s: Same
            semantics as `sandbox.run_guest`."""
    return await _run_sandboxvm(
        fleet, target,
        guest=driver, args=args,
        extmem_mb=extmem_mb, window_mb=window_mb,
        deny_libs=deny_libs, name=name, timeout_s=timeout_s,
        resident_driver=True, test_elf=test,
    )


# ---------------------------------------------------------------------
# last_trap — filter sys.debug_ring for SandboxVM trap signatures
# ---------------------------------------------------------------------


class LastTrapResult(BaseModel):
    """Return shape for `sandbox.last_trap`."""

    target: str
    found: bool
    trap_kind: str | None = None
    """One of DSI / ISI / alignment / program / fp_unavailable when
    the ring contained a recognised trap line."""
    fingerprint: str | None = None
    """Specific shape match — currently only ``kmod_libcall_null4``
    (the documented HD_SCSICMD-via-DoIO limitation)."""
    traptype_hex: str | None = None
    """Raw trap_type as hex (e.g. ``"0x300"``) when extracted from
    the ring text."""
    raw_lines: list[str] = Field(default_factory=list)
    """The matching ring lines, in order. Empty when ``found`` is
    False."""
    captured_at: str | None = None
    """ISO-8601 host timestamp from the underlying sys.debug_ring
    call. ``None`` when no debug ring was captured (e.g. retry
    exhausted)."""
    attempts: int = 0
    """Number of sys.debug_ring calls made before finding a trap.
    Useful for diagnosing the ring-write race."""


# Lines emitted by SandboxVM's tc_TrapCode trampoline carry an
# explicit trap-type prefix. We match a generous regex so upstream
# can change the exact prose without breaking the filter.
_RE_TRAPTYPE = re.compile(r"(?i)\btrap[_ ]?type\s*=\s*0x([0-9a-f]+)")
_RE_SANDBOXVM_LINE = re.compile(r"(?i)\[sandbox(vm)?\]")
_RE_TRAP_LINE = re.compile(
    r"(?i)\b(trap|dsi|isi|alignment|illegal|privilege|fault)\b"
)

# Map raw traptype hex (lower, no 0x) to a kind string.
_TRAP_KIND_BY_HEX: dict[str, str] = {
    "300": "DSI",
    "400": "ISI",
    "600": "alignment",
    "700": "program",
    "800": "fp_unavailable",
}

# Kmod-libcall NULL+4 fingerprint hints — same set as `_classify_exit`
# but applied to ring text rather than guest stderr. All three must
# appear within the candidate trap window.
_KMOD_HINTS = ("Traptype=0x300", "dar=0x4", "dsisr=0x00800000")


def _extract_trap(lines: Sequence[str]) -> tuple[
    str | None, str | None, str | None, list[str],
]:
    """Walk `lines` and return (trap_kind, fingerprint,
    traptype_hex, matching_lines).

    A trap is anchored by either a `traptype=` hex value or a hard
    trap keyword (`dsi`, `isi`, `alignment`, `illegal`, `privilege`,
    `fault`, `trap`). A bare `[sandboxvm]` prefix on its own is not
    enough — SandboxVM emits routine status lines too, and we don't
    want to count "guest 0 ready" as a trap.

    Once an anchor is found we expand it backwards (and forwards)
    over adjacent [sandboxvm]-prefixed or anchor-matching lines so
    the caller gets the surrounding dump (registers, dsisr, dar,
    callsite hints) rather than just the headline."""
    if not lines:
        return None, None, None, []

    def _is_anchor(line: str) -> bool:
        return bool(
            _RE_TRAPTYPE.search(line) or _RE_TRAP_LINE.search(line),
        )

    def _is_block_member(line: str) -> bool:
        return bool(
            _is_anchor(line) or _RE_SANDBOXVM_LINE.search(line),
        )

    # Walk from the end backwards to find the last anchor.
    last_trap_idx: int | None = None
    for i in range(len(lines) - 1, -1, -1):
        if _is_anchor(lines[i]):
            last_trap_idx = i
            break
    if last_trap_idx is None:
        return None, None, None, []

    # Expand backwards over [sandboxvm] / anchor lines for context.
    start = last_trap_idx
    while start > 0 and _is_block_member(lines[start - 1]):
        start -= 1

    # Expand forwards similarly so a multi-line trap dump (registers,
    # callsite, etc.) all lands in the block.
    end = last_trap_idx
    while end + 1 < len(lines) and _is_block_member(lines[end + 1]):
        end += 1

    block = list(lines[start:end + 1])
    block_text = "\n".join(block)

    # Extract first traptype hex in the block — the trampoline emits
    # the trap_type field on the first line of its dump.
    trap_kind: str | None = None
    traptype_hex: str | None = None
    m = _RE_TRAPTYPE.search(block_text)
    if m:
        hex_lower = m.group(1).lower().lstrip("0") or "0"
        traptype_hex = f"0x{hex_lower}"
        trap_kind = _TRAP_KIND_BY_HEX.get(hex_lower)

    fingerprint: str | None = None
    if all(h in block_text for h in _KMOD_HINTS):
        fingerprint = "kmod_libcall_null4"

    return trap_kind, fingerprint, traptype_hex, block


async def sandbox_last_trap(
    fleet: Fleet, target: str, *,
    since_s: float = 60.0,
    max_lines: int = 500,
    retry_count: int = 3,
    retry_delay_s: float = 0.2,
) -> LastTrapResult:
    """Filter the kernel debug ring for SandboxVM trap fingerprints.

    Calls `sys.debug_ring` with a small retry loop (default 3 tries,
    200 ms apart) because the kernel ring write is asynchronous to
    SandboxVM exit — a `last_trap` immediately after `run_guest`
    can race the trap line into the ring. Each retry re-reads the
    full ring and re-scans for trap markers.

    `since_s` and `max_lines` are forwarded to `sys.debug_ring`.

    Returns an empty `found=False` result when no trap signature is
    visible after all retries — that's the genuine "no trap to
    report" case, not an error."""
    if retry_count < 1:
        retry_count = 1
    if retry_delay_s < 0:
        retry_delay_s = 0.0

    last_lines: list[str] = []
    last_captured_at: str | None = None
    attempts = 0
    for attempt in range(retry_count):
        attempts = attempt + 1
        ring = await sys_tool.sys_debug_ring(
            fleet, target,
            since_s=since_s, max_lines=max_lines,
        )
        last_lines = list(ring.lines)
        last_captured_at = ring.captured_at
        kind, fp, hex_str, block = _extract_trap(last_lines)
        if block:
            return LastTrapResult(
                target=target,
                found=True,
                trap_kind=kind,
                fingerprint=fp,
                traptype_hex=hex_str,
                raw_lines=block,
                captured_at=ring.captured_at,
                attempts=attempts,
            )
        if attempt + 1 < retry_count:
            await asyncio.sleep(retry_delay_s)

    return LastTrapResult(
        target=target,
        found=False,
        captured_at=last_captured_at,
        attempts=attempts,
    )


# ---------------------------------------------------------------------
# run_batch — multiple guests in one sandboxvm invocation
# ---------------------------------------------------------------------

SANDBOXVM_MAX_GUESTS = 16
"""Upstream SandboxVM caps a single invocation at 16 guests
(``src/main.c:SANDBOXVM_MAX_GUESTS``). We surface the same limit
rather than launching multiple sandboxvm processes — the
single-process flow has been validated end-to-end on real X5000."""


class BatchGuestSpec(BaseModel):
    """One entry in a `sandbox.run_batch` request."""

    guest: str
    """AOS path to the guest ELF."""
    name: str | None = None
    """Optional override for this guest's name. When None, the
    batch name is suffixed with ``.<idx>`` (mirrors SandboxVM's
    own per-guest naming convention so the upstream
    `T:sandboxvm-<name>.<idx>.{out,err}` capture files line up)."""


class BatchEntryResult(BaseModel):
    """Per-guest sub-result inside a `BatchRunResult.entries` list."""

    guest: str
    name: str
    exit_code: int
    trap_kind: str | None = None
    trap_fingerprint: str | None = None
    stdout: str = ""
    stderr: str = ""
    capture_paths: list[str] = Field(default_factory=list)


class BatchRunResult(BaseModel):
    """Return shape for `sandbox.run_batch`."""

    target: str
    batch_name: str
    command: str
    """The single sandboxvm invocation that ran the whole batch."""
    aggregate_exit_code: int
    """Mirrors SandboxVM's ``last_nonzero`` semantics: 0 when every
    guest returned 0, otherwise the last non-zero rc seen. Useful as
    a quick "did the whole batch pass?" check."""
    all_clean: bool
    """Convenience: ``aggregate_exit_code == 0``."""
    duration_s: float
    entries: list[BatchEntryResult]


# Regex for parsing per-guest exit codes from the debug ring. The
# format is `[sandboxvm] guest_run_elf <path> returned <rc>; calling
# guest_destroy` (per src/main.c). Resident-mode invocations are
# single-guest only and therefore not run via run_batch, so this one
# pattern covers the whole batch flow.
_RE_GUEST_EXIT = re.compile(
    r"\[sandboxvm\]\s+guest_run_elf\s+(\S+)\s+returned\s+(-?\d+)",
)


def _parse_per_guest_exits(
    ring_lines: Sequence[str], expected: int,
) -> list[tuple[str, int]]:
    """Walk `ring_lines` and return the most recent `expected`
    matches of the per-guest exit format, preserving order
    (oldest-first within the trailing window).

    Returns a list of (path, rc) pairs. May be shorter than
    `expected` if the ring rotated past the older guests before we
    could capture it -- the caller pads with a sentinel rc."""
    matches: list[tuple[str, int]] = []
    for line in ring_lines:
        m = _RE_GUEST_EXIT.search(line)
        if m:
            matches.append((m.group(1), int(m.group(2))))
    return matches[-expected:] if expected > 0 else []


def _build_batch_command(sandboxvm_path: str, *,
                         guests: Sequence[str],
                         extmem_mb: int,
                         window_mb: int,
                         deny_libs: Sequence[str],
                         name: str) -> str:
    """Construct the AmigaDOS command line for a multi-guest run.

    Format:
        "<sandboxvm>" -m <MB> -w <MB> -n <name> [-x lib]... <g0> <g1> ...

    Multi-guest mode forbids ``--``-style argv passing (SandboxVM
    enforces this in src/main.c) so per-guest args aren't part of
    the contract here. Use `sandbox.run_guest` when you need argv."""
    parts: list[str] = [f'"{sandboxvm_path}"']
    parts.extend(["-m", str(extmem_mb)])
    parts.extend(["-w", str(window_mb)])
    parts.extend(["-n", _shell_safe(name)])
    for lib in deny_libs:
        parts.extend(["-x", _shell_safe(lib)])
    for g in guests:
        parts.append(f'"{g}"')
    return " ".join(parts)


async def sandbox_run_batch(
    fleet: Fleet, target: str, *,
    guests: Sequence[BatchGuestSpec | dict],
    extmem_mb: int | None = None,
    window_mb: int | None = None,
    deny_libs: Sequence[str] | None = None,
    name: str | None = None,
    timeout_s: float = 600.0,
) -> BatchRunResult:
    """Run up to 16 guests sequentially inside a single SandboxVM
    invocation.

    Probe-gated and lock-serialised like `run_guest` / `run_driver`.
    Per-guest argv is **not** supported (SandboxVM forbids ``--``
    with multi-guest); for that, call `sandbox.run_guest` per
    binary. Per-guest deny-lists also aren't supported — `deny_libs`
    is a global list applied to every guest in the batch (mirrors
    SandboxVM's ``-x`` semantics).

    Per-guest exit codes are parsed from the kernel debug ring
    (``[sandboxvm] guest_run_elf <path> returned <rc>``). When the
    ring rolls past older entries before we capture it, missing
    per-guest results are reported as ``exit_code=0`` with
    ``stdout=""`` -- the aggregate ``aggregate_exit_code`` is still
    correct because SandboxVM itself returns the last non-zero rc.

    Args:
        guests: List of BatchGuestSpec entries (or equivalent dicts
            via Pydantic coercion). Min 1, max 16.
        extmem_mb / window_mb / deny_libs / name: Same semantics as
            `sandbox.run_guest`. Apply globally to the whole batch.
        timeout_s: Host-side request timeout. Higher default than
            run_guest (600 s vs 120 s) because a 16-guest batch can
            comfortably exceed the single-guest budget.

    For JSON-config-driven multi-step bundles without the SandboxVM
    harness, use ``tests.run_suite`` instead."""
    if not guests:
        raise InvalidParams(
            "sandbox.run_batch requires at least one guest entry",
        )
    if len(guests) > SANDBOXVM_MAX_GUESTS:
        raise InvalidParams(
            f"sandbox.run_batch supports at most "
            f"{SANDBOXVM_MAX_GUESTS} guests per invocation; "
            f"got {len(guests)}",
            data={"max": SANDBOXVM_MAX_GUESTS, "got": len(guests)},
        )

    specs: list[BatchGuestSpec] = [
        g if isinstance(g, BatchGuestSpec) else BatchGuestSpec(**g)
        for g in guests
    ]

    probe = await _ensure_available(fleet, target)
    assert probe.path is not None

    cfg = fleet.target_config(target).sandbox
    if extmem_mb is None:
        extmem_mb = cfg.default_extmem_mb if cfg else 1024
    if window_mb is None:
        window_mb = cfg.default_window_mb if cfg else 256

    merged_deny: list[str] = []
    if cfg and cfg.deny_libs:
        merged_deny.extend(cfg.deny_libs)
    if deny_libs:
        for lib in deny_libs:
            if lib not in merged_deny:
                merged_deny.append(lib)

    # Batch name. Single base name; per-guest names get `.<idx>`
    # appended by SandboxVM itself (we mirror that for capture-file
    # path resolution).
    batch_name = name or "batch"
    per_guest_names = [
        s.name if s.name else f"{batch_name}.{i}"
        for i, s in enumerate(specs)
    ]

    cmd = _build_batch_command(
        probe.path,
        guests=[s.guest for s in specs],
        extmem_mb=extmem_mb,
        window_mb=window_mb,
        deny_libs=merged_deny,
        name=batch_name,
    )

    lock = _lock_for(target)
    t0 = time.monotonic()
    async with lock:
        mcpd = fleet.mcpd(target)
        raw = await mcpd.request(
            "exec.cmd",
            {
                "command": cmd,
                "timeout_ms": int(timeout_s * 1000),
            },
            timeout_s=max(timeout_s + 5.0, 30.0),
        )
        aggregate = int(raw.get("exit_code", 0))

        # Read the kernel ring after the run to recover per-guest
        # exit codes. We ask for a generous max_lines so a 16-guest
        # batch's worth of "returned" lines + per-guest preludes all
        # fit in the captured window.
        ring = await sys_tool.sys_debug_ring(
            fleet, target,
            since_s=60.0,
            max_lines=2000,
        )
        per_guest_exits = _parse_per_guest_exits(ring.lines, len(specs))

        # Per-guest captures. Names follow SandboxVM's
        # `T:sandboxvm-<gname>.{out,err}` convention.
        entries: list[BatchEntryResult] = []
        for idx, spec in enumerate(specs):
            gname = per_guest_names[idx]
            out_path = f"T:sandboxvm-{gname}.out"
            err_path = f"T:sandboxvm-{gname}.err"
            stdout, stdout_present = await _read_capture(
                fleet, target, out_path,
            )
            stderr, stderr_present = await _read_capture(
                fleet, target, err_path,
            )
            await _delete_capture(fleet, target, out_path)
            await _delete_capture(fleet, target, err_path)

            # Match per-guest exit by ordinal — the ring entries we
            # parsed are in batch order, but if the ring rotated
            # past older guests, the K we captured map to the LAST
            # K guests in the batch (oldest got dropped). Compute
            # an alignment offset accordingly. Aggregate exit_code
            # already covers the "did anything fail" question; the
            # default-to-0 for missing entries is purely cosmetic.
            ring_offset = len(specs) - len(per_guest_exits)
            if idx >= ring_offset:
                exit_code = per_guest_exits[idx - ring_offset][1]
            else:
                exit_code = 0

            trap_kind, fingerprint = _classify_exit(exit_code, stderr)

            capture_paths: list[str] = []
            if stdout_present:
                capture_paths.append(out_path)
            if stderr_present:
                capture_paths.append(err_path)

            entries.append(BatchEntryResult(
                guest=spec.guest,
                name=gname,
                exit_code=exit_code,
                trap_kind=trap_kind,
                trap_fingerprint=fingerprint,
                stdout=stdout,
                stderr=stderr,
                capture_paths=capture_paths,
            ))

    return BatchRunResult(
        target=target,
        batch_name=batch_name,
        command=cmd,
        aggregate_exit_code=aggregate,
        all_clean=(aggregate == 0
                   and all(e.exit_code == 0 for e in entries)),
        duration_s=time.monotonic() - t0,
        entries=entries,
    )


# ---------------------------------------------------------------------
# Test-only helpers
# ---------------------------------------------------------------------


def _reset_module_state_for_tests() -> None:
    """Clear caches/locks. Called by the test suite between
    parametrised runs so probe results from one test don't leak
    into the next. Not part of the public surface."""
    _PROBE_CACHE.clear()
    _TARGET_LOCKS.clear()


# Suppress unused-import warning when the file is loaded in unusual
# contexts (e.g. module import tests).
_ = os
