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

Each QEMU target needs its own host-side ports. The daemon inside a
guest always binds 4322 (TCP) and 4323 (UDP discovery); the host side
of those forwards is what must differ, or the second guest's QEMU
refuses to start on a duplicate rule. The discovery forward follows
the `endpoint` port automatically, so two targets that differ there
can run side by side. Override it only if that number is taken:

```toml
[targets.qemu-pegasos2.channels.mcpd]
endpoint       = "127.0.0.1:4422"
discovery_port = 4999
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
| `machine`       | `installer.required_files`, `installer.preflight`, `installer.run`, `installer.stage`, `installer.verify` |
| `iso_filename`  | `installer.install_x5000`, `installer.run`, `installer.stage` (auto-detected when omitted) |

Per-call overrides always win, so a single config can serve a
multi-machine fleet — set the most common values in `[defaults]`
and pass overrides on the rare exceptions.

The same precedent applies to the existing `[server] default_target`
key: a single-target setup can omit `target=...` from every call.

## Helper paths (`[paths]`)

The optional `[paths]` block points at external host-side checkouts
and binaries. Every entry is **only required by a specific tool
surface** — leave the others unset (or comment them out) and the
remaining tools work fine. None of these are needed to talk to an
already-running target via `fs.*` / `exec.*` / `sys.*` / `wb.*` /
`power.*`.

| `[paths]` key      | Required by                                                                                                                        | What to point it at |
|---|---|---|
| `qemu_runner`      | `qemu.*` lifecycle tools (`start` / `stop` / `reset` / `screenshot` / `savevm` / `loadvm` / `list_snapshots` / `delete_snapshot`) and the QMP transport. | An absolute path to a checkout of `derfsss/qemu-runner`. |
| `amiga_qemu_tests` | `tests.*` orchestration (`tests.list_suites`, `tests.run_suite`, `tests.run_standard_dos_tests`, `tests.parse_output`).             | An absolute path to a checkout of `derfsss/AmigaQemuTests`. |
| `qemu_binary`      | `qemu.start` only.                                                                                                                  | An absolute path to `qemu-system-ppc` (`qemu-system-ppc.exe` on Windows). |

When a tool needs one of these and it's missing, the call returns
an `InvalidParams` error naming the missing key, e.g.
*`paths.amiga_qemu_tests not set in config — see USAGE.md#helper-paths-paths or run amiga-fleet-mcp --init`*.

## Discovering targets without prior IP knowledge

```sh
amiga-fleet-mcp --discover --discover-timeout-ms 5000
```

The host broadcasts a UDP probe on the LAN. Every running MCPd
responds with its IP, port, hostname, and method count. Use the
output to populate `endpoint` fields in the configuration.

