# MCP-AmigaOS4

[![CI](https://github.com/derfsss/MCP-AmigaOS4/actions/workflows/ci.yml/badge.svg)](https://github.com/derfsss/MCP-AmigaOS4/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-BSD--3--Clause-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/downloads/)
[![AmigaOS 4.1 FE](https://img.shields.io/badge/AmigaOS-4.1%20FE-orange.svg)](https://www.hyperion-entertainment.com/)

A [Model Context Protocol](https://modelcontextprotocol.io) server for
driving AmigaOS 4.1 machines — both QEMU guests and real PowerPC
hardware — from MCP-aware clients such as Claude Code, Claude Desktop,
and IDE plugins.

## At a glance

Brings AmigaOS 4.1 inside your AI client. From a single MCP session you
can:

- **Browse and edit files** on the target Amiga, **run AmigaDOS
  commands** with captured stdout, **upload large binaries** with zlib
  compression and resumable chunks.
- **Inspect live state** — running tasks, opened libraries, mounted
  volumes, public screens, the last alert (decoded), CPU + cache +
  AttnFlags.
- **Drive QEMU lifecycle** — start, stop, screenshot the framebuffer,
  save / restore VM snapshots, attach a GDB stub for whole-system
  debug.
- **Coordinate a fleet** — run one method across any mix of QEMU
  guests and real hardware in parallel; barrier, quorum, or
  cross-target file relay.
- **Install AmigaOS 4.1 FE end-to-end** onto a blank volume — preflight,
  ISO mount, copy, LHA extract, Kicklayout patch, verify — via a
  single `installer.run` call.
- **Live X5000 hardware introspection** — board / CPU temperatures,
  voltages, fan PWM/RPM via the Cyrus MCU, plus CCSR registers, TLB
  walks, and IDebug-driven per-task crash snapshots.
- **Out-of-band power control** — with the FTDI USB-TTL cable wired
  to the X5000 P18 / A1222 P15 header, `power.on` / `power.off` /
  `power.toggle_stream` drive the MCU debug shell directly from
  MCP. Works regardless of AOS / MCPd state; the only software
  path to boot a fully-off X5000.
- **Iterate on programs and drivers without power-cycling** —
  `sandbox.*` wraps [SandboxVM](https://github.com/derfsss/SandboxVM)
  so a guest segfault no longer takes down the daemon. Edit →
  upload → run → read structured exit code + trap classification +
  captured stdout/stderr; load `.device` / `.library` drivers in
  resident-init mode and chain a test program against them, all in
  a single tool call.
- **See the screen** — `wb.screenshot` captures a Workbench screen to
  PNG on the Amiga and returns it, on real hardware as well as QEMU.
- **Drive the UI** — `input.*` synthesises keyboard and mouse events,
  so an agent can dismiss a requester or click through a GUI
  installer. Off by default, and openable only by a deliberate action
  on the target itself; see [SECURITY.md](SECURITY.md).

**137 typed MCP tools across 14 namespaces, 8 live resources**,
verified end-to-end on QEMU Pegasos2 and real AmigaOne X5000
hardware.

## Quick start

Three steps: install the host server, put the daemon on the Amiga,
point your MCP client at it.

```sh
# 1. Host server (Python 3.11+)
cd host && uv sync            # or: pip install -e .

# 2. Tell it about your machines. The wizard writes a validated
#    config.toml to the platform default location.
uv run amiga-fleet-mcp --init

# 3. Check it can reach them
uv run amiga-fleet-mcp --health-check
```

The Amiga side needs `MCPd` running on each target. Either deploy it
from the host:

```sh
python scripts/install_mcpd_autostart.py <target-ip>:4322
```

or, on the Amiga itself, grab `MCPd-<version>.lha` from the [latest
release](https://github.com/derfsss/MCP-AmigaOS4/releases/latest):

```
LhA x MCPd-1.3.lha
Execute MCPd/MCPd-Install
```

The archive is assembled on AmigaOS, so the protection bits are
already correct — extract and the daemon runs. (A raw ELF copied over
SMB or a USB stick arrives with the executable bit protected and has
to be `Protect +rwed`-ed by hand.)

Either way MCPd lands in `SYS:System/MCPd/`, gains a watchdog, and
auto-starts on boot. Then register the server with your client — for
Claude Code:

```sh
claude mcp add amiga-fleet -- amiga-fleet-mcp
```

Full detail in [INSTALL.md](INSTALL.md); configuration options in
[USAGE.md](USAGE.md).

## Documentation

| Topic | Document |
|---|---|
| What you can do with it | [USAGE.md](USAGE.md) |
| Full command / method / tool reference | [COMMANDS.md](COMMANDS.md) |
| Installing pre-built artefacts | [INSTALL.md](INSTALL.md) |
| Setup spec for AI agents | [AGENTS_SETUP.md](AGENTS_SETUP.md) |
| Building from source | [BUILD.md](BUILD.md) |
| Host server (Python) — quickstart | [host/README.md](host/README.md) |
| Amiga daemon (C) — quickstart and source layout | [mcpd/README.md](mcpd/README.md) |
| Change log | [CHANGELOG.md](CHANGELOG.md) |
| Contributing | [CONTRIBUTING.md](CONTRIBUTING.md) |
| Security policy | [SECURITY.md](SECURITY.md) |
| Credits | [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md) |
| Licence | [LICENSE](LICENSE) (BSD-3-Clause) |
| Third-party dependencies | [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md) |

**New here?** Read [USAGE.md](USAGE.md) for the architecture and tool
surface, then [INSTALL.md](INSTALL.md) to install pre-built artefacts
or [BUILD.md](BUILD.md) to build from source.

## Overview

MCP-AmigaOS4 is a two-piece system:

- **`amiga-fleet-mcp`** — a Python MCP server that runs on the user's
  workstation. It manages a fleet of one or more AmigaOS 4 targets,
  fans out operations across them in parallel, and bridges to QEMU for
  lifecycle, snapshots, and GDB.
- **`MCPd`** — a small C daemon running on each AmigaOS 4 target. It
  exposes filesystem, process, system, debug, and Workbench operations
  as JSON-RPC 2.0 methods over a 4-byte length-prefix framed TCP
  connection on port 4322, plus a UDP discovery responder on port 4323.

## Capabilities

A summary follows; the full list of tools, resources, methods, and
helper scripts lives in [COMMANDS.md](COMMANDS.md), with narrative
context in [USAGE.md](USAGE.md).

- 137 typed MCP tools across `fs.*`, `exec.cmd`, `sys.*`, `wb.*`,
  `debug.*`, `qemu.*`, `fleet.*`, `tests.*`, `events.wait`,
  `app.notify`, `notify.*`, `installer.*`, `serial.*`, `power.*`,
  `sandbox.*`, and `input.*`, plus a namespace dispatcher per group.
  Every tool takes a `target`, so the same call works against a QEMU
  guest or a real machine.
  (`events.subscribe`, `events.unsubscribe`, and `events.test_emit`
  are exposed only as daemon RPC methods, not MCP tools — clients
  call them directly through the transport.)
- Eight live MCP resources for fleet status, per-target system
  snapshots, capability reports, and serial logs.
- Parallel multi-target operations (`fleet.run_on_all`,
  `fleet.barrier`, `fleet.quorum_run`, `fleet.relay`) with optional
  per-target tag filters.
- LAN-local target discovery (UDP broadcast).
- QEMU lifecycle management plus snapshot save/load/list/delete.
- IDebug-driven per-task introspection: register / memory read and
  write, symbolicated stack traces, breakpoint control.
- Live system-state reporting: CPU model, caches, AttnFlags, resource
  probes, mounted volumes, active assigns, library list, public
  screens, application registry.
- Crash-survivable program and driver iteration (`sandbox.*`): run a
  guest ELF or a resident `.library` / `.device` inside SandboxVM and
  get back a structured exit code, a trap classification, and the
  captured output — without the machine going down with it.
- Screen capture to PNG on the target (`wb.screenshot`) — works on
  real hardware, where a QMP framebuffer dump is not an option.
- Keyboard and mouse injection (`input.*`) for driving Workbench and
  GUI installers, disabled by default at the daemon.
- Server-pushed events (`events.subscribe`) plus a long-poll
  alternative (`events.wait`).
- Auto-start install integration: a single command deploys MCPd to
  `SYS:System/MCPd/`, registers a watchdog wrapper, and patches
  `S:Network-Startup`. Boot-to-bind is approximately eleven seconds on
  an AmigaOne X5000. The daemon announces itself in the kernel debug
  ring once the port is actually accepting connections, so a
  boot-time start is observable without a Shell — see
  [mcpd/README.md](mcpd/README.md#startup-beacon).
- Per-tool parameter defaults via a `[defaults]` block in
  `config.toml` (`dest_volume`, `sources_dir`, `machine`,
  `iso_filename`). Set frequently-repeated values once and skip
  them in subsequent tool calls; explicit per-call values always
  override. See [USAGE.md § Per-tool
  defaults](USAGE.md#per-tool-defaults).
- Native AmigaOS 4.1 FE installer pipeline (`installer.*`): preflight,
  ISO mount, recursive copy, LHA extraction, Kicklayout patching,
  per-machine staged install, and post-install verification.
- Host-side serial-capture service (`serial.*`): start / stop / tail
  background captures of a target's debug UART for kernel-debug
  output during boot or crash investigation.
- Live X5000 hardware introspection (real-hardware only): Cyrus MCU
  supervisor protocol over UART (`sys.mcu_cmd`), Freescale QorIQ CCSR
  reads (`sys.read_ccsr`), TLB1 dump (`sys.tlb_dump`), and (with
  appropriate care) supervisor-mode physical-address reads
  (`sys.read_pa`).

## Supported targets

| Target | Status |
|---|---|
| QEMU Pegasos2 | Verified end to end, including the installer pipeline |
| QEMU AmigaOne, SAM460ex | Supported |
| AmigaOne X5000 (Freescale P5020 / E5500) | Verified on hardware, including `power.*`, the SoC introspection methods, and UI injection |
| AmigaOne A1222 / Tabor | Supported; board detection and the simplified install path are in place |
| AmigaOne X1000 / Nemo | Recognised by board detection |

`sandbox.*` needs a machine with ExtMem, so it is available on X5000
and A1222 but not on Pegasos2. `power.*` and the QorIQ introspection
methods are real-hardware features by nature.

See [INSTALL.md](INSTALL.md) for runtime requirements per target and
the auto-start install procedure, or [BUILD.md](BUILD.md) for
cross-compiling MCPd from source.

## Requirements

**Host:** Python 3.11, 3.12, or 3.13, on Windows, Linux, or macOS.
QEMU features additionally need a `qemu-system-ppc` binary;
`power.*` needs an FTDI USB-TTL cable wired to the target's MCU
header; `serial.*` needs a serial cable to the target's debug UART.

**Target:** AmigaOS 4.1 Final Edition with TCP/IP (Roadshow) and
`z.library` v53+. The daemon is a single ~300 KiB PowerPC
executable with no other dependencies.

Everything degrades per feature rather than per install: a target
without the MCU cable simply reports `NotCapable` for `power.*` and
keeps the rest of the surface. [INSTALL.md § Per-feature
prerequisites](INSTALL.md#per-feature-prerequisites) lists what each
group needs.

## Licence

[BSD 3-Clause](LICENSE). Copyright © 2026 Richard Gibbs.

Third-party dependencies retain their own permissive licences; the
full index is in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
See [ACKNOWLEDGEMENTS.md](ACKNOWLEDGEMENTS.md) for credits.
