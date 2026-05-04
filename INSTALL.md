# Installation

This document covers installing MCP-AmigaOS4 from pre-built artefacts.
For build instructions see [BUILD.md](BUILD.md). For day-to-day usage
see [USAGE.md](USAGE.md).

The project consists of two artefacts that are installed in two
different places:

1. The host MCP server (**`amiga-fleet-mcp`**) — a Python package that
   runs on the workstation alongside the MCP client.
2. The Amiga-side daemon (**`MCPd`**) — a PowerPC AmigaOS 4 ELF binary
   that runs on each target.

## Runtime requirements

### Host workstation

| Requirement | Version | Purpose |
|---|---|---|
| Operating system | Linux, macOS, or Windows 10+ | Any platform that runs Python and (optionally) Docker. |
| Python | 3.11 or later | Host server runtime. The server uses the standard library `tomllib`, which is 3.11+. |
| `uv` *or* `pip` | current | Python dependency manager. `uv` is recommended for reproducibility; plain `pip` works. |
| Docker Desktop | current | **Optional.** Required only if you intend to cross-compile MCPd from source rather than use a pre-built binary. |
| QEMU PPC | 8.0 or later | **Optional.** Required only for QEMU targets. The binary `qemu-system-ppc` (Linux/macOS) or `qemu-system-ppc.exe` (Windows). |
| MCP client | per client | Claude Code, Claude Desktop, or any other MCP-aware client capable of launching a stdio server. |
| TCP/UDP outbound | ports 4322 (TCP), 4323 (UDP) | Reach to MCPd and discovery on each target. |

### AmigaOS 4 target

| Requirement | Notes |
|---|---|
| AmigaOS 4.1 Final Edition (Update 1 or later) | Ships Kickstart 54.x and Workbench 53.x. Update 3 is the canonical test target. |
| `bsdsocket.library` (Roadshow) | TCP/UDP stack. Bundled with AmigaOS 4.1 FE. |
| `application.library` v53.11 or later | Used for `sys.applications`, `app.notify`, and AmiDock registration. Bundled with AmigaOS 4.1 FE. |
| `dos.library` and `intuition.library` | Standard system libraries. |
| `i2c.resource`, `performancemonitor.resource`, `xena.resource`, `acpi.resource`, `fsldma.resource` | **Optional.** Only required for the corresponding `sys.hardware.*` methods. Not all targets expose all resources. |
| Free disk space | Approximately 200 KiB on the boot volume for `SYS:System/MCPd/`. |
| Free memory | Approximately 1 MiB working set for the daemon plus its peer tasks. |
| Network connectivity | Wired Ethernet or working WiFi via Roadshow. |

The daemon does not require any specific board model; CPU detection is
fully automatic. Boards currently recognised: X5000 (P5020), X1000
(PA6T-1682M), A1222 (T1042), Pegasos2, SAM440/460, AmigaOne XE/Micro.

## Per-feature prerequisites

The core surface (`fs.*`, `exec.cmd`, `sys.version` / `tasks` /
`libraries` / `devices` / `ports` / `lastalert` / `uptime` / `memory`
/ `volumes` / `assigns` / `hardware` / `applications` /
`alert_decode`, `wb.*`, `events.*`, `proto.*`) works on every
supported target out of the box. The features below have additional
requirements.

### `sys.hardware.i2c`

Requires `i2c.resource` to be available on the target. Present on
A-EON-built AmigaOne hardware; absent on QEMU and on most older
boards. Returns `available=false` cleanly when missing.

### `sys.hardware.perfcounters`

Requires `performancemonitor.resource`. Same availability profile as
above.

### `sys.executable.symbols`

Requires `elf.library` v52 or later. Bundled with AmigaOS 4.1 FE.

### `sys.mcu_cmd` (X5000 only)

Talks the Cyrus board's MCU supervisor protocol (TRM section 8.1)
over `serial.device` unit 1 at 38400 8N1, and is the canonical
sensor path on X5000 (board / CPU temperatures, fan PWM/RPM, voltage
rails, soft power-off). Requirements:

- AmigaOne X5000 / Cyrus motherboard with internal MCU (no other
  board exposes this protocol).
- The internal P18 / P15 MCU debug header is **not** required for
  this method — the MCU's runtime UART1 is wired through the
  on-board `serial.device` unit 1, so no external cable is needed.
