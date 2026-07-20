# Change log

## 1.3 — Keyboard and mouse injection

Wire protocol stays at 1.0: `input.*` is purely additive, and
`proto.capabilities` derives its method list dynamically, so framing,
error codes, and notification shape are unchanged. An older host talks
to a 1.3 daemon fine; an older daemon reports no `input` capability.

### Added

- **`input.*` — keyboard and mouse injection**, via `input.device`
  `IND_WRITEEVENT` on the Amiga side. Seven methods: `input.state`
  (read-only pointer / focus / geometry), `input.type`, `input.key`,
  `input.mouse_move`, `input.click`, `input.drag`, `input.scroll`.
  Works on real hardware and QEMU alike since it goes through MCPd
  rather than QMP. Registered as seven fine-grained MCP tools plus an
  `input` namespace dispatcher.
- **Disabled by default at both layers.** The daemon gate is the real
  control: `input.*` returns `-32003` unless MCPd was started with the
  new `--enable-input` flag *or* the sentinel file
  `SYS:System/MCPd/ENABLE-INPUT` exists. The gate is read once at
  startup, so no RPC method can switch it on — enabling requires
  filesystem access to the target and a daemon restart. The sentinel
  is the primary mechanism because `MCPd-Watchdog` relaunches MCPd
  with no arguments, so a CLI-flag-only gate would be silently lost on
  the first restart. `proto.capabilities` now reports
  `input.enabled`, and the methods stay advertised when off so clients
  can distinguish "switched off" from "old daemon".
- **Host-side gate**: new `[targets.<name>.input]` config block
  (`enabled`, `max_text_len`, `max_events`, `default_delay_ms`,
  `allow_drag`), defaulting to absent. Raises `NotCapable` naming both
  gates. The `--init` wizard asks about it, defaulting to no.
- **New install scripts** `MCPd-Enable-Input` / `MCPd-Disable-Input`,
  copied by `MCPd-Install` but never run by it.
- `confirm: true` required on the committing operations (`type`,
  `key`, `click`, `drag`); not on `mouse_move`, `scroll`, `state`.
  `ctrl+lamiga+ramiga` additionally requires `confirm_reset: true`
  because it reboots the machine.
- Per-call caps enforced daemon-side: 256 events, 20 s wall clock, 512
  characters, 64 drag steps, `delay_ms` 0–1000. Held modifiers and
  mouse buttons are always released before returning, including on the
  abort path, so a truncated drag cannot leave a stuck mouse button.
- `CLAUDE.md` — repo orientation notes for AI assistants.

- **Layout-correct typing.** `input.type` maps characters through the
  target's `keymap.library` (`MapANSI`), including dead-key sequences,
  so non-US keymaps receive the intended characters. Verified
  end-to-end on a German (QWERTZ) AmigaOS 4.1 FE guest: typing
  `Echo TYPED-OK >T:typed.txt` into a Shell produced exactly that
  file. `keymap="us"` forces the built-in table, which is also the
  automatic fallback; the result reports which path ran.

### Verified on hardware

Exercised against a QEMU Pegasos2 guest running AmigaOS 4.1 FE
Update 3 (Kickstart 54.57):

- gate closed by default — all 7 methods return `-32003`,
  `proto.capabilities` reports `input.enabled: false`
- `MCPd-Enable-Input` → restart → gate open; `MCPd-Disable-Input` →
  restart → closed again
- absolute pointer moves land exactly on target; relative moves and
  wheel scroll confirmed
- keyboard injection confirmed end-to-end through a Shell
- daemon-side `confirm` gates and the `ctrl+lamiga+ramiga`
  `confirm_reset` guard all reject as designed

### Known limitations

- F11/F12 rawkey codes and the NewMouse wheel constants are still
  flagged in `mcpd/src/methods/input.c` as unverified.
  `input.mouse_move` defaults to a read-position-then-relative-delta
  strategy (verified accurate); `absolute_mode="raw"` remains
  available to A/B the true-absolute event form.
- `input.click` does not report the achieved pointer position the way
  `input.mouse_move` and `input.drag` do.

