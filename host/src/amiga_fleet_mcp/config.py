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


class TargetChannels(BaseModel):
    """Per-target channel configuration."""

    model_config = ConfigDict(extra="allow")

    mcpd: McpdChannel | None = None
    qmp: QmpChannel | None = None
    gdb: GdbChannel | None = None
    uboot: SerialChannel | None = None
    mcu: SerialChannel | None = None


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


class PathsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    qemu_runner: Path | None = None
    amiga_qemu_tests: Path | None = None
    combi_installer: Path | None = None
    spe_tests: Path | None = None
    adtools_gdb: Path | None = None
    qemu_binary: Path | None = None


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