- `cmd="s"` (soft power-off) is gated by `confirm: true`.

Talks the documented MCU protocol directly via AOS's `serial.device`
— no closed-source dependency.

### `sys.read_ccsr`, `sys.read_pa`, `sys.tlb_dump` (Freescale QorIQ only)

These methods read SoC-internal state through TLB1-mapped supervisor
addresses. They require:

- A Freescale QorIQ CPU: P5020 (X5000) or P1022 / T1042 (A1222).
  Will return errors on any other PowerPC variant.
- Real hardware. QEMU's e500 model does not implement the full CCSR
  aperture, the MCU UART, or some of the perfmon counters.

`sys.read_pa` is **dangerous**: it performs an unchecked
supervisor-mode load at any 36-bit physical address. A wrong PA
takes a DSI exception that crashes the connection-handling task
(the daemon's main listener stays up by design — see the
spawn-per-connection isolation note in `mcpd/src/main.c`). Use the
TLB1 dump first to identify mapped regions; do not call from
production scripts.

### `sys.cold_reboot`

Software cold reboot via `IExec->ColdReboot`. Requires `confirm:
true`. The response flushes before the reboot fires; clients should
expect the connection to drop immediately afterwards. Reliable on
real hardware. **Not** reliable on QEMU AmigaOS 4 guests — the
guest's reboot path leaves QEMU in an unrecoverable state; kill the
QEMU process and start a fresh one instead.

### `wb.*` and `app.notify`

`wb.*` requires `intuition.library` (always present on AmigaOS 4).
`app.notify` requires `application.library` v53.11 or later (bundled
with AmigaOS 4.1 FE) and a running Ringhio daemon (also bundled).

### `debug.*` (per-task IDebug methods)

`debug.task_snapshot`, `debug.symbol`, `debug.stacktrace`,
`debug.write_memory`, and `debug.write_register` need `IDebug` on
the target. Bundled with the AmigaOS 4 SDK and present on all
AmigaOS 4.1 FE installs.

### `debug.*` (whole-system GDB-stub methods, QEMU only)

`debug.read_registers`, `debug.read_memory`, `debug.set_breakpoint`,
`debug.clear_breakpoint`, `debug.step`, `debug.continue`,
`debug.backtrace`, `debug.stop_reason`, and `debug.detach` drive
QEMU's GDB Remote Serial Protocol stub.

- Requires a `[targets.<name>.channels.gdb]` block in the config
  with `enabled = true` and a `port` (typically `tcp::1234`).
- The host server auto-injects `-gdb tcp::PORT` into the QEMU
  command line when a `gdb` channel is present.
- Real hardware has no GDB stub; these tools are QEMU-only.

### `qemu.*`

Requires:

- A `[targets.<name>.channels.qmp]` block with the QMP socket
  endpoint in the config.
- `qemu-system-ppc` 8.0 or later on the host.
- A working machine config (`qemu_config = "..."`) pointing at the
  per-machine kyvos JSON.

`qemu.savevm` / `loadvm` / `list_snapshots` / `delete_snapshot`
additionally require a qcow2 backing image with snapshot support.

### `installer.*`

Drives an end-to-end AmigaOS 4.1 FE install. Requirements:

- A host-side `sources_dir` containing the official Hyperion ISO
  for the target machine (`AmigaOneX5000InstallCD-<rev>.iso`,
  `AmigaOneA1222InstallCD-<rev>.iso`, etc.) plus the relevant
  Update LHAs (`AmigaOS4.1FinalEditionUpdateN-NN.NN.lha`), Enhancer
  Software LHA (`enhancer_software_2.2.lha`), and per-machine
  extras (X5000 needs `NGFCheck.lha` + `NGFS.lha`). Run
  `installer.required_files` for the machine to see the full list.
- A built `MCPd` binary, either in `sources_dir` or in the
  install-support tree (set `$AMIGA_INSTALL_SUPPORT_DIR` or pass
  `bootstrap_dir=`).
- A `diskimage-bootstrap/` directory holding `MountDiskImage`,
  `diskimage.device`, and `CDFileSystem`. The installer falls back
  to `<sources_dir>/diskimage-bootstrap/` if `bootstrap_dir` is not
  given.
