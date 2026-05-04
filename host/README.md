# amiga-fleet-mcp

Host-side MCP server for the MCP-AmigaOS4 project. See the top-level
documentation set:

- [USAGE.md](../USAGE.md) — overview, configuration, and per-tool
  parameter defaults (`[defaults]` block in `config.toml`).
- [INSTALL.md](../INSTALL.md) — installation requirements and
  procedure, including per-feature prerequisites.
- [BUILD.md](../BUILD.md) — building from source.
- [COMMANDS.md](../COMMANDS.md) — full command, tool, method, and
  resource reference.

## Quick development setup

```sh
uv sync                                     # or: pip install -e .[dev]
uv run amiga-fleet-mcp --version
uv run amiga-fleet-mcp --list-tools
uv run pytest -q
uv run ruff check src tests
uv run mypy src
```

The server is invoked as a stdio subprocess by an MCP-aware client.
Direct invocation is for diagnostics only — see
[COMMANDS.md](../COMMANDS.md) for the full CLI flag list.
