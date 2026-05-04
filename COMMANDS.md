# Command reference

Every command, MCP tool, MCP resource, RPC method, and helper script
the project ships, with a one-line description of each. This is the
authoritative single page; see [USAGE.md](USAGE.md) for narrative
context.

## Host CLI: `amiga-fleet-mcp`

The host server. Invoked by an MCP client over stdio, or directly
for diagnostics.

| Flag | Purpose |
|---|---|
| *(no flag)* | Start the MCP server. The default mode when invoked by an MCP client. |
| `--version` | Print the host package version and exit. |
| `--config PATH` | Path to a `config.toml`. Defaults to `$AMIGA_FLEET_CONFIG` or the platform default location. |
| `--inspect` | Load the config, list configured targets, and exit. Does not start MCP. |
| `--health-check` | Probe every configured target's MCPd channel; report advertised methods plus AmigaOS version per target. Exits non-zero if any target is unreachable. |
| `--list-tools` | Print every registered MCP tool name + title + first line of its docstring. Does not start MCP. |
| `--list-resources` | Print every registered MCP resource URI / template + title + mime type. Does not start MCP. |
| `--discover` | UDP-broadcast probe for MCPd instances on the LAN. Prints discovered targets and exits. Does not start MCP. |
| `--discover-timeout-ms N` | Window to wait for discovery responses. Default `1500`. |

## MCPd command-line arguments

The Amiga-side daemon. Invoked from a Shell on the target or via the
auto-start watchdog.

| Flag | Purpose |
|---|---|
| *(no flag)* | Bind on the default port and serve forever. |
| `--port N` | Listen on TCP port `N` instead of the default 4322. |
| `--version` | Print the daemon version and exit. |
| `--help`, `-h` | Print usage and exit. |

## RPC methods (daemon side)

Every method advertised by `proto.capabilities`. JSON-RPC 2.0 over a
4-byte big-endian length-prefix framed TCP socket on port 4322.