- A formatted destination volume on the target that is **not** the
  current system volume. `installer.preflight` checks this and
  refuses to proceed against a system volume.
- The A1222 install path additionally requires a mounted RESCUEV2:
  or AAATECH0: USB volume on the target with the bootloader files;
  preflight emits a warning when targeting A1222.

### `serial.*`

Captures bytes from a host-side serial port into a log file.
Requirements:

- `pyserial` on the host (declared as a dependency of the host
  package; installed automatically by `uv sync` / `pip install`).
- A serial port wired to the target's debug UART. For X5000, that
  is the rear-panel DB9 (visible to the host as `/dev/ttyUSBn` or
  `COMn`); for A1222 it is the equivalent header. The port name is
  configured per target under `[targets.<name>.channels.serial]`.
- Operating-system permission to open the port. On Linux that
  usually means membership of the `dialout` (Debian/Ubuntu) or
  `uucp` (Arch) group.

`serial.*` does not depend on MCPd; the capture runs purely on the
host.

## Installing the host server

### Using `uv` (recommended)

```sh
cd host
uv sync
uv run amiga-fleet-mcp --version
```

`uv sync` resolves and installs the host package and its dev
dependencies into a local `.venv/`.

### Using `pip`

```sh
cd host
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .
amiga-fleet-mcp --version
```

### Wiring the server into an MCP client

For Claude Code:

```sh
claude mcp add amiga-fleet -- amiga-fleet-mcp --config /path/to/config.toml
```

A starter configuration file lives at `host/config.example.toml`; copy
it to a permanent location (for example
`~/.config/amiga-fleet-mcp/config.toml`) and edit the `[targets.*]`
blocks. See [USAGE.md](USAGE.md) for the configuration schema.

The starter config also ships with a commented `[defaults]` block —
uncomment and fill it in to give frequently-repeated parameters
(`dest_volume`, `sources_dir`, `bootstrap_dir`, `machine`, ...) a
fleet-wide default. See [USAGE.md § Per-tool
defaults](USAGE.md#per-tool-defaults) for the full list of keys.

## Installing MCPd on a target

The auto-start installer needs MCPd to be running on the target
*once* so that the daemon's own filesystem methods can deploy it
persistently. Pick whichever first-time transfer method matches
your environment.

> **Before you bind port 4322**: MCPd has no authentication on
> its TCP listener and a complete connection has full filesystem
> + AmigaDOS shell access on the target. Run targets on a
> private LAN, behind a host-only QEMU NAT, or over a
> point-to-point link. Don't expose 4322 / 4323 to an untrusted
> network. See [SECURITY.md](SECURITY.md) for the full operator-
> responsibility model.

### Step 1: One-time bootstrap

#### Real hardware (X5000 / A1222 / X1000 / SAM460 / classic AmigaOne)

- **Web browser on the target.** If you have a working browser
  on the Amiga (IBrowse, OWB, NetSurf, AWeb-AP1, ...) and
  Roadshow is up, just download the binary directly from the
  GitHub release:

  ```
  https://github.com/derfsss/MCP-AmigaOS4/releases/latest/download/MCPd
  ```

  Save it somewhere writable, then proceed to the launch step
  below. Easiest option for users who already have an Amiga on
  the LAN.
- **USB stick.** Copy `mcpd/MCPd` onto a FAT-formatted USB stick
  on the host workstation, plug it into the target, then copy
  from `USB0:` (or wherever it mounts) to a writable AmigaDOS
  volume — typically `RAM:` for the one-time launch.
- **SMB networking.** Share the host directory containing
  `mcpd/MCPd` over SMB, then mount that share on the target via
  Workbench → `SYS:Tools/smbfs`. Roadshow + smbfs ship with
  AmigaOS 4.1 FE.
- **SerialShell.** Install
  [SerialShell](https://os4depot.net/) (search OS4Depot for
  *serialshell*) on the target, then drive the upload + launch
  from the host:

  ```sh
  python scripts/x5000_bootstrap_mcpd.py --host <target-ip>
  ```

  This is the most automated option — the script uploads the
  binary to `RAM:MCPd` and launches it in a single command.
  SerialShell is a third-party tool; MCPd doesn't depend on
  it at run time.

#### QEMU guests (pegasos2 / amigaone / sam460ex)

- **Web browser on the guest.** Same idea as real hardware —
  if the guest image includes a browser (IBrowse, OWB,
  NetSurf, ...) and the SLIRP NAT lets it reach
  `github.com`, fetch
  `https://github.com/derfsss/MCP-AmigaOS4/releases/latest/download/MCPd`
  from inside the guest.
- **USB share.** Hand the binary to the guest by attaching a
  host file as a USB-storage device on the QEMU command line,
  then mount it inside the guest.
- **9P share.** Mount a host directory directly inside the guest
  using the
  [virtio9p driver](https://os4depot.net/) (search OS4Depot for
  *virtio9p*). No copy step needed — the file appears live on
  the AmigaDOS side as soon as it's written on the host.
  Recommended when iterating quickly during development.
- **SerialShell.** Same procedure as real hardware. The QEMU
  guest needs SerialShell pre-installed and the host reaches it
  via the guest's TCP-forward port (typically 4321 → hostfwd
  4421 in qemu-runner-shaped configs).