Local QEMU guests are found too, even though a broadcast cannot
reach them: `fleet.discover` also probes the forwarded discovery port
of every configured QEMU target. Those responders come back with the
endpoint the host can actually connect to and the name of the
configured target they belong to, rather than the port the daemon
binds inside the guest. Anything answering that is *not* already in
the configuration is reported with `target: null` — which makes this
the quick way to answer "what else is running on this network?".

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
| `fs.*` | Filesystem operations: list (recursive optional), stat, read (offset / length), write, delete (recursive optional), makedir, rename, protect, copy, hash (SHA-256). Plus `upload` / `download` whole-file wrappers that hide chunking + base64 + zlib for any-size, any-byte transfers in either direction (optional resume + sha256 verify). |
| `exec.cmd` | Run an AmigaDOS command with optional `args[]`, `cwd`, and timeout. |
| `sys.*` | System introspection: version, uptime, memory, volumes, assigns, tasks, libraries, devices, ports, loaded modules, last alert (decoded), hardware (CPU + AttnFlags + resource probes), I²C bus enumeration, performance counters, ELF symbol query, application registry, alert decoder, kernel debug ring, software cold reboot. On Freescale QorIQ boards, also CCSR register reads, TLB1 dump, physical-address reads, and the X5000 Cyrus MCU supervisor protocol. |
| `wb.*` | Workbench: screens, windows, public-screen registry, frontmost screen and window, plus `wb.screenshot` — capture a screen to PNG on the target and bring it back to the host. Works on real hardware as well as QEMU. |
| `debug.*` | Per-task and whole-system debug: task snapshot, symbolicated stack trace, address-to-symbol resolution, register and memory read / write, breakpoints, single step, continue, detach. |
| `qemu.*` | QEMU lifecycle: start, stop, reset, status, screenshot, serial-log resource, snapshots (savevm / loadvm / list / delete). |
| `fleet.*` | Multi-target operations: list targets, target status, run-on-all, barrier, quorum run, relay (cross-target file copy), discover. |
| `tests.*` | Wraps the AmigaQemuTests harness as MCP methods. |
| `events.*` | Event channel: long-poll wait, server-push subscribe / unsubscribe, test emission. |
| `app.notify`, `notify.fleet`, `notify.on_alert` | Ringhio popup notifications: per-target, broadcast, and event-driven. |
| `installer.*` | Native AmigaOS 4.1 FE installer pipeline: list machines, scan host sources, preflight, mount / unmount ISO, recursive copy, LHA extraction, Kicklayout read / write / patch, staged upload, per-machine install (`installer.run` / `installer.install_x5000`), and post-install verification. |
| `serial.*` | Host-side serial-capture lifecycle: start, stop, status, read, tail, clear. Captures a target's debug UART (e.g. X5000 rear-panel DB9) into a host-side log so kernel-debug output during boot or crash recovery can be read without a terminal program tying up the cable. |
| `power.*` | Host-side X5000 / A1222 MCU debug-shell driver over the FTDI USB-TTL header (X5000 P18, A1222 P15). `power.on` boots a powered-off box; `power.off` shuts it down; `power.toggle_stream(watch_s)` captures continuous sensor blocks; plus `help`, `identify`, `identify_dates`, `sensors`, `shell`. The only software power-on path on real X5000 — bypasses MCPd entirely so it works when AOS is off or wedged. |
| `input.*` | Keyboard and mouse injection: pointer state, type, key chords, move, click, drag, scroll. **Disabled by default at the daemon** — see [Driving the target's UI](#driving-the-targets-ui-input). |
| `sandbox.*` | Run programs and drivers inside SandboxVM so a guest crash doesn't take the machine down: probe, deploy, run one guest, run a resident driver, run a batch, read the last trap. See [Driver / program iteration](#driver--program-iteration-via-sandboxvm-sandbox). |

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

## Whole-file transfers (`fs.upload` / `fs.download`)

The low-level `fs.read` / `fs.write` / `fs.write_chunk` methods
operate at byte-level on the wire (32 MiB JSON-RPC frame cap).
Two convenience wrappers hide all of that — point at a single
file in either direction and the transfer just works:

```python
# Host -> target. Any size, any byte content.
await fs.upload(
    target="x5000-real",
    local_path="/host/path/to/big.iso",
    remote_path="DH0:tmp/big.iso",
    verify=True,            # SHA-256 both sides after; raise on mismatch
)

# Target -> host. Same UX.
await fs.download(
    target="x5000-real",
    remote_path="DH0:S/Startup-Sequence",
    local_path="/host/path/startup.txt",
    resume=True,            # continue an interrupted partial download
)
```

What happens transparently:

- **Auto-chunking** — files larger than `chunk_size` (24 MiB raw
  by default) split into multiple `fs.write_chunk` calls (or
  multiple `fs.read(offset, length)` calls on the way back).
  Each chunk lands at its byte offset in the same destination
  file; there's no separate reassembly step.
- **Auto base64** — the JSON-RPC envelope can't carry raw bytes,
  so chunks are base64-encoded on the way out and decoded on
  the way back. Binary-clean: NUL / 0xFF / arbitrary bytes
  round-trip exactly.
- **Auto zlib** (uploads only) — when `compression="auto"` and
  the result is at least 5% smaller, each chunk ships
  pre-compressed. Skips already-compressed payloads (.lha /
  .zip / .iso).
- **Optional resume** — `resume=True` probes the remote /
  local file size and continues from the partial state.
- **Optional verify** — `verify=True` adds an `fs.hash`
  round-trip + a local hashlib walk and raises on mismatch.

For library callers, the same surface is reachable via
`amiga_fleet_mcp.tools.fs.fs_upload` / `fs_download`.

## Live debugging

Per-task introspection uses the AmigaOS `IDebug` interface under
`Forbid()`. `debug.task_snapshot` captures registers, traptype, DAR /
DSISR, and a frame-chain backtrace. `debug.stacktrace` adds
symbolication via `IDebug->StackTrace`. `debug.symbol` resolves an
arbitrary address to module / function / source.

For QEMU targets, the `gdb` channel exposes whole-system register and
memory access through QEMU's GDB stub.

## Making a write durable (`fs.sync`)

AmigaOS commits writes to disk on its own schedule. That is invisible
while a machine keeps running and decisive when it stops abruptly: a
write made seconds earlier can still be only in RAM, and a killed QEMU
guest, a power cut or a cold reboot loses it outright. Measured on a
pegasos2 guest, a 312 KB write was still not on disk two seconds
later — and the flush has enough jitter that no fixed delay is a
guarantee.

```python
fs.sync()                      # flush SYS:
fs.sync(path="Work:project")   # flush whatever volume that lives on
```

`fs.sync` sends `ACTION_FLUSH` and waits for the filesystem to answer,
so when it returns the data is down. `qemu.stop` calls it for you
before stopping a guest you have written to; reach for it directly
before anything else abrupt, such as `sys.cold_reboot` or cutting
power with `power.off`.

## Seeing the screen (`wb.screenshot`)

Capture what is actually on the target's display, as a PNG:

```python
wb.screenshot()                      # frontmost screen, inlined as base64
wb.screenshot(screen_index=1)        # a specific screen
wb.screenshot(save_path="shot.png")  # keep the file at a host path
wb.screenshot(inline=False)          # skip the base64 in the result
```

The daemon grabs the screen with `graphics.library` `ReadPixelArray`
and encodes the PNG on the Amiga; the host tool then downloads it and
tidies up the temporary file on the target.

Unlike `qemu.screenshot`, which goes through QMP and therefore only
works for QEMU guests, this runs on the machine itself — so it is the
way to see a real X5000 or A1222 desktop. It also fans out:
`fleet.run_on_all` with `wb.screenshot` gives one image per machine.

Pair it with `wb.windows` / `wb.frontmost` when you need the geometry
behind what you are looking at, and with `input.*` below when you want
to act on it.

## Driving the target's UI (`input.*`)

The `wb.*` tools only *look* at Intuition. `input.*` writes to it —
synthesising keyboard and mouse events through `input.device` so an
agent can dismiss a requester, click Proceed in a GUI installer, or
drive Workbench itself.

This is the most dangerous surface in the project, and it is **off by
default behind two independent gates**.

### Opening the gates

**1. On the Amiga (the real control).** Either start the daemon with
`--enable-input`, or — better — create the sentinel file:

```
Execute SYS:System/MCPd/MCPd-Enable-Input
```

then restart MCPd. The flag does *not* survive a restart, because
`MCPd-Watchdog` relaunches with no arguments; the sentinel does.
MCPd prints `*** INPUT INJECTION ENABLED ***` at startup when the
gate is open — if you don't see that line, it isn't.

The gate is read once at startup, so **no RPC method can turn it
on**. Enabling requires filesystem access to the target.

**2. In the host config (a wrong-target guard).**

```toml
[targets.x5000-real.input]
enabled = true
```

Absent or `false` raises `NotCapable` before anything reaches the
wire. This exists to stop an agent firing input at a target you never
meant to drive; it is *not* access control, since anything that can
reach TCP 4322 bypasses the host entirely.

To revoke: `Execute SYS:System/MCPd/MCPd-Disable-Input`, restart MCPd.

### Look before you click

You are typing blind unless you check first. The idiom is:

```
input.state                    -> pointer position, active window
wb.windows                     -> where everything is
input.mouse_move  x=.. y=..    -> returns the position ACHIEVED
input.state                    -> confirm before committing
input.click       confirm=true
wb.frontmost                   -> confirm activation changed
```

`input.mouse_move` reports where the pointer actually ended up rather
than where you asked it to go, so the move is self-verifying. Absolute
coordinates are clamped to the frontmost screen.

### Typing

```
input.type  text="Hello" confirm=true
input.key   keys=["lamiga","q"] confirm=true      # or chord="lamiga+q"
```

For `input.key`, every entry but the last must be a modifier. The
`ctrl+lamiga+ramiga` reset chord additionally requires
`confirm_reset=true`.

`input.type` is layout-correct: the daemon maps each character through
the target's own `keymap.library` (`MapANSI`), including dead-key
sequences for accented characters, so a German or French machine
receives what you asked for. Pass `keymap="us"` to force the built-in
US table; that table is also the automatic fallback if
`keymap.library` cannot be opened, and the result's `keymap` field
tells you which path ran. Characters that cannot be produced on the
active layout come back in `unmapped[]` rather than being silently
dropped, so check that field if the result matters.

⚠️ Everything typed through `input.type` is recorded **in cleartext**
in the run archive. That is deliberate — it is an audit log — but it
means you must not type credentials through it.

### Limits

Each call is capped at 256 events, 20 s of wall clock, 512 characters
of text, and 64 drag steps. A call that hits a cap stops cleanly and
returns `truncated: true`. The daemon always releases held modifiers
and mouse buttons before returning, including on the abort path, so a
truncated `input.drag` cannot leave the machine with a stuck button.

`[targets.<n>.input] allow_drag = false` disables `input.drag` alone
while leaving the rest of the surface available.

## Out-of-band power control (`power.*`)

For real-hardware targets with the FTDI USB-TTL cable wired to the
internal MCU debug header (X5000 P18, A1222 P15), the `power.*`
namespace drives the MCU's interactive `>>` shell directly from
the host. **It bypasses MCPd entirely** — works regardless of AOS
or MCPd state, which makes it the only software path to:

- Boot a fully-off X5000 (`power.on` ↔ MCU `p` command).
- Recover a wedged box where `sys.cold_reboot` can't reach the
  daemon any more (`power.off` then `power.on`).
- Stream live sensor blocks over the cable
  (`power.toggle_stream` with `watch_s = N`) without touching
  AOS at all.

Setup is one TOML block per target — see
[INSTALL.md § `power.*`](INSTALL.md#power-x5000--a1222-internal-mcu-header-only)
for the cable pinout and config:

```toml
[targets.x5000-real.channels.mcu]
enabled = true
port    = "COM5"            # or /dev/ttyUSB1
baud    = 38400
```

| Tool | Shell | Confirm | What it does |
|---|---|---|---|
| `power.help` | `help` | — | Print the MCU's command list. |
| `power.identify` | `id` | — | Cyrus-Plus board name + MCU/CPLD versions. |
| `power.identify_dates` | `id date` | — | MCU + CPLD build dates and times. |
| `power.sensors` | `v` | — | Voltages + temperatures, human-formatted. (For the structured wire form, prefer `sys.mcu_cmd cmd="v"`.) |
| `power.toggle_stream` | `q` | yes | Toggle continuous-stream mode. With `watch_s = N`, capture for N seconds and auto-toggle off. |
| `power.on` | `p` | yes | Power up all supplies. **Resets the box if already on.** |
| `power.off` | `s` | yes | Shut down all supplies. |
| `power.shell` | (any) | yes | Generic passthrough — escape hatch for undocumented MCU shell commands. |

Hardware-destructive tools (`on`, `off`, `toggle_stream`, `shell`)
require `confirm: true` on every call — same accidental-fire guard
as `sys.cold_reboot`. If the cable is captured by `serial.*`, the
power tools return `NotCapable` until the capture stops; the two
surfaces share the underlying serial-port file handle.

A typical recovery flow when the X5000 is wedged on the network:

```
power.off(target="x5000-real", confirm=true)
# wait ~25 s for the supplies to drop
power.on(target="x5000-real", confirm=true)
# wait ~54 s for AOS + MCPd to come back up
fleet.target_status(target="x5000-real")     # confirm reachable
```

`power.on` `p` empirically takes ~80 s for a full off-then-boot
cycle before MCPd is reachable again on real X5000 hardware.

## Driver / program iteration via SandboxVM (`sandbox.*`)

The `sandbox.*` namespace wraps
[SandboxVM](https://github.com/derfsss/SandboxVM) — a PPC AOS4
binary that runs guest ELFs inside a sandboxed `IExec` clone with
a `tc_TrapCode` trampoline that survives DSI / ISI / illegal /
alignment / privilege traps. The integration lets an agent
edit-compile-deploy-run a binary on a real or emulated AmigaOS
target without power-cycling on every crash.

### When to use it

- Iterating on a new program where you expect occasional segfaults
  during development.
- Loading a `.device` / `.library` driver and exercising it with a
  follow-on test program, **without** doing a real `Mount` on the
  live system.
- Running a regression bundle of test binaries (≤ 16 per call) and
  collecting per-guest exit codes + trap classifications.

For untrusted code or genuinely unsafe fuzz targets, use QEMU
instead — SandboxVM is API-boundary, not MMU-boundary, so a wild
store like `*(int *)0xDEADBEEF = 42` still corrupts host memory
(the sandbox catches the *trap that follows*, not the store).
This is documented and expected.

### Prerequisites

- Target must be ExtMem-capable: X5000 / X1000 / A1222 or QEMU
  AmigaOne. Pegasos II is refused at probe time.
- A built `bin/sandboxvm` (PPC AOS4 binary). Fastest route:
  download from
  [the SandboxVM releases page](https://github.com/derfsss/SandboxVM/releases/latest).
  Build-from-source documented in the SandboxVM repo's README
  (Docker cross-compile via `walkero/amigagccondocker:os4-gcc11`).
- Optional `[paths] sandboxvm` in `config.toml` so `sandbox.deploy`
  doesn't need an explicit `source=`. Optional
  `[targets.<name>.sandbox]` block for per-target overrides
  (binary path, default extmem / window size, always-on deny-libs).

### Typical inner loop

```
sandbox.probe(target="x5000")
# -> SANDBOXVM_MISSING -- binary not on target yet

sandbox.deploy(target="x5000", confirm=true)
# uploads bin/sandboxvm to SYS:Tools/sandboxvm, invalidates the
# probe cache, re-pins it to the deployed path

# build + upload your guest binary via the host Docker SDK +
# fs.upload -- or just fs.upload an existing build.
fs.upload(target="x5000",
          local_path="bin/my_test",
          remote_path="RAM:my_test")

sandbox.run_guest(target="x5000", guest="RAM:my_test")
# -> { exit_code: 0, trap_kind: null, stdout: "...", ... }

# on a crash:
sandbox.run_guest(target="x5000", guest="RAM:crashy")
# -> { exit_code: -768, trap_kind: "DSI", ... }
sandbox.last_trap(target="x5000")
# -> { found: true, trap_kind: "DSI", traptype_hex: "0x300",
#      raw_lines: [<full trap dump from the kernel ring>], ... }
# -- daemon survived the crash; the next sandbox call works
# without intervention.
```

### Driver iteration

```
fs.upload(target="x5000",
          local_path="bin/virtioscsi.device",
          remote_path="RAM:virtioscsi.device")
fs.upload(target="x5000",
          local_path="bin/virtioscsi-test",
          remote_path="RAM:virtioscsi-test")

sandbox.run_driver(target="x5000",
                   driver="RAM:virtioscsi.device",
                   test="RAM:virtioscsi-test")
# Loads the driver via -r mode (CLT_InitFunc + IExec clone),
# then chains the test ELF in the SAME Guest so OpenLibrary
# resolves the freshly-loaded driver via the resident-lib
# registry. Per-iteration cycle ≈ 1 s on real X5000.
```

### Batched test bundles

```
sandbox.run_batch(target="x5000", guests=[
    {"guest": "RAM:test_1"},
    {"guest": "RAM:test_2"},
    {"guest": "RAM:test_3"},
    # ... up to 16
])
# -> {
#   aggregate_exit_code: -768,   // last non-zero rc seen
#   all_clean: false,
#   entries: [
#     { name: "batch.0", exit_code: 0, trap_kind: null, ... },
#     { name: "batch.1", exit_code: -768, trap_kind: "DSI", ... },
#     { name: "batch.2", exit_code: 0, trap_kind: null, ... },
#   ],
# }
```

Per-guest exit codes are recovered from the kernel debug ring
after the batch finishes (`[sandboxvm] guest_run_elf <path>
returned <rc>` lines emitted via `DebugPrintF`). Per-guest argv
and per-guest deny-lists are deliberately not supported — both
conflict with SandboxVM's actual CLI. Use `sandbox.run_guest`
per-binary when either matters.

For multi-target fan-out, wrap a `sandbox.run_batch` call in
`fleet.run_on_all` to drive the same bundle across X5000 + A1222
+ QEMU AmigaOne in parallel; per-target results come back keyed
by target name.

### Cross-references

- `sys.debug_ring` is the underlying primitive `sandbox.last_trap`
  filters against. Useful well outside sandbox: post-install
  forensics, hardware bring-up.
- `tests.run_suite` is the right tool for JSON-config-driven
  multi-step test bundles **without** the SandboxVM harness.

## Validation

```sh
# Run all five validation rounds against any reachable MCPd
python scripts/validate.py --endpoint <target-ip>:4322

# Exercise the input.* gate end to end: refused by default, opened
# only by the sentinel + a restart, injection verified, closed again
python scripts/validate_input.py --target x5000 --restart-mode cold

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

- `sys.debug_ring` returns the ring contents over MCP, so no Shell
  access is needed. `C:DumpDebugBuffer` from a Shell on the target
  does the same locally.
- For richer detail, replace `kernel` with `kernel.debug` in
  `SYS:Kickstart/Kicklayout` and reboot. The verbose kernel logs
  memory tracking, library load events, and exception details at a
  modest runtime cost.

**The debug buffer does not wrap.** Once full, the kernel stops
adding entries rather than overwriting the oldest, so on a machine
with a chatty driver the buffer can be exhausted during boot and
nothing logged afterwards will appear — including MCPd's own startup
beacon and any later trap. Compare `raw_size` across two
`sys.debug_ring` calls a minute apart: no growth on an active machine
means the buffer is full, not that the machine is quiet. A serial
capture (`serial.*`) is not subject to the buffer and is the reliable
option on such a machine.

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

If the daemon is wedged and the network is no longer responsive,
two out-of-band recovery paths are available:

- `sys.cold_reboot(target=..., confirm=true)` if MCPd itself is
  still answering JSON-RPC.
- On X5000 / A1222 with the MCU header cable wired,
  `power.off(target=..., confirm=true)` followed by `power.on(...)`
  cycles the box from outside the SoC entirely (see
  [Out-of-band power control](#out-of-band-power-control-power)).
