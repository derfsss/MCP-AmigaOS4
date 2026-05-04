# Third-party licences

Index of every third-party dependency this project links against, the
licence under which it is distributed, and the strategy by which we
incorporate it.

The project's policy in summary:

- Permissive licences only (MIT, BSD-2-Clause, BSD-3-Clause, Apache-2.0,
  ISC, Zlib, MPL-2.0).
- GPL is excluded for static linking.
- LGPL is acceptable for AmigaOS shared libraries that the project
  does not bundle (loaded dynamically at run time on the target).
- Vendored or vendorable dependencies are added as **git submodules**
  under `third-party/<name>/` rather than copied into our own source
  tree. The upstream's licence file is brought in via the submodule
  and referenced from this index; we do not duplicate licence text.

## Patching strategies

Each submodule is incorporated under one of three strategies, recorded
in the table below:

- **(a) Fork** — submodule points at a project fork (typically on the
  `derfsss` GitHub organisation), tracking a branch with our patches.
  Used when patches are substantial.
- **(b) Patches** — submodule points at an upstream pinned commit;
  patches live in `third-party/<name>-patches/*.patch` and are applied
  at build time. Used when patches are small.
- **(c) Clean** — submodule points at an upstream commit; no patches
  needed. The library compiles for AmigaOS 4 unmodified.

## When adding a new submodule

1. `git submodule add <url> third-party/<name>` (URL points upstream
   for strategies (b) and (c); at our fork for (a)).
2. Pin a specific commit or tag, not a branch.
3. Verify the upstream licence file is permissive and the SPDX
   identifier is on the allowed list above.
4. Append a row to the **Vendored libraries** table.
5. For strategy (b), create `third-party/<name>-patches/` and add
   patch files in deterministic order (`0001-*.patch`, etc.).
6. For strategy (a), fork on the project organisation, push the
   patched branch, point the submodule at the fork.
7. Place the licence's full text (verbatim) in `LICENSES/<SPDX>.txt`
   if it isn't already there.
8. Single commit: submodule add + this file's row + the licence text
   if new.

## Vendored libraries (submodules)

| Name | Submodule path | Pinned commit | Licence (SPDX) | Strategy | Upstream | Used for |
|---|---|---|---|---|---|---|
| cJSON | `third-party/cjson` | `acc7623` (v1.7.18) | MIT | (c) clean | https://github.com/DaveGamble/cJSON | JSON parsing and encoding inside MCPd. |

## Build-time dependencies (host side)

Declared in `host/pyproject.toml`. These are pip / uv dependencies,
not submodules. They install into the project's virtual environment;
no source is vendored into this repository.

| Name | Min version | Licence (SPDX) | Used for |
|---|---|---|---|
| `mcp[cli]` | 1.0 | MIT | Model Context Protocol SDK. |
| `pydantic` | 2.0 | MIT | Configuration and tool-parameter schemas. |
| `pyserial` | 3.5 | BSD-3-Clause | One-time serial-console bootstrap helper. |

## AmigaOS shared libraries linked at run time

These ship with AmigaOS or with separately-installed packages on the
target. The project does not bundle them; it loads them dynamically.
Listed for completeness and licence transparency.

| Library | Version | Licence | Notes |
|---|---|---|---|
| `bsdsocket.library` | Roadshow 4 or later | proprietary (ships with AmigaOS 4) | TCP / UDP sockets. |
| `dos.library`, `exec.library`, `intuition.library`, `application.library` | AmigaOS 4 base | proprietary (ships with AmigaOS 4) | Standard system libraries. |
| [`clib4`](https://github.com/AmigaLabs/clib4) (link-time) | 2.1 or later | GPL-2.0 with linking exception | POSIX-shaped C library. |
