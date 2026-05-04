# Usage

This document covers what MCP-AmigaOS4 does and how to drive it once
installed. For installation see [INSTALL.md](INSTALL.md). For build
instructions see [BUILD.md](BUILD.md).

## Architecture

MCP-AmigaOS4 is two cooperating processes:

- **`MCPd`** runs on each AmigaOS 4 target (real hardware or QEMU
  guest). It listens for JSON-RPC 2.0 requests on TCP port 4322 and
  responds to UDP discovery probes on port 4323.
- **`amiga-fleet-mcp`** runs on the user's workstation. It is a
  Model Context Protocol server that connects to one or more `MCPd`
  instances and exposes them as typed MCP tools to any MCP-aware
  client.

Most users interact with the system through their MCP client. The
host server is invoked as a stdio command by the client and the user
never types its commands directly except for diagnostic purposes.

## Configuring the fleet

Copy `host/config.example.toml` to a permanent location (for example
`~/.config/amiga-fleet-mcp/config.toml`) and edit the `[targets.*]`
blocks. A minimal multi-target configuration:

```toml
[server]
mcp_transport = "stdio"
archive_root = "~/amiga-fleet-archive"

[targets.x5000-real]
type = "remote"
display_name = "X5000"
tags = ["real", "x5000"]
[targets.x5000-real.channels.mcpd]
endpoint = "<target-ip>:4322"

[targets.qemu-pegasos2]
type = "qemu"
display_name = "QEMU Pegasos2"
machine = "pegasos2"
tags = ["qemu", "pegasos2"]
qemu_config = "<absolute path to your QEMU machine's config.json>"
[targets.qemu-pegasos2.channels.mcpd]
endpoint = "127.0.0.1:4422"
[targets.qemu-pegasos2.channels.qmp]
endpoint = "127.0.0.1:14422"
```

Wire the server into the MCP client:

```sh
claude mcp add amiga-fleet -- amiga-fleet-mcp --config /path/to/config.toml
```

Run it directly for diagnostics:

```sh
amiga-fleet-mcp --config /path/to/config.toml
amiga-fleet-mcp --list-tools
amiga-fleet-mcp --list-resources
amiga-fleet-mcp --health-check
```

## Per-tool defaults

Tools that accept frequently-repeated parameters (the AmigaDOS
volume to install into, the host directory holding source ISOs and
LHAs, the canonical machine identifier, ...) can have their values
defaulted in `config.toml` so a typical session does not have to
repeat them on every call:

```toml
[server]
default_target = "x5000"

[defaults]
dest_volume   = "BootTest:"
sources_dir   = "/path/to/your/amiga-source-tree"
bootstrap_dir = "/path/to/your/diskimage-bootstrap"
machine       = "X5000"
# iso_filename = "AmigaOneX5000InstallCD-53.42.iso"   # rarely needed; auto-detected
```

Resolution order on every call:

1. The value passed explicitly to the tool call wins.
2. Otherwise, the matching `[defaults]` entry is used.
3. Otherwise, the call returns an `InvalidParams` error that names
   both the parameter and the config key, e.g.
   *`missing required parameter 'dest_volume'; pass it explicitly
   or set [defaults] dest_volume = ... in your config.toml.`*

### Supported keys

| `[defaults]` key | Used by |
|---|---|
| `dest_volume`   | `installer.preflight`, `installer.read_kicklayout`, `installer.write_kicklayout`, `installer.patch_kicklayout`, `installer.install_x5000`, `installer.run`, `installer.stage`, `installer.verify` |
| `sources_dir`   | `installer.scan_sources`, `installer.preflight`, `installer.install_x5000`, `installer.run`, `installer.stage` |
| `bootstrap_dir` | `installer.stage` |
| `machine`       | `installer.required_files`, `installer.preflight`, `installer.run`, `installer.stage`, `installer.verify` |
| `iso_filename`  | `installer.install_x5000`, `installer.run`, `installer.stage` (auto-detected when omitted) |

Per-call overrides always win, so a single config can serve a
multi-machine fleet — set the most common values in `[defaults]`
and pass overrides on the rare exceptions.

The same precedent applies to the existing `[server] default_target`
key: a single-target setup can omit `target=...` from every call.

## Discovering targets without prior IP knowledge

```sh
amiga-fleet-mcp --discover --discover-timeout-ms 5000
```

The host broadcasts a UDP probe on the LAN. Every running MCPd
responds with its IP, port, hostname, and method count. Use the
output to populate `endpoint` fields in the configuration.

## Tool surface

