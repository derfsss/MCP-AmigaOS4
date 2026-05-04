# Change log

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
