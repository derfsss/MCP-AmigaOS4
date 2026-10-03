"""Config layer.

Loads `config.toml` (path overridable via AMIGA_FLEET_CONFIG) and
validates with Pydantic. Unknown fields are ignored so older
configs keep working as the schema grows.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    log_level: Literal["debug", "info", "warn", "error"] = "info"
    log_dir: Path = Path("logs")
    archive_root: Path = Path("archive")
    #: How many past run directories to keep under `archive_root`.
    #: Nothing pruned them before, and they accumulate for as long as
    #: the project is used. None disables pruning.
    archive_keep_runs: int | None = 50
    #: How many QEMU serial logs to keep per target. One is written
    #: per `qemu.start` and they are never small -- a chatty boot with
    #: kernel debug on runs to hundreds of megabytes. None disables.
    serial_log_keep: int | None = 20
    #: Total size budget for those logs, per target, in megabytes.
    #: A count on its own does not bound the disk: twenty logs of
    #: 150 MB is still 3 GB. Oldest are deleted first, and the newest
    #: is never touched -- a running guest is probably writing to it.
    #: None disables the size budget and leaves only the count.
    serial_log_max_total_mb: int | None = 512
    mcp_transport: Literal["stdio", "sse", "streamable-http"] = "stdio"
    mcp_http_addr: str = "127.0.0.1:7180"
    # When set, MCP tool calls that omit `target` resolve to this
    # name. Lets single-target setups skip the boilerplate. Override
    # per-call by passing target explicitly. (Improvement #2 from
    # the API tidy pass.)
    default_target: str | None = None


class SerialShellChannel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    endpoint: str = "127.0.0.1:4321"

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, v: str) -> str:
        if ":" not in v:
            raise ValueError("endpoint must be host:port")
        host, _, port = v.rpartition(":")
        if not host or not port.isdigit():
            raise ValueError("endpoint must be host:port (numeric port)")
        return v

    @property
    def host(self) -> str:
        return self.endpoint.rpartition(":")[0]

    @property
    def port(self) -> int:
        return int(self.endpoint.rpartition(":")[2])


class QmpChannel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    endpoint: str = "127.0.0.1:14422"

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, v: str) -> str:
        if ":" not in v:
            raise ValueError("endpoint must be host:port")
        host, _, port = v.rpartition(":")
        if not host or not port.isdigit():
            raise ValueError("endpoint must be host:port (numeric port)")
        return v

    @property
    def host(self) -> str:
        return self.endpoint.rpartition(":")[0]

    @property
    def port(self) -> int:
        return int(self.endpoint.rpartition(":")[2])


class McpdChannel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    endpoint: str = "127.0.0.1:4322"
    # Host-side UDP port for the LAN discovery forwarder. The guest always
    # binds the fixed discovery port; the HOST port must be unique per
    # concurrently-running QEMU instance. None => derive from the mcpd host
    # port (already unique per target) so two qemu targets never collide.
    discovery_port: int | None = None

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, v: str) -> str:
        if ":" not in v:
            raise ValueError("endpoint must be host:port")
        host, _, port = v.rpartition(":")
        if not host or not port.isdigit():
            raise ValueError("endpoint must be host:port (numeric port)")
        return v

    @property
    def host(self) -> str:
        return self.endpoint.rpartition(":")[0]

    @property
    def port(self) -> int:
        return int(self.endpoint.rpartition(":")[2])


class GdbChannel(BaseModel):
    """QEMU's GDB-RSP stub. Enable with `-gdb tcp::PORT` on QEMU.

    Used for whole-system memory + register inspection (PowerPC CPU
    state, not per-task Amiga). Per-task / IDebug integration is
    separate.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    endpoint: str = "127.0.0.1:1234"

    @field_validator("endpoint")
    @classmethod
    def _check_endpoint(cls, v: str) -> str:
        if ":" not in v:
            raise ValueError("endpoint must be host:port")
        host, _, port = v.rpartition(":")
        if not host or not port.isdigit():
            raise ValueError("endpoint must be host:port (numeric port)")
        return v

    @property
    def host(self) -> str:
        return self.endpoint.rpartition(":")[0]

    @property
    def port(self) -> int:
        return int(self.endpoint.rpartition(":")[2])


class SerialChannel(BaseModel):
    """Host-side serial port attached to a target.

    `uboot` is the rear-panel DB9 (U-Boot console + AOS4 kernel debug
    when boot args include `serial`). `mcu` is the internal MCU UART
    header (X5000 P18 / A1222 P15 — 38400 8N1).
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    port: str = ""
    baud: int = 115200


class InputConfig(BaseModel):
    """Per-target input-injection policy. Disabled by default.

    ⚠️ This is a *host-side convenience* gate, not a security boundary.
    The real control lives in the daemon: MCPd must have been started
    with `--enable-input`, or have the sentinel file
    `SYS:System/MCPd/ENABLE-INPUT` present, or every `input.*` call
    returns -32003 no matter what this file says. Anything that can
    reach TCP 4322 bypasses this block entirely.

    Its purpose is to stop an agent from firing input at a target the
    operator never intended to drive — a wrong-target guard, not an
    access-control one.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    #: Characters per `input.type`. Lower than the daemon's own 512
    #: on purpose: typing costs about two events per character, and
    #: the event cap below binds first, so a longer string was only
    #: ever typed in part and reported `truncated: true`. Agreeing
    #: with the real limit beats advertising one that cannot be met.
    max_text_len: int = 128
    #: Input events per call, matching the daemon's cap. Enforced
    #: there; checked here so an over-long call fails before half of
    #: it has been typed into a live desktop.
    max_events: int = 256
    default_delay_ms: int = 15
    allow_drag: bool = True


