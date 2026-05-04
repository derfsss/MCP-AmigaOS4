# Building from source

This document covers compiling the project from source. For installing
pre-built artefacts see [INSTALL.md](INSTALL.md). For day-to-day usage
see [USAGE.md](USAGE.md).

The build produces two artefacts:

1. **`mcpd/MCPd`** — a PowerPC AmigaOS 4 ELF binary, cross-compiled
   inside a Docker container.
2. **`amiga-fleet-mcp`** — a Python distribution (wheel + sdist) for
   the host server.

The two builds are independent. If a pre-built MCPd binary is already
available, the host-side build alone is sufficient.

## Build requirements

### Common

| Requirement | Version | Purpose |
|---|---|---|
| `git` | 2.20 or later | Submodule fetch (`third-party/cjson`). |
| Disk space | 1 GiB free | Build artefacts, Docker layers, virtualenvs. |

### MCPd (Amiga-side daemon)

| Requirement | Version | Purpose |
|---|---|---|
| Docker Desktop *or* Docker Engine | 20.10 or later | Hosts the cross-compile container. The Makefile pulls `walkero/amigagccondocker:os4-gcc11` (≈1.5 GiB image). |
| `make` | GNU Make 4.x | Drives the cross-compile via the bundled `Makefile`. On Windows, install via MSYS2 (`pacman -S make`) or use the Linux subsystem. |
| Network access to Docker Hub | one-time, ≈1.5 GiB pull | Required to fetch the cross-compile image on first build. The image is cached locally afterwards. |
| Toolchain (alternative to Docker) | `ppc-amigaos-gcc 11.5.0` from [`adtools`](https://github.com/sba1/adtools) | Native cross-compile without Docker. Only required if you prefer to avoid containers. |

### Host server (Python package)

| Requirement | Version | Purpose |
|---|---|---|
| Python | 3.11, 3.12, or 3.13 | Host server runtime. |
| `uv` *or* `pip` + `venv` | current | Dependency installer. `uv` is the recommended development environment. |
| `hatchling` | 1.18 or later | Build backend. Installed automatically by `uv build` or `pip wheel`. |

### Runtime test infrastructure (optional)

These are not strictly required to compile, but the included
end-to-end validation scripts depend on them:

| Requirement | Version | Purpose |
|---|---|---|
| `qemu-system-ppc` | 8.0 or later | QEMU validation scripts. |
| AmigaOS 4 QEMU image | — | A bootable AmigaOS 4.1 FE image. The image path is referenced from the per-target `qemu_config` field in the host configuration. |
| AmigaOne X5000 hardware | optional | Real-hardware validation; not required to build. |

## Building MCPd

### Cross-compile via Docker (recommended)

```sh
cd mcpd
make docker-build
```

The Makefile mounts the project root into the container at `/src` and
runs `make all` inside. Output: `mcpd/MCPd` (approximately 220 KiB
PowerPC ELF, including the cJSON parser and the full RPC method
registry).

On Windows under MSYS2, the Makefile auto-detects the host and
converts paths via `pwd -W`. If a Docker error reports
`Access is denied` on a Windows path, the shell is mangling the mount
spec; invoke Docker directly:

```sh
MSYS_NO_PATHCONV=1 docker run --rm \
  -v "$(cygpath -w "$(pwd)/.."):/src" \
  -w /src/mcpd \
  walkero/amigagccondocker:os4-gcc11 make all
```

### Cross-compile natively

If `ppc-amigaos-gcc` (from the
[`adtools`](https://github.com/sba1/adtools) toolchain) is on `PATH`:

```sh
cd mcpd
make all
```

The Makefile auto-detects the toolchain via `CC ?= ppc-amigaos-gcc`.

### Source layout

```
mcpd/
  Makefile
  src/
    main.c              listener loop, accept + spawn-per-connection
    conn_ctx.h          per-task connection context (bsdsocket
                        interface, fd) addressed via tc_UserData
    frame.c, frame.h    4-byte BE length-prefix framing
    rpc.c, rpc.h        JSON-RPC dispatch + method registry
    b64.c, b64.h        base64 codec
    sha256.c, sha256.h  public-domain SHA-256 reference impl
    discovery.c/h       UDP discovery responder peer task
    crashhook.c         IDebug crash hook (captures DAR + register
                        state on user-task DSI / ISI faults)
    methods/
      proto.c           proto.capabilities, proto.version
      sys.c             sys.* introspection (23 methods)
      fs.c              fs.* (11 methods)
      exec.c            exec.cmd
      wb.c              wb.* (4 methods)
      debug.c           debug.* (5 methods, IDebug-driven)
      events.c          events.{wait,subscribe,unsubscribe,test_emit}
      hwres.c           sys.hardware.{i2c,perfcounters}
      elfm.c            sys.executable.symbols (ELF reader)
      applib.c          sys.applications, app.notify, app-register glue
      mcu.c             sys.mcu_cmd (X5000 Cyrus MCU UART supervisor)
      amigautil.c       SystemTagList wrapper
      helpers.c         param parsing helpers
  install/
    MCPd-Install        AmigaDOS install script
    MCPd-Uninstall      AmigaDOS uninstall script
    MCPd-Watchdog       relaunch wrapper
  third-party/cjson     cJSON 1.7.18 (submodule)
```

### Adding a new RPC method

1. Implement the handler in an existing or new
   `mcpd/src/methods/*.c` file. The signature is
   `int my_method(cJSON *params, cJSON **out_result, cJSON **out_err)`.
2. Add an `extern` declaration plus a registry entry to
   `mcpd/src/rpc.c`.
3. If the file is new, list it in `mcpd/Makefile` under `SRCS`.
4. Rebuild and redeploy via `scripts/install_mcpd_autostart.py`.
5. Add a host-side wrapper in `host/src/amiga_fleet_mcp/tools/` and
   register it as an MCP tool in `host/src/amiga_fleet_mcp/server.py`.
6. Add coverage to one of the round validation scripts under
   `scripts/`.

## Building the host server

### Using `uv`

```sh
cd host
uv sync
uv run amiga-fleet-mcp --version
```

`uv sync` resolves `pyproject.toml`, installs the project plus dev
dependencies into `.venv/`, and is fully reproducible via
`uv.lock`.

### Using `pip`

```sh
cd host
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .[dev]
amiga-fleet-mcp --version
```

### Building distributables

```sh
cd host
uv build
```

Output: `host/dist/amiga_fleet_mcp-<version>-py3-none-any.whl` and the
matching sdist.

### Tests

```sh
cd host
uv run pytest tests/unit -q                  # unit tests (≈170)
uv run mypy src                              # type-check
uv run ruff check src tests                  # lint
```

End-to-end validation (requires running daemon and/or QEMU) is driven
by the scripts described in [USAGE.md](USAGE.md).

## Continuous integration

`.github/workflows/ci.yml` exercises:

- **Host job**: `uv sync` against Python 3.11 / 3.12 / 3.13, then
  `ruff check src tests`, `mypy src`, and `pytest -q`.
- **MCPd job**: pulls `walkero/amigagccondocker:os4-gcc11` and runs
  `make all` from the project root mounted at `/src`. The
  cross-compile must succeed cleanly (zero warnings beyond known
  cJSON `override-init` notes).

Per-PR runs cover both jobs. End-to-end QEMU validation runs locally
on demand and is not part of CI.

## Third-party dependencies

The full per-dependency licence index is in
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md). The project's
internal policy, in brief:

- Submodules only — no vendored copies.
- Permissive licences only (MIT, BSD-2/3, Apache-2.0, ISC, Zlib,
  MPL-2.0). GPL excluded for static linking. LGPL acceptable for
  AmigaOS shared libraries that are not bundled (for example,
  `amissl.library` for the planned TLS option).
- Per-dependency patching strategy is one of: (a) fork on
  `derfsss/<name>`, (b) submodule pinned at upstream commit plus
  `third-party/<name>-patches/*.patch` applied at build time, or
  (c) clean upstream commit with no patches.

Currently the only third-party dependency in MCPd is **cJSON 1.7.18**,
included as a clean upstream submodule (strategy (c)).

## Source-of-truth statement

The `mcpd/` and `host/` source trees are the authoritative description
of behaviour. The Markdown documents in this repository capture
intent, history, and architectural decisions — they are not
authoritative for the current API surface. Inspect the source code,
or query the running daemon via `proto.capabilities`, when in doubt.