`amiga-fleet-mcp --list-tools` enumerates every registered tool. The
groups, in summary. Several groups have target-specific prerequisites
(GDB stub for whole-system debug, QorIQ CCSR for the SoC introspection
methods, the Cyrus MCU + UART1 for `sys.mcu_cmd`, source ISOs for
the installer, etc.) — see [INSTALL.md § Per-feature
prerequisites](INSTALL.md#per-feature-prerequisites) for the full
breakdown.

| Namespace | Description |
|---|---|
| `proto.*` | Capability and version reporting. |
| `fs.*` | Filesystem operations: list (recursive optional), stat, read (offset / length), write, delete (recursive optional), makedir, rename, protect, copy, hash (SHA-256). |
| `exec.cmd` | Run an AmigaDOS command with optional `args[]`, `cwd`, and timeout. |
| `sys.*` | System introspection: version, uptime, memory, volumes, assigns, tasks, libraries, devices, ports, last alert (decoded), hardware (CPU + AttnFlags + resource probes), I²C bus enumeration, performance counters, ELF symbol query, application registry, alert decoder. |
| `wb.*` | Workbench introspection: screens, windows, public-screen registry, frontmost screen and window. |
| `debug.*` | Per-task and whole-system debug: task snapshot, symbolicated stack trace, address-to-symbol resolution, register and memory read / write, breakpoints, single step, continue, detach. |
| `qemu.*` | QEMU lifecycle: start, stop, reset, status, screenshot, serial-log resource, snapshots (savevm / loadvm / list / delete). |
| `fleet.*` | Multi-target operations: list targets, target status, run-on-all, barrier, quorum run, relay (cross-target file copy), discover. |
| `tests.*` | Wraps the AmigaQemuTests harness as MCP methods. |
| `events.*` | Event channel: long-poll wait, server-push subscribe / unsubscribe, test emission. |
| `app.notify`, `notify.fleet`, `notify.on_alert` | Ringhio popup notifications: per-target, broadcast, and event-driven. |
| `installer.*` | Native AmigaOS 4.1 FE installer pipeline: list machines, scan host sources, preflight, mount / unmount ISO, recursive copy, LHA extraction, Kicklayout read / write / patch, staged upload, per-machine install (`installer.run` / `installer.install_x5000`), and post-install verification. |
| `serial.*` | Host-side serial-capture lifecycle: start, stop, status, read, tail, clear. Captures a target's debug UART (e.g. X5000 rear-panel DB9) into a host-side log so kernel-debug output during boot or crash recovery can be read without a terminal program tying up the cable. |

## MCP resources

Live, read-only data exposed under the `amiga://` URI scheme:

- `amiga://fleet/targets`
- `amiga://{target}/sys/version`
- `amiga://{target}/sys/tasks`
- `amiga://{target}/proto/capabilities`
- `amiga://{target}/qemu/serial_log` (QEMU targets only)
- `amiga://runs/`
- `amiga://runs/{run_id}`
- `amiga://runs/{run_id}/calls`

## Multi-target operations

Most fleet methods accept either an explicit `targets` list or a
`tags` filter. A request with neither runs against every configured
target.

```python
# Pseudocode (the actual call comes through the MCP client)
fleet.run_on_all(method="sys.version", tags=["qemu"])
```

`fleet.barrier` is the same with a per-target timeout — slow targets
fail individually rather than blocking the whole fan-out.
`fleet.relay` copies a file between two targets via the host.
`fleet.quorum_run` requires at least *N* of *M* targets to succeed.

## Server-pushed events

```
events.subscribe { topics: ["sys.lastalert", "debug.exception"] }
```

Once subscribed, the daemon emits JSON-RPC notifications whenever the
relevant baseline values change. The Python client demultiplexes
notifications from request responses transparently. Register a
callback with `subscribe_notifications(handler)` on the transport, or
poll the queue with `get_notification(timeout_s)`.

`events.wait` is the long-poll alternative for clients that prefer not
to maintain a persistent connection.

## QEMU lifecycle

`qemu.start` boots a target from its `qemu_config` and captures
serial output to the archive. `qemu.savevm` and `qemu.loadvm`
checkpoint and restore guest state. `qemu.screenshot` returns a PNG
image of the framebuffer.

## Live debugging

Per-task introspection uses the AmigaOS `IDebug` interface under
`Forbid()`. `debug.task_snapshot` captures registers, traptype, DAR /
DSISR, and a frame-chain backtrace. `debug.stacktrace` adds
symbolication via `IDebug->StackTrace`. `debug.symbol` resolves an
arbitrary address to module / function / source.

For QEMU targets, the `gdb` channel exposes whole-system register and
memory access through QEMU's GDB stub.

## Validation

```sh
# Run all five validation rounds against any reachable MCPd
python scripts/validate.py --endpoint <target-ip>:4322

# End-to-end install + reboot test on QEMU pegasos2
python scripts/qemu_install_test.py \
    --peg2-config /path/to/qemu/pegasos2/config.json \
    --shared-dir  /path/to/qemu/SHARED:

# Drive a real X5000 and a QEMU pegasos2 guest in parallel
python scripts/concurrent_x5000_qemu.py \
    --x5000       <x5000-ip> \
    --peg2-config /path/to/qemu/pegasos2/config.json
```

Each script accepts `--help` for the full flag list. `--endpoint`
also reads `$MCPD_ENDPOINT`; `--x5000` reads `$X5000_HOST`; the
QEMU binary is auto-detected on PATH but can be pinned via
`--qemu-binary` or `$QEMU_BINARY`.

## Reading on-target debug output

When the daemon (or AmigaOS itself) emits debug output via `KPrintF`
or `DebugPrintF` and there is no host serial cable attached, two
options exist:

- `C:DumpDebugBuffer` from a Shell on the target prints the current
  contents of the kernel debug ring buffer.
- For richer detail, replace `kernel` with `kernel.debug` in
  `SYS:Kickstart/Kicklayout` and reboot. The verbose kernel logs
  memory tracking, library load events, and exception details at a
  modest runtime cost.

## Stopping MCPd

From a Shell on the target:

```
Status FULL
Break <pid> C
```

MCPd traps `SIGBREAKF_CTRL_C`, unregisters from
`application.library`, closes its sockets, and exits cleanly. If the
auto-start watchdog is installed, it will relaunch the daemon shortly
afterwards; break the watchdog process too if the daemon should stay
down until reboot.
