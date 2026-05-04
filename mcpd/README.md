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

The resulting `MCPd` ELF (~190 KiB PowerPC) lands beside the source
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
    fs.c               fs.* filesystem operations (11 methods)
    exec.c             exec.cmd
    wb.c               wb.* Workbench / Intuition queries
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
```

## Direct command-line invocation

```
MCPd                  ; default :4322
MCPd --port 4421      ; override port
MCPd --version
```

In production deployments the daemon is launched by the watchdog
script (`MCPd-Watchdog`) from `S:Network-Startup`. See
[INSTALL.md](../INSTALL.md) for the auto-start install procedure.
