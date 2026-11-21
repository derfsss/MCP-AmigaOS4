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

MCPd gives every accepted connection its own worker Process (up to 16
at once), so a request that crashes its handler costs that connection
only; the listener and the other connections carry on. Sockets are
non-blocking: a connection that stops making progress in the middle
of a request or a reply for 120 s is dropped, so a stalled peer cannot
hold a worker for good. An idle connection between requests is kept.

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

`qemu_config` names the JSON file `qemu.start` builds the QEMU command
line from: a [Kyvos](https://ko-fi.com/s/6476fdadd2) machine's
`config.json` (the recommended way to set up a guest), or one written from
[`host/qemu-config.example.json`](host/qemu-config.example.json). See
[QEMU_CONFIG.md](QEMU_CONFIG.md) for the format.

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
amiga-fleet-mcp --list-prompts
amiga-fleet-mcp --health-check
amiga-fleet-mcp --doctor
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
| `exec_priority` | `exec_cmd`, `tests.run_suite`, `sandbox.run_batch` — task priority for the jobs they start (default -2; see [Priorities](#priorities)) |

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
| `amiga_qemu_tests` | `tests.*` orchestration (`tests.list_suites`, `tests.run_suite`, `tests.run_standard_dos_tests`, `tests.parse_output`).             | An absolute path to a checkout of `derfsss/AmigaQemuTests`. |
| `qemu_binary`      | `qemu.start` only.                                                                                                                  | An absolute path to `qemu-system-ppc` (`qemu-system-ppc.exe` on Windows). |

The `qemu.*` lifecycle tools and the QMP transport need no path entry.
They use [`derfsss/qemu-runner`](https://github.com/derfsss/qemu-runner),
installed from GitHub as a package dependency at a pinned commit (see
`host/pyproject.toml`). An old `qemu_runner = ...` line is ignored.

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
| `fs.*` | Filesystem operations: list (recursive optional), stat, read (offset / length), write, delete (recursive optional), makedir, rename, protect, copy, hash (SHA-256), sync (flush cached writes to disk). Plus `upload` / `download` whole-file wrappers that hide chunking + base64 + zlib for any-size, any-byte transfers in either direction (optional resume + sha256 verify). |
| `exec.*` | Run AmigaDOS commands as tracked jobs: `cmd` (optional `args[]`, `cwd`, timeout, priority, sandbox), then `status`, `output`, `kill` and `list` for a job that is still running. See [Running commands](#running-commands-exec). |
| `sys.*` | System introspection: version, uptime, memory, volumes, assigns, tasks, libraries, devices, ports, last alert (decoded), hardware (CPU + AttnFlags + resource probes), I²C bus enumeration, performance counters, ELF symbol query, application registry, alert decoder, kernel debug ring, software cold reboot. On Freescale QorIQ boards, also CCSR register reads, TLB1 dump, physical-address reads, and the X5000 Cyrus MCU supervisor protocol. |
| `wb.*` | Workbench: screens, windows, public-screen registry, frontmost screen and window, plus `wb.screenshot` — capture a screen to PNG on the target and bring it back to the host. Works on real hardware as well as QEMU. |
| `debug.*` | Per-task and whole-system debug: task snapshot, symbolicated stack trace, address-to-symbol resolution, register and memory read / write, breakpoints, single step, continue, detach. |
| `qemu.*` | QEMU lifecycle: start, stop, reset, status, screenshot, serial-log resource, snapshots (savevm / loadvm / list / delete). |
| `fleet.*` | Multi-target operations: list targets, target status, fleet snapshot (one-call health summary), run-on-all, barrier, quorum run, relay (cross-target file copy), discover. |
| `tests.*` | Wraps the AmigaQemuTests harness as MCP methods. |
| `events.*` | Event channel: `events.wait` long-poll. The daemon also has server-push `events.subscribe` / `events.unsubscribe` / `events.test_emit`, reached through the transport rather than as MCP tools (see [Server-pushed events](#server-pushed-events)). |
| `app.notify`, `notify.fleet`, `notify.on_alert` | Ringhio popup notifications: per-target, broadcast, and event-driven. |
| `installer.*` | Native AmigaOS 4.1 FE installer pipeline: list machines, scan host sources, preflight, mount / unmount ISO, recursive copy, LHA extraction, Kicklayout read / write / patch, staged upload, per-machine install (`installer.run` / `installer.install_x5000`), and post-install verification. |
| `serial.*` | Host-side serial-capture lifecycle: start, stop, status, read, tail, clear. Captures a target's debug UART (e.g. X5000 rear-panel DB9) into a host-side log so kernel-debug output during boot or crash recovery can be read without a terminal program tying up the cable. |
| `power.*` | Host-side X5000 / A1222 MCU debug-shell driver over the FTDI USB-TTL header (X5000 P18, A1222 P15). `power.on` boots a powered-off box; `power.off` shuts it down; `power.toggle_stream(watch_s)` captures continuous sensor blocks; plus `help`, `identify`, `identify_dates`, `sensors`, `shell`. The only software power-on path on real X5000 — bypasses MCPd entirely so it works when AOS is off or wedged. |
| `input.*` | Keyboard and mouse injection: pointer state, type, key chords, move, click, drag, scroll. **Disabled by default at the daemon** — see [Driving the target's UI](#driving-the-targets-ui-input). |
| `sandbox.*` | Run programs and drivers inside SandboxVM so a guest crash doesn't take the machine down: probe, deploy, run one guest, run a resident driver, run a batch, read the last trap. See [Driver / program iteration](#driver--program-iteration-via-sandboxvm-sandbox). |

Every tool exists in two forms: a fine-grained tool per method
(`fs_list`, `sys_version`, ...) and one dispatcher per namespace
(`fs`, `sys`, `exec`, `wb`, `input`, `debug`, `events`, `qemu`,
`fleet`, `tests`, `app`, `installer`, `serial`, `power`, `sandbox`)
that takes the method name and a `params` object. These two calls do
the same thing:

```python
fs_list(target="x5000-real", path="SYS:C")
fs(method="list", params={"target": "x5000-real", "path": "SYS:C"})
```

The dispatchers keep the tool list short for clients that load every
tool description up front. [COMMANDS.md](COMMANDS.md) has the full
list of tools, parameters and dispatcher method names.

## Ready-made workflows (prompts)

The server also offers eight MCP prompts: step-by-step workflows that
the AI follows using the tools above. In Claude Code they appear as
slash commands, e.g. `/mcp__amiga-fleet__getting_started`; other
clients list them in their prompt picker. `amiga-fleet-mcp
--list-prompts` prints them with their arguments.

| Prompt | Arguments | What it does |
|---|---|---|
| `getting_started` | `target?` | A read-only tour of one Amiga: version, volumes, memory, tasks and a screenshot. |
| `investigate_crash` | `target?`, `what_happened?` | Collects alert, debug-ring, crash-hook and screen evidence after a crash or hang, then explains the likely cause. |
| `deploy_and_run` | `local_path`, `target?`, `remote_dir?`, `args?` | Uploads a program, fixes its protection bits and runs it, sandboxed when SandboxVM is there. |
| `check_fleet` | `tags?` | Probes every target and summarises which are up, their memory, and any recent alerts. |
| `safe_reboot` | `target?` | Cold-boots a target without losing writes: checks jobs, flushes, starts a fresh serial log, power-cycles or restarts QEMU, waits for MCPd. |
| `deploy_library` | `local_path`, `remote_path`, `target?` | Backs up and replaces a library, device or driver, cold-boots, and confirms the new version is the one loaded. |
| `run_test_batch` | `local_script`, `target?`, `remote_dir?`, `results?` | Uploads an AmigaDOS script, runs it as a tracked job, follows it without re-running it, and collects the results. |
| `ab_test_fix` | `repro`, `local_path`, `remote_path`, `target?` | Runs a reproduction, deploys the fix with a cold boot, runs it again, and compares the two. |

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
`fleet.snapshot` gives a one-call health summary of every target, and
`fleet.target_status` probes one.

## Server-pushed events

```
events.subscribe { topics: ["sys.lastalert", "debug.exception"] }
```

Once subscribed, the daemon emits JSON-RPC notifications whenever the
relevant baseline values change. The Python client demultiplexes
notifications from request responses transparently. Register a
callback with `subscribe_notifications(handler)` on the transport, or
poll the queue with `get_notification(timeout_s)`.

`events.subscribe` accepts the topics `sys.lastalert` and
`debug.exception`. It is a daemon method, not an MCP tool.

`events.wait` is the long-poll alternative for clients that prefer not
to maintain a persistent connection, and the one exposed as an MCP
tool. The daemon snapshots state, polls every 200 ms, and returns the
first changes it sees, or an empty list when `timeout_ms` runs out
(the daemon caps it at 60 s; call again to keep waiting):

```python
events_wait(target="x5000-real",
            topics=["sys.lastalert", "debug.exception"],  # or "sys.task"; default all
            timeout_ms=30000)
```

## Notifications (`app.notify`, `notify.*`)

Pop up a Ringhio notification on the Amiga's screen, on one target or
all of them, or wait for a crash and announce it:

```python
app_notify(target="x5000-real", title="Build", text="Upload finished")
notify_fleet(title="Fleet", text="Tests starting", tags=["real"])
notify_on_alert(target="x5000-real", timeout_ms=60000)
# waits on events.wait for a debug.exception, then posts a popup
```

## QEMU lifecycle

`qemu.start` boots a target from its `qemu_config` and captures
serial output to the archive. `qemu.savevm` and `qemu.loadvm`
checkpoint and restore guest state. `qemu.screenshot` returns a PNG
image of the framebuffer.

```python
qemu_start(target="qemu-pegasos2")          # returns the PID
qemu_status(target="qemu-pegasos2")         # process / QMP reachability
qemu_savevm(target="qemu-pegasos2", name="clean-boot")
qemu_loadvm(target="qemu-pegasos2", name="clean-boot")
qemu_screenshot(target="qemu-pegasos2", save_path="peg2.png")
qemu_stop(target="qemu-pegasos2")           # fs.sync first, then QMP quit
```

`qemu.reset` stops the QEMU process and starts a fresh one, which is
the safe way to restart a guest.

## Files (`fs.*`)

```python
fs_list(target="x5000-real", path="Work:", recursive=True, max_depth=2)
fs_stat(target="x5000-real", path="S:Startup-Sequence")
fs_copy(target="x5000-real", src="S:Startup-Sequence", dst="RAM:ss.bak")
fs_protect(target="x5000-real", path="RAM:my_test", bits=0)  # raw mask; 0 = rwed allowed
fs_hash(target="x5000-real", path="RAM:my_test")      # SHA-256
fs_delete(target="x5000-real", path="RAM:old", recursive=True)
```

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

`transfer` picks how the bytes travel:

- **`"auto"` (default)** — on MCPd 2.3 and later, uploads of 1 MiB or
  more and downloads of any size go as a **raw byte stream**
  (`fs.stream_put` / `fs.stream_get`): no base64, no round trip per
  chunk, 64-bit offsets. If the connection breaks mid-stream, the
  wrapper reconnects and continues from the bytes already moved. At
  the end it compares a CRC-32 of the bytes with the daemon's
  (`fs.stream_status`); the result reports `crc32_verified: true`
  when one stream carried the whole file and the two matched.
  Smaller uploads, and every transfer to an older daemon, use the
  chunked path below.
- **`"stream"`** — always stream; refused with `NotCapable` on a
  daemon older than 2.3.
- **`"chunked"`** — never stream.

The result's `transfer` field says which path ran. The chunked path:

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

`chunk_size` and `compression` apply to the chunked path only. On
either path:

- **Optional resume** — `resume=True` probes the remote /
  local file size and continues from the partial state.
- **Optional verify** — `verify=True` adds an `fs.hash`
  round-trip + a local hashlib walk and raises on mismatch.

While any transfer method runs (`fs.read`, `fs.write`,
`fs.write_chunk`, `fs.stream_get`, `fs.stream_put`), MCPd keeps the
screen blanker from starting, and allows it again 30 s after the last
transfer ends. A blanker starting mid-transfer could slow an upload
to a stall. The user's blanker settings are not changed.

For library callers, the same surface is reachable via
`amiga_fleet_mcp.tools.fs.fs_upload` / `fs_download`.

## Live debugging

Per-task introspection uses the AmigaOS `IDebug` interface under
`Forbid()`. `debug.task_snapshot` captures registers, traptype, DAR /
DSISR, and a frame-chain backtrace. `debug.stacktrace` adds
symbolication via `IDebug->StackTrace`. `debug.symbol` resolves an
arbitrary address to module / function / source.

```python
debug_task_snapshot(target="x5000-real", name="Workbench")
debug_stacktrace(target="x5000-real", name="MyProgram", max_frames=32)
debug_symbol(target="x5000-real", address=0x7F3A1C40)
```

For QEMU targets, the `gdb` channel exposes whole-system register and
memory access through QEMU's GDB stub. Add a
`[targets.<name>.channels.gdb]` block (endpoint default
`127.0.0.1:1234`) and `qemu.start` adds `-gdb tcp::<port>` to the
QEMU command line:

```python
debug_read_registers(target="qemu-pegasos2")
debug_set_breakpoint(target="qemu-pegasos2", addr=0x01234560)
debug_continue(target="qemu-pegasos2", timeout_s=10)
debug_backtrace(target="qemu-pegasos2")
debug_detach(target="qemu-pegasos2")            # let the guest run on
```

## Running commands (`exec.*`)

Every `exec_cmd` runs as a **job** on the target. The result carries
a `job_id`, the process id, the exact exit code and `result2` (the
command's IoErr):

```python
exec_cmd(command="List", args=["SYS:C"])
# -> { state: "exited", exit_code: 0, result2: 0, job_id: 17,
#      output: "...", ... }
```

**A timeout does not stop the command.** If it is still running when
`timeout_s` (default 30) runs out, the result comes back with
`state: "running"`, `exit_code: null` and its `job_id`, and the
command carries on. Do not run it again — that starts a second copy.
Follow it up instead:

```python
exec_status(job_id=17)            # running or exited, exit code, elapsed time
exec_output(job_id=17, offset=0)  # read its output so far, then from `offset`
exec_kill(job_id=17)              # send CTRL-C (signal="d"/"e"/"f" for the others)
exec_list()                       # the last 32 jobs, newest first
```

Two limits are worth knowing. AmigaDOS often writes a command's
output out only when the command ends, so `exec_output` on a running
job may show less than it has printed. And `exec_kill` only sends a
break signal: a program that ignores breaks keeps running, and the
result says `exited: false`.

The host never resends a request that may already have run; an error
that leaves that open says so in `data.may_have_run`.

### Priorities

Jobs run at task priority **-2** by default, below ordinary programs
at 0. MCPd's connection workers run at **+1**, so the daemon keeps
answering even while a job — or anything else at the normal
priority — uses the whole CPU. Set a job's priority per call with
`priority` (on `exec_cmd`, `tests.run_suite` and `sandbox.run_batch`),
or for every call in the config:

```toml
[defaults]
exec_priority = -2
```

The daemon caps a job's priority at 0, one below its workers, and
at -20 at the bottom. Programs a job starts with `Run` begin at
priority 0 whatever the job's priority.

### Sandboxed commands

`sandbox=true` runs `command` — a program path, not a Shell line —
under [SandboxVM](#driver--program-iteration-via-sandboxvm-sandbox),
as an ordinary job with the same id, status, output, kill and
priority. If the program crashes, the machine and MCPd survive, and
the result's `sandbox` block reports what happened:

```python
exec_cmd(command="RAM:my_test", sandbox=True, timeout_s=60)
# -> { ..., sandbox: { name: "...", end_reason: "trap", trap_kind: "DSI",
#      trap_lines: [<ip / dar / dsisr / registers>], stdout: "...", stderr: "" } }
```

`end_reason` is `exit`, `trap`, `aborted` or `timeout`. `trap_lines`
is SandboxVM's own trap report, taken from the kernel debug ring.
Optional `sandbox_extmem_mb`, `sandbox_window_mb` and
`sandbox_deny_libs` override the target's `[targets.<name>.sandbox]`
defaults, and `sandbox_time_limit_s` makes SandboxVM itself abort the
program after that many seconds.

A sandboxed job that ignores break signals can be stopped with
`exec_kill(job_id=..., force=True)`. That needs **SandboxVM 1.1** or
later (it aborts the guest and frees what it opened);
`sandbox_probe` reports the installed `sandboxvm_version` and
`abort_supported`. `force` is refused for jobs that are not
sandboxed. Sandboxed commands need an ExtMem machine (X5000, A1222,
X1000, QEMU AmigaOne), not Pegasos II.

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
whenever the guest's MCPd answers; reach for it directly
before anything else abrupt, such as `sys.cold_reboot` or cutting
power with `power.off`.

## System information (`sys.*`)

Read-only questions about a running machine are one call each:

```python
sys_version(target="x5000-real")      # Kickstart / Workbench versions
sys_memory(target="x5000-real")       # free / largest / total
sys_volumes(target="x5000-real")      # mounted volumes
sys_tasks(target="x5000-real")        # ready + waiting tasks
sys_hardware(target="x5000-real")     # CPU, caches, board probes
sys_lastalert(target="x5000-real")    # last alert, decoded
sys_alert_decode(code=0x8000000B)     # decode any alert code
sys_debug_ring(target="x5000-real", since_s=60)   # kernel debug ring
```

On real X5000 / A1222 boards there are also SoC and MCU readers:
`sys.mcu_cmd` (X5000 Cyrus MCU, e.g. `cmd="t"` for temperatures),
`sys.read_ccsr`, `sys.tlb_dump` and the dangerous `sys.read_pa`.
`sys.cold_reboot(confirm=true)` reboots the target. Flush first with
`fs.sync`. [COMMANDS.md](COMMANDS.md) lists every `sys.*` tool.

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

The host refuses earlier than that by default: `input.type` takes at
most 128 characters (`[targets.<n>.input] max_text_len`; typing costs
about two events per character, so the 256-event cap binds before the
daemon's 512), and a call that needs more than `max_events` (256)
events is refused before anything is sent. Text must be Latin-1;
other characters are refused by name.

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

While a board is powered off, only `power.on` and `power.off` are
safe: any other MCU command can wedge a cold MCU until mains power is
cut. So `power.help`, `identify`, `identify_dates`, `sensors`,
`toggle_stream` and `shell` refuse when the target's MCPd does not
answer. Pass `force=True` only when you know the board is on.

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
# -> { exit_code: -768, trap_kind: "DSI", ... }     (SandboxVM 1.0)
# -> { exit_code: 50, trap_kind: "DSI", ... }       (SandboxVM 1.1: 50 = any trap; kind read from the debug ring)
sandbox.last_trap(target="x5000")
# -> { found: true, trap_kind: "DSI", traptype_hex: "0x300",
#      raw_lines: [<full trap dump from the kernel ring>], ... }
# -- daemon survived the crash; the next sandbox call works
# without intervention.
```

SandboxVM 1.1 passes a guest's own positive return code through
(capped at 127) but exits 50 for any trap, 51 when aborted and 52 when
its `-T` time limit runs out. On a 50, `run_guest` / `run_driver` (and
`exec_cmd` with `sandbox=true`, see
[Sandboxed commands](#sandboxed-commands)) read the trap kind from
SandboxVM's report in the debug ring, matched to the run by its name.

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
- `exec_cmd` with `sandbox=true` runs one program sandboxed as a
  tracked job you can poll and stop — see
  [Sandboxed commands](#sandboxed-commands).
- `tests.run_suite` is the right tool for JSON-config-driven
  multi-step test bundles **without** the SandboxVM harness.

## Serial capture (`serial.*`)

`serial.*` records a target's debug UART into a host-side log in the
background, so boot and crash output is kept even when the machine
(or MCPd) is down. Configure the port once per target; the default
channel `uboot` is the X5000's rear-panel DB9 (U-Boot console and
kernel debug), and `mcu` is the internal MCU header:

```toml
[targets.x5000-real.channels.uboot]
port = "COM7"            # or /dev/ttyUSB0
baud = 115200
```

```python
serial_start(target="x5000-real", truncate=True)   # fresh log
# ... reboot, reproduce the crash ...
serial_tail(target="x5000-real", max_bytes=8192)   # last 8 KiB
serial_read(target="x5000-real", offset=0)         # page through it
serial_stop(target="x5000-real")
```

Start the capture before a risky reboot: the kernel debug output it
records does not depend on the debug ring, which can be full (see
[Reading on-target debug output](#reading-on-target-debug-output)).
The `mcu` channel shares its cable with `power.*`, which returns
`NotCapable` while a capture holds it.

## Installing AmigaOS 4.1 FE (`installer.*`)

`installer.*` installs AmigaOS 4.1 Final Edition onto a blank volume
of a running target, from install ISOs and update LHAs in a host
directory. Set the usual values once in [`[defaults]`](#per-tool-defaults),
then check, stage, run and verify:

```python
installer_list_machines()                # supported machines
installer_scan_sources()                 # what sources_dir holds
installer_preflight(target="x5000-real") # safety check, read-only
installer_stage(target="x5000-real", confirm=True)   # upload to <dest>:tmp/
installer_run(target="x5000-real")                    # dry run: the plan only
installer_run(target="x5000-real", dry_run=False, confirm=True)
installer_verify(target="x5000-real")    # check the installed files
```

`installer.run` supports X5000, A1222, AmigaOne, Pegasos II and
Sam460. The building blocks (`mount_iso`, `copy_tree`, `apply_lha`,
`read_kicklayout` / `write_kicklayout` / `patch_kicklayout`) are
tools of their own; mutating ones need `confirm=True`.

## Test suites (`tests.*`)

`tests.*` runs suites from an AmigaQemuTests checkout, set as
`[paths] amiga_qemu_tests` (see [Helper paths](#helper-paths-paths)):

```python
tests_list_suites()                                   # config/projects/*.json
tests_run_suite(target="qemu-pegasos2", suite="<suite>")
tests_run_standard_dos_tests(target="qemu-pegasos2")  # the dos_basic suite
tests_parse_output(output_b64="...")                  # [PASS]/[FAIL] lines
```

Suite commands run as tracked jobs at `[defaults] exec_priority`
(or `priority=`).

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

Run the scripts with the Python from `host/.venv`: most import
`amiga_fleet_mcp`, and some import `qmp_client` / `serial_client` from
the `qemu-runner` package, which is installed only as a host
dependency. Each script accepts `--help` for the full flag list. `--endpoint`
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
