# Change log

## Unreleased

### Changed

- Documentation pass across the README and everything it links to,
  checked against the live tool surface rather than against itself.
  `z.library`, `keymap.library` and `graphics.library` are now listed
  as target requirements; `input.*`, `sandbox.*` and `wb.screenshot`
  have per-feature prerequisite entries; the README gained a quick
  start; and `CONTRIBUTING.md` now says to branch from `develop`
  rather than the protected release branch.
- Two behaviours that are easy to misread are now written down: the
  AmigaOS kernel debug buffer does not wrap (so `sys.debug_ring` can
  be blind to everything logged after boot on a machine with a
  verbose driver), and only `power.on` / `power.off` are safe to send
  to the MCU while a board is powered off.

## 1.3 — Sandboxed iteration, screen capture, and input injection

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
  copied but never run -- by `MCPd-Install`, by `installer.stage` +
  the `install_mcpd` sequence step, and by
  `scripts/install_mcpd_autostart.py`, so a machine provisioned any of
  the three ways has the documented persistent way to open the gate.
- `confirm: true` required on the committing operations (`type`,
  `key`, `click`, `drag`); not on `mouse_move`, `scroll`, `state`.
  `ctrl+lamiga+ramiga` additionally requires `confirm_reset: true`
  because it reboots the machine.
- Per-call caps enforced daemon-side: 256 events, 20 s wall clock, 512
  characters, 64 drag steps, `delay_ms` 0–1000. Held modifiers and
  mouse buttons are always released before returning, including on the
  abort path, so a truncated drag cannot leave a stuck mouse button.

- **Layout-correct typing.** `input.type` maps characters through the
  target's `keymap.library` (`MapANSI`), including dead-key sequences,
  so non-US keymaps receive the intended characters. Verified
  end-to-end on a German (QWERTZ) AmigaOS 4.1 FE guest: typing
  `Echo TYPED-OK >T:typed.txt` into a Shell produced exactly that
  file. `keymap="us"` forces the built-in table, which is also the
  automatic fallback; the result reports which path ran. Text is
  decoded from UTF-8 to codepoints before mapping (both mapping paths
  are one-byte ANSI), so an accented character is one keystroke rather
  than one per UTF-8 byte; codepoints above U+00FF, which no Amiga
  keymap can generate, are reported in `unmapped[]`. The 512 cap
  counts characters on both sides.
- **The input gate is visible in the kernel debug ring.** MCPd emits
  `[MCPd] input_gate state=... source=...` at startup and adds
  `input=on|off` to the `[MCPd] ready` beacon, both readable through
  `sys.debug_ring`. The startup banner alone goes to stdout, which is
  `NIL:` on the watchdog auto-start path -- invisible exactly where it
  matters most.


- **`wb.screenshot`** — capture an AmigaOS screen to a PNG on the
  target and bring it back to the host. The daemon grabs the chosen
  screen (frontmost, or by `screen_index`) with `graphics.library`
  ReadPixelArray and PNG-encodes it using the already-linked
  `z.library` (deflate + CRC32); AmigaOS 4.1 ships no PNG *writer*
  datatype, so the encoding is done directly rather than via
  `datatypes.library`. The host tool downloads the file (`fs.download`)
  and optionally inlines it as base64. Unlike `qemu.screenshot` (QMP
  `screendump`, QEMU-only), this works on real X5000 / A1222 hardware
  too, and fans out via `fleet.run_on_all`.

