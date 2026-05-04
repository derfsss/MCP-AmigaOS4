# Acknowledgements

MCP-AmigaOS4 stands on a generation of work by other people. The
following parties, projects, and libraries enabled it.

## Tooling

- **walkero** — for the
  [`walkero/amigagccondocker`](https://hub.docker.com/r/walkero/amigagccondocker)
  container image (`os4-gcc11` tag) that makes cross-compiling
  PowerPC AmigaOS 4 ELF a one-line operation on any host with Docker
  Desktop.
- **The QEMU project** — for `qemu-system-ppc` and the `pegasos2`,
  `amigaone`, and `sam460ex` machine implementations.
- **The adtools project** ([`sba1/adtools`](https://github.com/sba1/adtools))
  — for the cross and native PowerPC AmigaOS toolchain, including
  the patched GDB used for whole-system debugging.
- **Anthropic** — for the
  [Model Context Protocol](https://modelcontextprotocol.io)
  specification and the
  [`mcp` Python SDK](https://github.com/modelcontextprotocol/python-sdk)
  on which the host server is built.

## Third-party libraries

Compiled into MCPd, used at run time, or relied upon by the host:

- **cJSON** ([`DaveGamble/cJSON`](https://github.com/DaveGamble/cJSON))
  — MIT-licensed compact JSON parser. Vendored as a submodule at
  `third-party/cjson` (pinned at `v1.7.18`).
- **clib4** ([`AmigaLabs/clib4`](https://github.com/AmigaLabs/clib4))
  — modern POSIX-shaped C library for AmigaOS 4. Linked into MCPd
  by the cross-compile toolchain.
- **Public-domain SHA-256 reference implementation** — used by
  `fs.hash`. Inline in `mcpd/src/sha256.c`.
- **Pydantic** — typed-data backbone of the host server.
- **pyserial** — used by the host-side `serial.*` capture service
  for reading a target's debug UART.

The full per-dependency licence index is in
[THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).

If a credit is missing or wrong, please open an issue at
<https://github.com/derfsss/MCP-AmigaOS4/issues>.
