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

- Branch from `main` and keep the diff focused: one logical change
  per PR.
- CI must pass on every PR. The CI workflow runs ruff, mypy, pytest
  on Python 3.11 / 3.12 / 3.13, and cross-compiles MCPd in the
  `walkero/amigagccondocker:os4-gcc11` image.
- Match the surrounding code style (ruff covers most of it). New
  Python code should round-trip through `ruff check` and `mypy`
  cleanly.
- Update the relevant doc (USAGE.md / COMMANDS.md / INSTALL.md)
  when changing the user-visible surface, plus a `CHANGELOG.md`
  entry under `## Unreleased`.
- Sign your commits if you can (`git commit -S`); not mandatory.

## Adding a new MCP tool

1. Implement the host-side wrapper under
   `host/src/amiga_fleet_mcp/tools/<namespace>.py`.
2. Register it in `host/src/amiga_fleet_mcp/server.py` with a
   `@mcp.tool` decorator and add it to the matching namespace
   dispatcher.
3. If it talks to the daemon, also register the JSON-RPC method in
   `mcpd/src/rpc.c` and implement the handler under
   `mcpd/src/methods/`.
4. Add unit tests under `host/tests/unit/` using a fake transport.
5. Document it in [COMMANDS.md](COMMANDS.md) and (if it's a major
   addition) [USAGE.md](USAGE.md).

## Adding a new target machine

See `host/src/amiga_fleet_mcp/installer/machines.py` and the
per-machine sequence files under
`host/src/amiga_fleet_mcp/installer/sequences/`. The pattern is one
small `<machine>.py` that imports a `MachineConfig` and shared
build steps from `_steps.py`.

## Licence

By contributing you agree that your contributions are licensed
under the [BSD 3-Clause License](LICENSE).