- **`sandbox.*` namespace** — driver- and program-iteration loop on
  top of [SandboxVM](https://github.com/derfsss/SandboxVM), an AOS4
  in-process sandbox host that survives guest crashes (DSI / ISI /
  alignment / privilege traps). Five tools plus one shared primitive:
  - `sandbox.probe` — path resolution + executability +
    Pegasos II refusal. Three typed error codes
    (`SANDBOXVM_MISSING` / `SANDBOXVM_BROKEN` /
    `SANDBOXVM_INCOMPATIBLE_TARGET`). Probe cache (60 s TTL) for
    chatty workflows; eager re-probe via the tool itself.
  - `sandbox.deploy` — wraps `fs.upload` with SHA-256 verify +
    cache invalidation + path-pinned post-deploy probe.
  - `sandbox.run_guest` — runs one guest ELF, slurps
    `T:sandboxvm-<name>.{out,err}` captures, decodes SandboxVM's
    exit-code convention into `trap_kind` (DSI / ISI / alignment /
    program / fp_unavailable) and the documented kmod-libcall
    NULL+4 fingerprint.
  - `sandbox.run_driver` — resident-driver mode (`-r`) with
    optional `-t` follow-on test guest. Loads the driver via
    `RTF_AUTOINIT` Resident + `CLT_InitFunc` so a test program in
    the same Guest can `OpenLibrary` the driver by name.
  - `sandbox.run_batch` — up to 16 guests sequentially in one
    sandboxvm invocation. Per-guest exit codes recovered from the
    kernel debug ring; aggregate exit follows SandboxVM's
    `last_nonzero` convention. Multi-target fan-out via
    `fleet.run_on_all`.
  - `sandbox.last_trap` — filter-on-top of `sys.debug_ring` for
    SandboxVM trap signatures. 3-attempt × 200 ms retry handles
    the kernel-ring write race after a crashed `run_guest`.
- **`sys.debug_ring`** — new primitive that reads the kernel debug
  ring via `c:DumpDebugBuffer`. Lifted out of `sandbox.last_trap`
  during the namespace-overlap review so the rest of the project
  (post-install forensics, hardware bring-up) can use it too.
- **`[paths] sandboxvm`** — host-side path to a built
  `bin/sandboxvm`, used by `sandbox.deploy` source resolution.
- **`[targets.<name>.sandbox]` block** — per-target SandboxVM
  overrides: AOS path, default `-m` extmem MB, default `-w`
  window MB, always-on `-x` deny-libs.
- **Bundled `AmiDock.amiga.com.xml`** as a package resource. The
  installer's `patch_amidock_prefs` step previously skipped
  silently when no XML was staged; it now uses the bundled prefs
  by default, so a fresh install ships with Filer / Ranger /
  IBrowse / DiskImageGUI in the dock subdrawers.
- **`installer.dismount_combi_device`** — new step at the end of
  the install pipeline. Ejects the install ISO from
  `diskimage.device` unit 50 (defensive, even though `unmount_iso`
  ran), deletes the installer-written `DEVS:DOSDrivers/COMBI`
  mountfile (only when it carries the installer's marker so a
  user-owned COMBI: is left alone), and issues
  `c:Dismount COMBI: FORCE` to drop the live DOS entry.

- **MCPd readiness beacon in the kernel debug ring.** MCPd now emits
  a single machine-parseable line via `IExec->DebugPrintF` once the
  listen socket is bound and accepting:

  ```
  [MCPd] ready name=MCPd version=1.3 build_date=02.08.2026 build_time=18:18:04 port=4322
  ```

  The pre-existing `Printf` banner goes to stdout, which is `NIL:` on
  the auto-start path (`S:Network-Startup` -> `Run >NIL: <NIL: Execute
  MCPd-Watchdog`), so it never landed anywhere observable. The debug
  ring survives regardless of how MCPd was launched, so anything
  reading `C:DumpDebugBuffer` (`sys.debug_ring`, a serial capture, a
  boot watcher) can detect "MCPd is up" without opening a socket.
  Because the line is emitted *after* bind+listen succeed, seeing it
  means the port is genuinely accepting rather than merely that the
  binary loaded. Two companion lines cover the other outcomes:
  `[MCPd] startup_failed version=... reason=bsdsocket|listen ...` and
  `[MCPd] shutdown version=...` (clean exit, so a reader can tell a
  clean stop from a crash). The `[MCPd] ` prefix and the `key=value`
  shape are a parsed interface — keep them stable.

  Note that `sys.debug_ring` reaches `C:DumpDebugBuffer` *through*
  MCPd, so it cannot detect a daemon that failed to start; for that,
  read a serial capture (`serial.*`, or QEMU's `-serial stdio` log
  with `debuglevel=1`), which does not depend on the daemon.

  Validated on QEMU AmigaOne across two cold boots: the beacon
  appears in both the serial log and `sys.debug_ring`, and the
  shutdown line is emitted on a clean `Break`.

- **Build time is now stamped into the binary.** New `MCPD_TIME`
  macro (`BUILD_TIME := $(shell date +%H:%M:%S)` in `mcpd/Makefile`),
  so two builds made on the same day are distinguishable. Surfaced in
  the readiness beacon, `MCPd --version`, `proto.version.build_time`
  and `proto.capabilities.build.time`. Deliberately *not* added to
  the `$VER` cookie: AmigaDOS `Version` parses `(DD.MM.YYYY)` and a
  time component would break it.

### Changed

- **MCPd's listener process now runs at priority 1** (was: whatever
  it inherited from the launching Shell, i.e. 0). The accept+spawn
  loop burns almost no CPU, so running it just above Workbench keeps
  the daemon responsive to new connections on a loaded machine. The
  per-connection worker processes are unchanged at -1, so the actual
  heavy RPC work (chunked uploads, recursive copies, `exec.cmd`
  subprocesses) still yields to the user. Confirmed on QEMU
  AmigaOne: `Status FULL` reports
  `priority 1 ... SYS:System/MCPd/MCPd` after a cold boot, with
  `exec.cmd` subprocesses still at -1.