Some methods have target-specific prerequisites (Freescale QorIQ for
the CCSR / TLB / read_pa methods, `i2c.resource` /
`performancemonitor.resource` for the corresponding hardware probes,
the Cyrus MCU for `sys.mcu_cmd`, the QEMU GDB stub for whole-system
debug, source ISOs for the installer, a wired serial port for the
capture service). The full per-feature
breakdown lives in [INSTALL.md § Per-feature
prerequisites](INSTALL.md#per-feature-prerequisites).

### `proto.*`

| Method | Description |
|---|---|
| `proto.capabilities` | Server capability envelope: build metadata, methods, method names, namespace features, payload limits, board details. |
| `proto.version` | Server and protocol version. |

### `sys.*`

| Method | Description |
|---|---|
| `sys.version` | Kickstart and Workbench version strings. |
| `sys.tasks` | Ready and waiting tasks (name, type, priority, state). |
| `sys.libraries` | Opened libraries (name, version, revision, open count). |
| `sys.devices` | Opened devices in the same shape as `sys.libraries`. |
| `sys.ports` | Public message ports (name, priority, flags). |
| `sys.lastalert` | Most recent alert from `ExecBase->LastAlert`, with decoded subsystem / general / specific fields. |
| `sys.uptime` | Monotonic uptime via `ITimer->ReadEClock`. |
| `sys.memory` | Free / largest / total memory across `MEMF_ANY`, `MEMF_SHARED`, `MEMF_VIRTUAL`. |
| `sys.volumes` | Mounted volumes (LDF_VOLUMES walk). |
| `sys.assigns` | Active AmigaDOS assigns (LDF_ASSIGNS walk). |
| `sys.hardware` | CPU details, AttnFlags, and resource probes for board identification. |
| `sys.hardware.i2c` | Enumerate I²C buses and probe addresses via `i2c.resource`. |
| `sys.hardware.perfcounters` | Live CPU performance counters via `performancemonitor.resource`. |
| `sys.executable.symbols` | Read the ELF symbol table of a binary on the target via `elf.library`. |
| `sys.applications` | Applications registered with `application.library`. |
| `sys.alert_decode` | Decode an arbitrary AmigaOS alert code. |
| `sys.modules` | Loaded libraries / devices / resources with load base + size, for crash-address mapping. |
| `sys.crashhook_status` | Diagnostic counters for the daemon's IDebug crash hook (registered, fire count, exception count). |
| `sys.cold_reboot` | Software cold reboot via `IExec->ColdReboot`. Requires `confirm: true`; the response flushes before the reboot fires. |
| `sys.read_ccsr` | Read 32-bit register(s) from the Freescale QorIQ CCSR aperture (CPU PA `0xFE000000+offset`). Supervisor-mode read serialised under `Forbid()` / `Permit()`. Used for SoC introspection on real hardware. |
| `sys.read_pa` | DANGEROUS supervisor-mode read at any 36-bit physical address. No range checking; misuse can crash the connection-handling task (the daemon listener stays up by design). |
| `sys.tlb_dump` | Dump all 64 TLB1 entries from a P5020 / P1022 target via `tlbre` / MAS registers. |
| `sys.mcu_cmd` | X5000 Cyrus MCU supervisor protocol over `serial.device` unit 1 (38400 8N1). Documented commands `t` / `v` / `f` / `s` plus build-info `b`. The canonical sensor path on X5000 (board / CPU temperatures, voltage rails, fan PWM/RPM, soft power-off). |

### `fs.*`

| Method | Description |
|---|---|
| `fs.list` | Directory listing. Optional `recursive` + `max_depth`. |
| `fs.stat` | File or directory metadata. |
| `fs.read` | Read a file. Optional `offset` + `length` for chunked reads. |
| `fs.write` | Write a file from base64 payload. |
| `fs.delete` | Delete a path. Optional `recursive`. |
| `fs.makedir` | Create a directory. |
| `fs.rename` | Rename / move a file or directory. |
| `fs.protect` | Set protection bits on a path. |
| `fs.copy` | Copy a file (preserves protection and date via `CLONE`). |
| `fs.hash` | Streaming SHA-256 of a file. |

### `exec.*`

| Method | Description |
|---|---|
| `exec.cmd` | Run an AmigaDOS command. Optional `args[]`, `cwd`, `timeout_ms`. |

### `wb.*` (Workbench / Intuition)

| Method | Description |
|---|---|
| `wb.screens` | List public screens via `IntuitionBase` walk. |
| `wb.windows` | List windows across all screens. |
| `wb.publicscreens` | Public-screens registry via `LockPubScreenList`. |
| `wb.frontmost` | Frontmost screen, active screen, and active window. |

### `debug.*`

| Method | Description |
|---|---|
| `debug.task_snapshot` | Snapshot a named task: registers + frame-chain backtrace via `IDebug->ReadTaskContext`. |
| `debug.symbol` | Resolve an address to module / function / source via `IDebug->ObtainDebugSymbol`. |
| `debug.stacktrace` | Symbolicated backtrace of a named task via `IDebug->StackTrace`. |
| `debug.write_memory` | Write base64 bytes to an arbitrary memory address (requires `confirm: true`). |
| `debug.write_register` | Modify one register in a task's saved exception context (requires `confirm: true`). |

### `events.*`

| Method | Description |
|---|---|
| `events.wait` | Long-poll subscribed topics; returns on first delta or timeout. |
| `events.subscribe` | Server-push: register topics. Daemon emits JSON-RPC notifications on change. |
| `events.unsubscribe` | Server-push: clear the subscription. |
| `events.test_emit` | Synthesize a JSON-RPC notification (test path for the server-push wiring). |

### `app.*`

| Method | Description |
|---|---|
| `app.notify` | Post a Ringhio notification via `application.library`. |

## MCP tools (host side)

Each tool corresponds to one of the RPC methods above, with an MCP
schema for parameters and result. `amiga-fleet-mcp --list-tools`
prints the live list.

### Per-target tools

| Tool | Wraps |
|---|---|
| `proto_capabilities` | `proto.capabilities` |
| `sys_version` / `sys_tasks` / `sys_libraries` / `sys_devices` / `sys_ports` / `sys_lastalert` / `sys_uptime` / `sys_memory` / `sys_volumes` / `sys_assigns` / `sys_hardware` / `sys_hardware_i2c` / `sys_hardware_perfcounters` / `sys_executable_symbols` / `sys_applications` / `sys_alert_decode` / `sys_cold_reboot` / `sys_read_ccsr` / `sys_read_pa` / `sys_tlb_dump` / `sys_mcu_cmd` | `sys.*` |
| `fs_list` / `fs_stat` / `fs_read` / `fs_write` / `fs_delete` / `fs_makedir` / `fs_rename` / `fs_protect` / `fs_copy` / `fs_hash` | `fs.*` |
| `exec_cmd` | `exec.cmd` |
| `wb_screens` / `wb_windows` / `wb_publicscreens` / `wb_frontmost` | `wb.*` |
| `debug_task_snapshot` / `debug_symbol` / `debug_stacktrace` / `debug_write_memory` / `debug_write_register` / `debug_read_registers` / `debug_read_memory` / `debug_set_breakpoint` / `debug_clear_breakpoint` / `debug_step` / `debug_continue` / `debug_backtrace` / `debug_stop_reason` / `debug_detach` | `debug.*` (per-task IDebug + whole-system GDB stub) |
| `events_wait` | `events.wait` (long-poll only). The other three `events.*` methods are exposed only as daemon RPC; clients invoke them via the transport directly. |
| `app_notify` | `app.notify` |

### QEMU lifecycle tools (per-target, QEMU only)

| Tool | Description |
|---|---|
| `qemu_start` | Boot a target from its `qemu_config`. |
| `qemu_stop` | Stop the QEMU process via QMP. |
| `qemu_reset` | QMP `system_reset` (note: AmigaOS 4 guests do not reliably survive this; prefer stop + start). |
| `qemu_status` | Whether the QEMU process is alive. |
| `qemu_screenshot` | PNG of the framebuffer via QMP `screendump`. |
| `qemu_savevm` / `qemu_loadvm` / `qemu_list_snapshots` / `qemu_delete_snapshot` | Guest-state checkpoint / restore / inspect / delete. |

The captured QEMU serial output is exposed as the read-only resource
`amiga://{target}/qemu/serial_log`, not as a tool.

### Multi-target tools (`fleet.*`)

| Tool | Description |
|---|---|
| `fleet_list_targets` | List configured targets with their tags. |
| `fleet_target_status` | Per-target reachability and active channels. |
| `fleet_snapshot` | One-call health summary across the entire fleet. |
| `fleet_run_on_all` | Run a method on every (or tag-filtered) target in parallel. |
| `fleet_barrier` | Same as `run_on_all` but with a per-target timeout. |
| `fleet_quorum_run` | Require at least *N* of *M* targets to succeed. |
| `fleet_relay` | Copy a file from one target to another via the host. |
| `fleet_discover` | UDP-broadcast probe for MCPd instances. |

### Notification helpers

| Tool | Description |
|---|---|
| `notify_fleet` | Broadcast a Ringhio popup to every (or tag-filtered) target in parallel. |
| `notify_on_alert` | Long-poll a target's `debug.exception` topic; post a popup when one fires. |

### Test orchestration (`tests.*`)

| Tool | Description |
|---|---|
| `tests_list_suites` | Enumerate available test suites from the AmigaQemuTests harness. |
| `tests_run_suite` | Run a specific suite. |
| `tests_run_standard_dos_tests` | Convenience wrapper around the standard DOS test set. |
| `tests_parse_output` | Parse harness output into structured pass / fail counts. |

### Native installer (`installer.*`)

End-to-end AmigaOS 4.1 FE install pipeline.

The frequently-repeated parameters (`dest_volume`, `sources_dir`,
`bootstrap_dir`, `machine`, `iso_filename`) can be defaulted in
`config.toml` under `[defaults]` so a typical session does not have
to repeat them on every call — see [USAGE.md § Per-tool
defaults](USAGE.md#per-tool-defaults).

| Tool | Description |
|---|---|
| `installer_list_machines` | List supported target machines with their canonical IDs, friendly names, and aliases. |
| `installer_required_files` | For a given machine, the file manifest the installer expects under `sources_dir`. Honours `[defaults] machine`. |
| `installer_scan_sources` | Walk a host-side directory and report which install files are present, ambiguous, or unsupported. Honours `[defaults] sources_dir`. |
| `installer_preflight` | Composite pre-install safety check: `sources_dir` validity, machine resolution, dest-volume mount + cleanliness, mandatory-binaries availability. Read-only. Honours `[defaults] dest_volume / sources_dir / machine`. |
| `installer_stage` | Upload ISO + LHAs + `diskimage-bootstrap/` into `<dest>:tmp/`. Multi-GB upload; requires `confirm: true`. Honours `[defaults] dest_volume / sources_dir / bootstrap_dir / machine / iso_filename`. |
| `installer_mount_iso` | Mount an ISO via `diskimage.device` + `MountDiskImage`. Reversible via `installer_unmount_iso`. |
| `installer_unmount_iso` | Eject an ISO from a `diskimage.device` unit. Idempotent. |
| `installer_copy_tree` | Recursive AmigaDOS `Copy ALL CLONE QUIET` between paths on a target. |
| `installer_apply_lha` | Extract an LHA archive to a destination directory via `LhA x`. |
| `installer_read_kicklayout` | Read `<dest_volume>Kickstart/Kicklayout` as text. Honours `[defaults] dest_volume`. |
| `installer_write_kicklayout` | Write Kicklayout content; backs up the existing file first. Honours `[defaults] dest_volume`. |
| `installer_patch_kicklayout` | Atomic read-modify-write of Kicklayout: add modules + verbatim text replacement. Honours `[defaults] dest_volume`. |
| `installer_install_x5000` | Run the AmigaOne X5000 install sequence end-to-end. Defaults to `dry_run=True`. Honours `[defaults] dest_volume / sources_dir / iso_filename`. |
| `installer_run` | Dispatcher to any per-machine install sequence. Honours `[defaults] dest_volume / sources_dir / machine / iso_filename`. |
| `installer_verify` | Walk the per-machine post-install manifest and verify each file is present (with optional SHA-256). Honours `[defaults] dest_volume / machine`. |

### Serial capture (`serial.*`)

Host-side capture of a target's debug UART (for example, an
X5000's rear-panel DB9). Useful for reading kernel-debug output
during boot or crash investigation without tying up the cable in a
terminal program.

| Tool | Description |
|---|---|
| `serial_start` | Open the configured serial port and start a background capture to a host-side log. |
| `serial_stop` | Stop a running capture. Idempotent. |
| `serial_status` | List active captures. Optional filters by target and port. |
| `serial_read` | Read bytes from a capture log starting at `offset`. |
| `serial_tail` | Read the last `max_bytes` of a capture log. |
| `serial_clear` | Truncate the capture log. Capture must be stopped. |

### Power control (`power.*`)

Host-side driver for the X5000 / A1222 internal MCU debug shell,
reached via the FTDI USB-TTL header on the Amiga motherboard
(X5000 P18, A1222 P15). Bypasses MCPd entirely — works even when
AOS / MCPd are off or wedged. `power.on` is the only software path
to boot a fully-off X5000.

Requires `[targets.<name>.channels.mcu]` configured with the host
serial port + 38400 baud. Honours `[server] default_target`.

| Tool | Confirm | Description |
|---|---|---|
| `power_help` | — | List the MCU shell's commands (`help`). |
| `power_identify` | — | MCU H/W + F/W revisions (`id`). |
| `power_identify_dates` | — | MCU + CPLD build date and time (`id date`). |
| `power_sensors` | — | Voltages + temperatures, human-formatted (`v`). For the structured wire form, use `sys.mcu_cmd cmd="v"`. |
| `power_toggle_stream` | **yes** | Toggle continuous-emission state (`q`). Optional `watch_s` to capture for N seconds and auto-toggle off. |
| `power_on` | **yes** | Power up all supplies (`p`). Boots a powered-off X5000; **resets if already on**. |
| `power_off` | **yes** | Shut down all supplies (`s`). |
| `power_shell` | **yes** | Generic shell-command passthrough (escape hatch). |

### Namespace dispatchers

Each major namespace also has a single dispatcher tool that takes a
`method` argument and a parameter dict. They cover the same surface
as the fine-grained tools above and are useful when a client wants
to script an operation by method name rather than by tool name.

| Dispatcher | Routes to |
|---|---|
| `fs` | `fs.*` |
| `sys` | `sys.*` |
| `wb` | `wb.*` |
| `debug` | `debug.*` |
| `qemu` | `qemu.*` |
| `fleet` | `fleet.*` |
| `tests` | `tests.*` |
| `events` | `events.*` |
| `app` | `app.*` |
| `installer` | `installer.*` |
| `serial` | `serial.*` |
| `power` | `power.*` |

## MCP resources

Live, read-only data exposed under the `amiga://` URI scheme.

| URI | Description |
|---|---|
| `amiga://fleet/targets` | List of configured targets and their tags. |
| `amiga://{target}/sys/version` | Per-target Kickstart / Workbench snapshot. |
| `amiga://{target}/sys/tasks` | Per-target live task list. |
| `amiga://{target}/proto/capabilities` | Per-target daemon capability report. |
| `amiga://{target}/qemu/serial_log` | Tail of captured QEMU serial output (QEMU targets only). |
| `amiga://runs/` | Index of archived tool-call runs. |
| `amiga://runs/{run_id}` | Metadata for a specific run. |
| `amiga://runs/{run_id}/calls` | NDJSON of tool calls in a run. |

## Helper scripts

Located in `scripts/`. Run with `python scripts/<name>.py`.

### Install / lifecycle

| Script | Description |
|---|---|
| `install_mcpd_autostart.py [endpoint] [binary]` | Drop MCPd at `SYS:System/MCPd/`, upload the watchdog, patch `S:Network-Startup`. Idempotent. |

### Validation

| Script | Description |
|---|---|
| `validate.py --endpoint host:port [--rounds 1,2,3,4,5]` | Parametric runner for the validation suites against any endpoint. |
| `validate_full.py --endpoint host:port [--skip-stress] [--skip-mcp]` | Comprehensive capability sweep covering connectivity, fs, exec, sys, wb, debug, events, application library, edge cases, stress, and the host MCP protocol layer. |
| `validate_durability.py --endpoint host:port [--skip-long]` | Out-of-the-box durability sweep: raw-socket frame fuzzing, JSON-RPC envelope abuse, error-code coverage, connection lifecycle, long-running stability, filesystem corner cases, exec.cmd corners, subscription state transitions, concurrency, numeric boundaries, cross-method workflows, MCP resources, fleet host tools, CLI flags. |
| `round1_validation.py` … `round5_serverpush_validation.py` | The individual suites; standalone, hardcoded for the canonical test endpoint. |
| `qemu_install_test.py` | End-to-end: launch QEMU, bootstrap, install, kill+relaunch, verify auto-start, run all rounds. |
| `concurrent_x5000_qemu.py` | Drive an X5000 and a QEMU pegasos2 guest simultaneously; verifies fleet fan-out, relay, barrier. |
| `x5000_probe.py` | Quick reachability + capability probe on the X5000. |
| `x5000_method_sweep.py` | Walk every advertised method and exercise it. |

### Installer

| Script | Description |
|---|---|
| `run_installer_x5000.py` | Drive an end-to-end X5000 install via `installer_run` (defaults to dry-run). |
| `run_installer_stage.py` | Drive `installer_stage` to upload an ISO + LHAs + `diskimage-bootstrap/` to a target. |
| `deploy_mcpd_x5000.py` | One-shot deploy of a freshly built MCPd to a running X5000 (auto-start install + watchdog). |

### Diagnostics and probes

| Script | Description |
|---|---|
| `tail_serial.py` | Live tail a target's serial-capture log without booting an MCP client. |
| `validate_serial_tools.py` | Smoke-test the host-side `serial.*` capture lifecycle. |
| `investigate_crash.py` | Pull a crash snapshot via `debug.*` after a target has alerted. |
| `test_crashhook.py` | Force the IDebug crash hook to fire and verify recovery. |
| `test_events_soak.py` | Long-running event-channel soak test. |
| `probe_x5000_uboot.py`, `probe_x5000_mcu.py`, `probe_mcu_debug.py`, `probe_x5000_netinterfaces.py` | Hardware-specific probes used during X5000 bring-up. |
| `fetch_x5000_kickstart.py` | Pull the live `SYS:Kickstart/` tree off a real X5000 for offline analysis. |
| `fix_a1222_network_startup.py`, `fix_x5000_disk_info_and_neticon.py` | Targeted fix-up scripts for known target-specific quirks. |

## AmigaDOS install scripts

Located in `mcpd/install/`. Run from a Shell on the target with
`Execute <name>`.

| Script | Description |
|---|---|
| `MCPd-Install` | Install MCPd to `SYS:System/MCPd/MCPd`, set protection bits, back up `S:Network-Startup`, idempotently append the launch line. |
| `MCPd-Uninstall` | Restore `S:Network-Startup` from backup; remove `SYS:System/MCPd/`. |
| `MCPd-Watchdog` | Relaunch wrapper: runs MCPd in a loop with a five-second back-off between exits. Writes `T:MCPd-Watchdog.log`. |

## Build commands

| Command | Description |
|---|---|
| `cd mcpd && make docker-build` | Cross-compile MCPd via the `walkero/amigagccondocker:os4-gcc11` Docker image. |
| `cd mcpd && make all` | Cross-compile MCPd using a locally-installed `ppc-amigaos-gcc`. |
| `cd mcpd && make clean` | Remove built artefacts. |
| `cd host && uv sync` | Install host dependencies into `.venv/`. |
| `cd host && uv build` | Build a wheel + sdist into `host/dist/`. |
| `cd host && uv run pytest tests/unit -q` | Run the unit-test suite. |
| `cd host && uv run mypy src/amiga_fleet_mcp` | Strict type-check. |
| `cd host && uv run ruff check src` | Lint. |