## 1.2 — Guided setup and whole-file transfers

### Added

- **`amiga-fleet-mcp --init`** — guided setup wizard. Walks
  through `[server]`, `[paths]`, `[targets.*]` (one or more), and
  `[defaults]`, then validates the generated TOML through the same
  Pydantic schema the server uses at startup before writing it to
  the platform default location (or wherever `--config` points).
  Supports `--force` to overwrite without prompting and
  `--non-interactive` for CI smoke tests. 10 new unit tests cover
  the TOML emitter (round-trips through `tomllib`, handles
  backslashes / quotes / quoted keys) plus four scripted-prompt
  flows (QEMU target, remote + FTDI MCU cable, abort-on-overwrite,
  zero-targets).
- **`[paths]` documentation** — new "Helper paths" subsection in
  USAGE.md explains which tool surface needs each `[paths]` entry
  (`qemu_runner` → `qemu.*` + QMP, `amiga_qemu_tests` → `tests.*`,
  `qemu_binary` → `qemu.start`). Error messages from those tool
  surfaces now point at this section and at `--init`.
- **AGENTS_SETUP.md** — deterministic setup spec for AI agents.
  Decision tree, detection commands, minimal config templates per
  scenario, validation steps, and what each common error means.
- **INSTALL.md venv command** — split `python` into `python3` /
  `py -3` to match modern Linux/macOS defaults and the Windows
  launcher convention.
- **`host/config.example.toml`** — `[paths]` entries are now
  commented-out by default with per-key "required by tool surface
  X" notes (the un-commented `<placeholder>` form previously made
  them look mandatory).

- **`fs.upload` and `fs.download`** — whole-file transfer
  wrappers that hide the chunking + base64 + zlib mechanics.
  Point at a single file in either direction; works for any
  size and any byte content (binary-clean):
  - **`fs.upload(target, local_path, remote_path, ...)`**:
    auto-chunks files larger than fit in one JSON-RPC frame,
    auto-base64-encodes each chunk, auto-zlib-compresses
    when `compression="auto"` (default) and the result is at
    least 5% smaller. Skips pre-compressed .lha / .zip / .iso
    payloads automatically. No separate reassembly step —
    chunks land at their byte offsets in the destination file
    via `fs.write_chunk`.
  - **`fs.download(target, remote_path, local_path, ...)`**:
    auto-pages via repeated `fs.read(offset, length)` calls,
    auto-base64-decodes on receipt.
  - Both support `resume=True` (continues from the existing
    on-target / on-disk size) and `verify=True` (SHA-256
    both sides via `fs.hash` + local hashlib walk; raises on
    mismatch).
  - Available as fine-grained tools `fs_upload` / `fs_download`
    and via the `fs` namespace dispatcher
    (`fs(method="upload" / "download", params={...})`).
- 15 new unit tests under `tests/unit/test_fs_transfer.py`
  covering single-chunk + multi-chunk + auto-zlib + resume +
  verify paths, plus a binary-clean round-trip with all 256
  byte values to prove the auto-base64 path doesn't corrupt
  binary content.

### Tool count

- 118 → 121 tools (+`fs.upload`, +`fs.download`, +1 from the
  fine-grained registration shape).

## 1.1 — Out-of-band power control

### Added

- **`power.*` namespace** — host-side driver for the Amiga's
  internal MCU debug shell (X5000 P18 / A1222 P15) over an FTDI
  USB-TTL cable wired to the host. Bypasses MCPd entirely, so the
  tools work regardless of AOS / MCPd state. Eight tools:
  `power.help`, `power.identify`, `power.identify_dates`,
  `power.sensors`, `power.toggle_stream`, `power.on`, `power.off`,
  `power.shell`. The four destructive ones (`on` / `off` /
  `toggle_stream` / `shell`) require `confirm: true` — same
  accidental-fire guard as `sys.cold_reboot`.
