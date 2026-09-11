# MCPd

The Amiga-side daemon for the MCP-AmigaOS4 project. A small C program
that runs on AmigaOS 4 and exposes filesystem, process, system,
debug, and Workbench operations as JSON-RPC 2.0 methods over a 4-byte
length-prefix framed TCP socket on port 4322 (with a UDP discovery
responder on port 4323).

See the top-level documentation set:

- [USAGE.md](../USAGE.md) — overview.
- [INSTALL.md](../INSTALL.md) — runtime requirements and install
  procedure (including the auto-start integration that deploys MCPd
  to `SYS:System/MCPd/`).
- [BUILD.md](../BUILD.md) — build requirements and cross-compile
  instructions.
- [COMMANDS.md](../COMMANDS.md) — full RPC method reference.

## Quick build

```sh
make docker-build         # cross-compile via Docker Desktop
```

or, with a local `ppc-amigaos-gcc` toolchain:

```sh
make all
```

The resulting `MCPd` ELF (~300 KiB PowerPC) lands beside the source
files. See [BUILD.md](../BUILD.md) for prerequisites and details.

## Source layout

```
src/
  main.c               listener loop + spawn-per-connection accept
  conn_ctx.h           per-task connection context (per-task
                       bsdsocket interface + fd, addressed via
                       tc_UserData)
  frame.c, frame.h     4-byte BE length-prefix framing
  rpc.c, rpc.h         JSON-RPC dispatch and method registry
  b64.c, b64.h         base64 codec
  sha256.c, sha256.h   SHA-256 reference implementation
  discovery.c/h        UDP discovery responder peer task
  crashhook.c          IDebug crash hook
  methods/
    proto.c            proto.* (capability advertisement, version)
    sys.c              sys.* introspection (23 methods)
    fs.c               fs.* filesystem operations (12 methods,
                       including fs.sync -- ACTION_FLUSH)
    exec.c             exec.cmd
    wb.c               wb.* Workbench / Intuition queries
    screen.c           wb.screenshot (ReadPixelArray + PNG encode
                       via z.library)
    input.c            input.* keyboard / mouse injection --
                       DISABLED unless the operator opens the gate
    debug.c            debug.* per-task IDebug-driven helpers
    events.c           events.* long-poll and server-push
    hwres.c            sys.hardware.{i2c,perfcounters}
    elfm.c             sys.executable.symbols
    applib.c           application.library integration
    mcu.c              sys.mcu_cmd (X5000 Cyrus MCU UART supervisor)
    amigautil.c        AmigaDOS shell-out helper
    helpers.c          shared parameter-parsing helpers
install/
  MCPd-Install         AmigaDOS install script
  MCPd-Uninstall       AmigaDOS uninstall script
  MCPd-Watchdog        relaunch-on-exit wrapper
  MCPd-Enable-Input    open the input.* gate (operator action)
  MCPd-Disable-Input   close it again
```

60 JSON-RPC methods in total. `COMMANDS.md` documents every one.

## Direct command-line invocation

```
MCPd                  ; default :4322
MCPd --port 4421      ; override port
MCPd --enable-input   ; allow input.* -- off by default, and NOT
                      ; persistent; see SECURITY.md
MCPd --version
```

In production deployments the daemon is launched by the watchdog
script (`MCPd-Watchdog`) from `S:Network-Startup`. See
[INSTALL.md](../INSTALL.md) for the auto-start install procedure.

## Startup beacon

On a successful start MCPd writes one line to the kernel debug ring
via `IExec->DebugPrintF`:

```
[MCPd] ready name=MCPd version=1.3 build_date=02.08.2026 build_time=18:18:04 port=4322 input=off
```

`input=on|off` reports the keyboard / mouse injection gate, which is
resolved once at startup; a companion `[MCPd] input_gate state=...
source=...` line says where the setting came from.

The line is emitted **after** `bind` + `listen` succeed, so its
presence means the port is accepting connections — not merely that
the binary loaded. Two companion lines cover the other outcomes:

```
[MCPd] startup_failed version=1.3 reason=bsdsocket
[MCPd] startup_failed version=1.3 reason=listen port=4322
[MCPd] shutdown version=1.3
```

`shutdown` marks a clean exit, so a reader can distinguish a normal
stop from a crash (which produces no line at all).

The `[MCPd] ` prefix and the `key=value` shape are a parsed
interface — keep them stable.

The daemon also prints a banner to stdout, but on the auto-start
path stdout is `NIL:` (`Run >NIL: <NIL: Execute MCPd-Watchdog`), so
the debug ring is the only place a boot-time start is observable.

Read the beacon with any of:

- `C:DumpDebugBuffer` on the target (or the `sys.debug_ring` MCP
  tool, which wraps it)
- a serial capture of the debug UART — `serial.*` on real hardware,
  or QEMU's `-serial stdio` log with `debuglevel=1`

Two limits are worth knowing:

- `sys.debug_ring` reaches `DumpDebugBuffer` *through* MCPd, so it
  cannot detect a daemon that failed to start. Use the serial
  capture, which does not depend on the daemon.
- **The AmigaOS debug buffer does not wrap.** Once full it stops
  accepting entries, so on a machine whose buffer is exhausted before
  MCPd starts — a verbose graphics driver can fill it during boot —
  no beacon appears there at all. Compare `raw_size` across two
  `sys.debug_ring` calls: no growth on an active machine means the
  buffer is full, not that the system is quiet. Serial capture is
  unaffected.

Build date comes from the `$VER` cookie; build time is a separate
`MCPD_TIME` macro. Time is deliberately excluded from `$VER` because
AmigaDOS `Version` parses the `(DD.MM.YYYY)` form. Both are also
available from `MCPd --version`, `proto.version`, and
`proto.capabilities`.

## Process priorities

| Process | Priority |
|---|---|
| Listener (`main`) | **1** — above Workbench |
| `MCPd Client` (one per connection) | **-1** |
| `MCPd Discovery` | 0 |

The accept + spawn loop costs almost nothing, so running it above
Workbench keeps the daemon responsive to new connections on a loaded
machine. The per-connection workers sit below Workbench so the actual
RPC work — chunked uploads, recursive copies, `exec.cmd` subprocesses
— yields to the user.

The listener sets its own priority at startup, so it does not matter
what priority the launching Shell had.
