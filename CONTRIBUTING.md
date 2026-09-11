# Contributing to MCP-AmigaOS4

Contributions are welcome.

## Reporting issues

File issues on the [GitHub issue
tracker](https://github.com/derfsss/MCP-AmigaOS4/issues). For bug
reports please include:

- Target type (QEMU machine model + image, or real-hardware board).
- AmigaOS version on the target (`MCPd --version` output, or
  `Version` from a Shell on the target).
- Host OS + Python version + `amiga-fleet-mcp --version`.
- Concise reproduction steps and the actual vs expected output.

There is no separate channel for "security" reports. See
[SECURITY.md](SECURITY.md) — the daemon ships without
authentication on its TCP listener and is designed for
trusted, network-isolated deployments. If you've found a
behaviour that materially diverges from the documented design,
file it as a regular issue.

## Development setup

```sh
# Host (Python) side
cd host
uv sync                   # or: pip install -e .[dev]
uv run pytest -q
uv run ruff check src tests
uv run mypy src

# Daemon (C) side - cross-compile via Docker
cd ../mcpd
make docker-build
```

The full build / install procedure for both pieces lives in
[BUILD.md](BUILD.md) and [INSTALL.md](INSTALL.md).

## Pull requests

- **Branch from `develop` and target `develop`.** That is the active
  development branch; `main` holds releases and is protected. A PR
  aimed at `main` has to be rebased before it can land.
- Keep the diff focused: one logical change per PR.
- CI must pass on every PR. The CI workflow runs ruff, mypy, pytest
  on Python 3.11 / 3.12 / 3.13, and cross-compiles MCPd in the
  `walkero/amigagccondocker:os4-gcc11` image.
- Match the surrounding code style (ruff covers most of it). New
  Python code should round-trip through `ruff check` and `mypy`
  cleanly.
- Update the relevant doc (USAGE.md / COMMANDS.md / INSTALL.md)
  when changing the user-visible surface, plus a `CHANGELOG.md`
  entry under `## Unreleased`. If a new tool needs something the
  target doesn't have by default, add it to [INSTALL.md § Per-feature
  prerequisites](INSTALL.md#per-feature-prerequisites) — that table is
  how users find out why a call returns `NotCapable`.
- Sign your commits if you can (`git commit -S`); not mandatory.

## Adding a new MCP tool

1. Implement the host-side wrapper under
   `host/src/amiga_fleet_mcp/tools/<namespace>.py`.
2. Register it in `host/src/amiga_fleet_mcp/server.py` with a
   `@mcp.tool` decorator and add it to the matching namespace
   dispatcher.
3. If it talks to the daemon, also register the JSON-RPC method in
   `mcpd/src/rpc.c`, add the handler prototype to
   `mcpd/src/methods/methods.h`, and implement it under
   `mcpd/src/methods/`. **A new `.c` file must also be added to
   `SRCS` in `mcpd/Makefile`** — forgetting that is a link error, not
   a compile error, and the symptom looks nothing like the cause.
4. If the tool has a daemon-side capability gate, report its state in
   `proto.capabilities` (`mcpd/src/methods/proto.c`) too — and keep
   the method advertised when the gate is closed, so clients can tell
   "switched off" apart from "old daemon".
5. Add unit tests under `host/tests/unit/` using a fake transport. For
   a gated or `confirm`-guarded tool, assert both that the error is
   raised *and* that nothing reached the wire (`fake.calls == []`).
6. Document it in [COMMANDS.md](COMMANDS.md) and (if it's a major
   addition) [USAGE.md](USAGE.md). Anything that widens the remote
   attack surface also needs a [SECURITY.md](SECURITY.md) note.

## Adding a new target machine

See `host/src/amiga_fleet_mcp/installer/machines.py` and the
per-machine sequence files under
`host/src/amiga_fleet_mcp/installer/sequences/`. The pattern is one
small `<machine>.py` that imports a `MachineConfig` and shared
build steps from `_steps.py`.

## Licence

By contributing you agree that your contributions are licensed
under the [BSD 3-Clause License](LICENSE).