- **`power.on` is the only software path to boot a fully-off
  X5000.** Empirically a full off-then-boot cycle (`power.off` →
  `power.on` → MCPd reachable) takes ~80 s on real X5000.
  Recovery channel for wedged-network situations where
  `sys.cold_reboot` can't reach the daemon any more.
- New `[targets.<name>.channels.mcu]` configuration block
  surfaces the FTDI USB-TTL cable to the new namespace
  (`port`, `baud = 38400`). The schema field already existed; v1.1
  adds the first consumer.

### Tool count

- 109 → 118 tools (+8 fine-grained `power.*` + 1 namespace
  dispatcher).

### Internal

- New host-side `transports/p18.py` async pyserial driver.
- `tools/power.py` with channel-resolution + confirm-gate
  helpers; integrates with the existing `serial.*` capture
  registry (returns `NotCapable` if the same port is being
  captured, since the two surfaces share the underlying file
  handle).
- 18 new unit tests under `tests/unit/test_power.py` exercising
  every confirm path + every channel-resolution edge case with a
  mock-patched transport.

## 1.0 — Initial public release

First public release of MCP-AmigaOS4.

### Highlights

- **Two-piece architecture**: `amiga-fleet-mcp` (Python MCP server,
  host-side) + `MCPd` (C daemon, runs on each AmigaOS 4 target).
  JSON-RPC 2.0 over framed TCP on port 4322.
- **More than 100 typed MCP tools** across `fs.*`, `exec.cmd`,
  `sys.*`, `wb.*`, `debug.*`, `qemu.*`, `fleet.*`, `tests.*`,
  `events.*`, `app.notify`, `notify.*`, `installer.*`, and
  `serial.*`.
- **Multi-target fleets**: `fleet.run_on_all`, `fleet.barrier`,
  `fleet.quorum_run`, `fleet.relay`, all with optional tag
  filters. Works across any mix of QEMU guests and real hardware
  in one session.
- **LAN-local discovery** via UDP broadcast on port 4323
  (`fleet.discover`).
- **QEMU lifecycle + snapshots**: `qemu.start` / `stop` / `reset`
  / `screenshot` / `savevm` / `loadvm` / `list_snapshots` /
  `delete_snapshot`.
- **Live debug**: per-task IDebug snapshots + symbolicated stack
  traces on a real Amiga, plus whole-system register and memory
  access through QEMU's GDB stub on emulated targets.
- **Native AmigaOS 4.1 FE installer pipeline** (`installer.*`):
  preflight, ISO mount, recursive copy, LHA extraction,
  Kicklayout patching, staged upload, post-install verification,
  and per-machine sequences.
- **Live X5000 hardware introspection**: Cyrus MCU supervisor
  protocol over UART1 (`sys.mcu_cmd`), Freescale QorIQ CCSR reads
  (`sys.read_ccsr`), TLB1 dump (`sys.tlb_dump`), and (with care)
  arbitrary supervisor-mode physical-address reads
  (`sys.read_pa`).
- **Auto-start install** (`scripts/install_mcpd_autostart.py`):
  drops MCPd into `SYS:System/MCPd/`, registers a watchdog, and
  patches `S:Network-Startup`. Boot-to-bind ≈ 11 s on an X5000.
- **Per-tool parameter defaults** via a `[defaults]` block in
  `config.toml` (`dest_volume`, `sources_dir`, `bootstrap_dir`,
  `machine`, `iso_filename`). Set frequently-repeated values
  once and skip them in subsequent tool calls.

### Supported targets

- **QEMU**: Pegasos2 (validated end-to-end), AmigaOne, SAM460ex.
- **Real hardware**: AmigaOne X5000 (validated, Freescale P5020
  / E5500). AmigaOne A1222 / Tabor and AmigaOne X1000 / Nemo are
  recognised by board detection but not yet exercised in CI.

### Build environment

MCPd's cross-compile build is reproducible via the
`walkero/amigagccondocker:os4-gcc11` Docker image: AmigaOS 4.1 FE
SDK 54.16 with GCC 11.5. The exact SDK identifier is embedded in
each MCPd binary and surfaced via `proto.version` /
`proto.capabilities` `build.sdk`.