#### Once MCPd is on a writable volume

Run it from a Shell on the target:

```
Protect MCPd +rwed
Run >NIL: <NIL: MCPd
```

TCP port 4322 should become reachable from the host within a few
seconds.

### Step 2: Persistent auto-start

Once MCPd is reachable, install it into a persistent location and wire
it into the AmigaOS startup sequence:

```sh
python scripts/install_mcpd_autostart.py <target-ip>:4322
```

The installer performs five steps:

1. Creates the `SYS:System/MCPd/` drawer.
2. Copies the binary to `SYS:System/MCPd/MCPd` and clears the
   `e`-protected bit (AmigaOS new-file default).
3. Uploads the watchdog script `SYS:System/MCPd/MCPd-Watchdog`, which
   relaunches MCPd if it ever exits, with a 5 s back-off.
4. Creates a one-time backup of `S:Network-Startup` at
   `S:Network-Startup.before-mcpd`.
5. Idempotently appends a launch line that runs the watchdog from the
   `Network-Startup` script.

After the next reboot, MCPd binds on `:4322` within approximately
eleven seconds of cold boot.

### Manual install (no host helper)

Copy `mcpd/MCPd` and `mcpd/install/MCPd-Install` onto the target, then
from a Shell on the target:

```
CD <directory containing the files>
Execute MCPd-Install
```

The script performs the same Network-Startup patch as the host helper.

### Uninstalling

From a Shell on the target:

```
Execute SYS:System/MCPd/MCPd-Uninstall
```

This restores `S:Network-Startup` from the `.before-mcpd` backup and
removes the `SYS:System/MCPd/` drawer. Reboot to take effect.

## Verifying the installation

Run the validation suite against the freshly-installed daemon:

```sh
python scripts/validate.py --endpoint <target-ip>:4322 --rounds 1,2,3,4,5
```

A successful install reports:

```
[summary] endpoint=<target-ip>:4322  rounds=[1, 2, 3, 4, 5]  failures=0/5
```

## Network port reference

| Port | Protocol | Direction | Use |
|---|---|---|---|
| 4322 | TCP | host → target | MCPd JSON-RPC (request / response and server-push notifications). |
| 4323 | UDP | host → target (broadcast) | `fleet.discover` probe. |
| 4323 | UDP | target → host (unicast) | Discovery response. |

For QEMU targets these ports are forwarded via `slirp` `hostfwd`
clauses. The host-side ports are configurable per target (the
convention in this project is 4421 / 4422 / 4423 mapping to the
in-guest 4321 / 4322 / 4323).

## Troubleshooting

- **`bind(:4322) failed`** — usually a stale connection in TIME_WAIT.
  Recent MCPd builds set `SO_LINGER linger=0` to mitigate. If a fresh
  bind still fails, reboot the target or temporarily run on an
  alternative port via `MCPd --port 4422`.
- **`Method not found`** — the deployed binary is older than the
  client's wire schema. Rebuild and redeploy with
  `scripts/install_mcpd_autostart.py`.
- **Watchdog log empty** — the daemon's stdout is redirected to NIL
  by the watchdog launch line; structured events go through MCP.
  Inspect `T:MCPd-Watchdog.log` on the target for restart history.
