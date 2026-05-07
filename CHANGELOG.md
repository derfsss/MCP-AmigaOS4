# Change log

## Unreleased

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