class TargetChannels(BaseModel):
    """Per-target channel configuration."""

    model_config = ConfigDict(extra="allow")

    mcpd: McpdChannel | None = None
    qmp: QmpChannel | None = None
    gdb: GdbChannel | None = None
    uboot: SerialChannel | None = None
    mcu: SerialChannel | None = None


class SandboxTargetConfig(BaseModel):
    """Per-target SandboxVM overrides.

    Lives under `[targets.<name>.sandbox]` in config.toml. All fields
    optional; the `sandbox.*` namespace falls back to built-in defaults
    when an entry is omitted.
    """

    model_config = ConfigDict(extra="ignore")

    path: str | None = None
    """AOS path to sandboxvm on this target (e.g. ``"Tools:sandboxvm"``).
    When omitted, `sandbox.probe` searches a default list."""

    default_extmem_mb: int = 1024
    """Default ``-m <MB>`` for sandbox.run_guest."""

    default_window_mb: int = 256
    """Default ``-w <MB>`` for sandbox.run_guest."""

    deny_libs: list[str] = Field(default_factory=list)
    """Libraries always denied via ``-x`` for every guest run on this
    target. Merged with per-call ``deny_libs``."""


class TargetConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: Literal["qemu", "remote"]
    display_name: str | None = None
    machine: str | None = None
    qemu_config: Path | None = None
    master_image: Path | None = None
    idle_timeout_s: int = 300
    headless: bool = False
    tags: list[str] = Field(default_factory=list)
    channels: TargetChannels = Field(default_factory=TargetChannels)
    sandbox: SandboxTargetConfig | None = None

    # Policy, not a transport — so it sits on the target rather than in
    # `channels`. Defaults to None (absent) rather than a disabled
    # instance so "never configured" and "explicitly turned off" stay
    # distinguishable.
    input: InputConfig | None = None


class PathsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    qemu_runner: Path | None = None
    amiga_qemu_tests: Path | None = None
    combi_installer: Path | None = None
    spe_tests: Path | None = None
    adtools_gdb: Path | None = None
    qemu_binary: Path | None = None
    sandboxvm: Path | None = None
    """Host-side path to a built ``bin/sandboxvm``. Used by
    ``sandbox.deploy`` to resolve the upload source when no explicit
    ``source`` is passed."""


class DefaultsConfig(BaseModel):
    """Per-tool parameter defaults.

    When a tool is invoked without a value for one of these parameters
    and a default is set here, the server uses the default. Pass the
    parameter explicitly on a call to override.
    """

    model_config = ConfigDict(extra="ignore")

    # installer.* defaults
    dest_volume: str | None = None
    sources_dir: str | None = None
    machine: str | None = None
    # installer_run / installer_install_x5000
    iso_filename: str | None = None


class Config(BaseModel):
    model_config = ConfigDict(extra="ignore")

    server: ServerConfig = Field(default_factory=ServerConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    defaults: DefaultsConfig = Field(default_factory=DefaultsConfig)
    targets: dict[str, TargetConfig] = Field(default_factory=dict)

    def target(self, name: str) -> TargetConfig:
        if name not in self.targets:
            raise KeyError(f"unknown target: {name!r}")
        return self.targets[name]

    def resolve_target(self, name: str | None) -> str:
        """Resolve a possibly-empty target name against
        `server.default_target`. Returns the explicit target name when
        `name` is set, else falls back to the config default. Raises
        ValueError if neither is set or if the resolved name isn't a
        configured target.
        """
        chosen = (name or "").strip() or self.server.default_target
        if not chosen:
            raise ValueError(
                "no target given and no server.default_target in config"
            )
        if chosen not in self.targets:
            raise ValueError(
                f"unknown target: {chosen!r} "
                f"(configured: {sorted(self.targets)})"
            )
        return chosen


def default_config_path() -> Path:
    if env := os.environ.get("AMIGA_FLEET_CONFIG"):
        return Path(env)
    # `sys.platform` resolves to a literal at type-check time on
    # whichever platform mypy is run on, leaving one of these two
    # branches "unreachable" by static analysis. Use an Any-typed
    # alias so mypy keeps both branches live.
    platform: Any = sys.platform
    if platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "amiga-fleet-mcp" / "config.toml"
    return Path.home() / ".config" / "amiga-fleet-mcp" / "config.toml"


def load_config(path: Path | str | None = None) -> Config:
    p = Path(path) if path else default_config_path()
    if not p.exists():
        raise FileNotFoundError(f"config not found: {p}")
    with p.open("rb") as fh:
        data: dict[str, Any] = tomllib.load(fh)
    return Config.model_validate(data)