- **MCPd version bumped to 1.3.** `mcpd/src/main.c` `MCPD_VERSION`,
  `mcpd/src/rpc.h` `MCPD_SERVER_VERSION` (`mcpd/1.3`), and
  `mcpd/Makefile` `VERSION` — the last of which had drifted and was
  still reading `1.1` while the shipped v1.2 binary reported `1.2`.

- **`installer.*` no longer requires a host-side
  `diskimage-bootstrap/` directory.** The AOS 4.1 diskimage tools
  (`MountDiskImage` / `diskimage.device` / `CDFileSystem`) ship
  with AOS 4.1 itself; `stage_diskimage_tools` is now a probe
  that fails loudly if the running system is missing any of them
  (which would mean the install host isn't a working AOS 4.1
  install). The dest drive picks up fresh copies via the normal
  `copy_base_os` pull from `<ISO>:System/`. Drops `bootstrap_dir`
  from `installer.preflight` / `installer.stage` /
  `[defaults] bootstrap_dir`; the `--init` wizard no longer
  prompts for it.
- **`sandbox.probe` doesn't gate on banner content.** Real-X5000
  testing showed sandboxvm's printf-based usage banner is
  occasionally captured as empty by MCPd's exec.cmd (clib4 /
  newlib stdio buffering not flushed before the process detaches
  from its inherited Output() handle). The probe now treats any
  structured `exec.cmd` exit as proof the binary started.

### Fixed

- **Dependency majors are now capped** (`mcp[cli]>=1.0.0,<2`,
  `pydantic>=2.0,<3`). `uv.lock` is not committed, so CI resolves
  dependencies fresh on every run; the unbounded `mcp` requirement
  picked up mcp 2.2.0, which removed the 1.x
  `from mcp.server.fastmcp import FastMCP` entry point this server is
  written against, and the type-check failed for reasons unrelated to
  anything in the repo. Lift the cap in a change that ports to the 2.x
  API and is tested against it.

### Known limitations (`input.*`)

- F11/F12 are deliberately absent from the rawkey table: their AOS4
  codes were never verified on hardware, and an unknown key name fails
  cleanly where a wrong code would silently press something else. The
  NewMouse wheel constants remain unverified (behind `#ifndef` guards
  so a missing header cannot break the build).
- `input.mouse_move` defaults to a read-position-then-relative-delta
  strategy and reports the position actually achieved, but does not
  retry to converge. `input.click`'s pre-move and `input.drag`'s
  move-to-start do not read back at all, so pointer acceleration on
  real hardware could land them off-target; only QEMU pegasos2 has
  been measured. `absolute_mode="raw"` remains available to A/B the
  true-absolute event form.
- `input.click` does not report the achieved pointer position the way
  `input.mouse_move` and `input.drag` do.

### Tool count

- 121 → 137 tools (+6 `sandbox.*`, +`sys.debug_ring`,
  +`wb.screenshot`, +8 `input.*` including its dispatcher).
  13 → 14 namespaces.

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
