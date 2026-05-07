# AGENTS_SETUP.md — install spec for AI agents

This file is a deterministic, machine-followable spec for setting
up `amiga-fleet-mcp` on a fresh host. Point an AI agent at this
file and it can reach a working install + validated config + MCP
client wiring without further human input — provided the user
supplies the few facts the host can't introspect (target IPs,
optional FTDI cable port).

If you are a human, [INSTALL.md](INSTALL.md) is friendlier — read
that instead. This file is intentionally terse.

## Pre-conditions the agent must verify first

Run these checks. If any FAIL, **stop and ask the user**; don't
guess.

| Check                              | Command (Windows / *nix)                                              | Pass criterion |
|---|---|---|
| Git available                      | `git --version`                                                       | Exits 0. |
| Python ≥ 3.11                      | `py -3 --version` / `python3 --version`                               | Exits 0 and major ≥ 3, minor ≥ 11. |
| Repo cloned at known path          | `git rev-parse --show-toplevel` from inside the repo                  | Returns the MCP-AmigaOS4 root. |
| `host/` directory present          | `Test-Path host/pyproject.toml` / `test -f host/pyproject.toml`       | Exits 0. |

## Install (deterministic)

```sh
cd host
py -3 -m venv .venv          # *nix: python3 -m venv .venv
.venv\Scripts\activate       # *nix: . .venv/bin/activate
pip install -e .
amiga-fleet-mcp --version    # → "amiga-fleet-mcp <version>"
```

If `pip install -e .` fails, capture the full error and stop.
Common causes: no internet, corporate proxy, missing C toolchain
for `pyserial` (rare on Windows; install Build Tools for Visual
Studio if so).

## Config generation: pick exactly one path

### Path A — `--init` (preferred when interaction is allowed)

```sh
amiga-fleet-mcp --init
```

Answer prompts. The wizard validates through the same schema the
server uses; a successful exit means the config loads cleanly.

### Path B — `--init --non-interactive` (smoke / CI)

```sh
amiga-fleet-mcp --init --non-interactive --force
```

Writes a server-only stub with no targets. Confirm with
`amiga-fleet-mcp --inspect` (will print "targets: []").

### Path C — generate config directly (no interaction)

Use this when the agent has all answers up-front (e.g. running
under a fully autonomous orchestrator). Build the TOML, write it
to the platform default path, validate with `--inspect`.

#### Detect what to ask the user about

| Question                   | Detection command                                                       | If absent |
|---|---|---|
| QEMU host running?         | `Test-Path "$env:USERPROFILE/Projects/qemu-runner"`                     | Skip QEMU targets entirely. |
| AmigaQemuTests checkout?   | `Test-Path "$env:USERPROFILE/Projects/AmigaQemuTests"`                  | Omit `paths.amiga_qemu_tests`; `tests.*` returns `InvalidParams`. |
| `qemu-system-ppc` on PATH? | `(Get-Command qemu-system-ppc -ErrorAction SilentlyContinue).Path`      | Omit `paths.qemu_binary`; `qemu.start` returns `InvalidParams`. |
| FTDI cable wired?          | Windows: `Get-PnpDevice -FriendlyName "*FTDI*" -Status OK`; *nix: `ls /dev/ttyUSB*` | Omit `[targets.*.channels.mcu]`; `power.*` returns `NotCapable` per target. |

#### Decision tree

```
Is the user driving any QEMU guests?
├── No  → omit [paths.qemu_runner], [paths.qemu_binary]; no qemu-* targets.
└── Yes → set both. For each guest: ask machine (pegasos2 / amigaone
          / sam460ex), qemu_config path, hostfwd ports.

Is the user driving any real AmigaOS hardware?
├── No  → no remote targets.
└── Yes → for each: ask the IP, set
          [targets.<name>.channels.mcpd] endpoint = "<ip>:4322".
          If FTDI cable detected and wired to P18 (X5000) or P15
          (A1222), additionally set [targets.<name>.channels.mcu]
          with the detected port and baud = 38400.

Is the user running test orchestration?
├── No  → omit [paths.amiga_qemu_tests].
└── Yes → set it.
```

#### Minimal config templates (start from one of these)

Single QEMU Pegasos2 only:

```toml
[server]
log_level = "info"
log_dir = "<user-cache>/amiga-fleet-mcp/logs"
archive_root = "<user-cache>/amiga-fleet-mcp/archive"
mcp_transport = "stdio"
default_target = "qemu-pegasos2"

[paths]
qemu_runner = "<absolute path>"
qemu_binary = "<absolute path to qemu-system-ppc>"

[targets.qemu-pegasos2]
type = "qemu"
machine = "pegasos2"
qemu_config = "<absolute path to config.json>"

[targets.qemu-pegasos2.channels.mcpd]
enabled = true
endpoint = "127.0.0.1:4422"

[targets.qemu-pegasos2.channels.qmp]
enabled = true
endpoint = "127.0.0.1:14422"
```

Single real X5000 only:

```toml
[server]
log_level = "info"
log_dir = "<user-cache>/amiga-fleet-mcp/logs"
archive_root = "<user-cache>/amiga-fleet-mcp/archive"
mcp_transport = "stdio"
default_target = "x5000"

[targets.x5000]
type = "remote"
display_name = "X5000"
tags = ["real-hw", "x5000"]

[targets.x5000.channels.mcpd]
enabled = true
endpoint = "<target-ip>:4322"

# Only set if the FTDI cable is detected. Required for power.*.
# [targets.x5000.channels.mcu]
# enabled = true
# port = "COM5"          # or /dev/ttyUSB1 on Linux/macOS
# baud = 38400
```

Mixed fleet: combine both `[targets.*]` blocks; pick a sensible
`default_target`.

## Validation steps (mandatory)

Run all four. If any fails, fix before declaring success.

```sh
# 1. Schema is valid + targets present.
amiga-fleet-mcp --inspect
# Expected: prints each target with its type and mcpd endpoint.

# 2. List the live tool surface (no target reach required).
amiga-fleet-mcp --list-tools | wc -l
# Expected: 121 tools as of v1.2.

# 3. Probe every target's MCPd channel (target reach required).
amiga-fleet-mcp --health-check
# Expected: exits 0; non-zero means at least one target is down.

# 4. Discover any other MCPd instances on the LAN (optional sanity).
amiga-fleet-mcp --discover --discover-timeout-ms 3000
# Expected: prints any reachable MCPd, or nothing if isolated.
```

## Wire the server into Claude Code

```sh
claude mcp add amiga-fleet -- amiga-fleet-mcp --config <absolute-path-to-config.toml>
```

Verify with `claude mcp list` then issue a tool call from
within Claude Code: `proto.capabilities` (no target arg if
`default_target` is set, otherwise pass `target=<name>`). A
successful response with `methods: [...]` confirms end-to-end
wiring.

## Common errors and what they mean

| Error message fragment                            | Root cause                                                              |
|---|---|
| `paths.qemu_runner not set in config`             | `qemu.*` or QMP transport invoked but `[paths] qemu_runner` is missing. Either set it or stop using those tools. |
| `paths.qemu_binary not set in config`             | `qemu.start` invoked but no `[paths] qemu_binary`. Set it. |
| `paths.amiga_qemu_tests not set in config`        | `tests.*` invoked but no `[paths] amiga_qemu_tests`. Set it. |
| `target.qemu_config is required for qemu.start`   | Target's `qemu_config` path missing from `[targets.<name>]`. Set it. |
| `NotCapable: channel mcu`                         | `power.*` invoked but `[targets.<name>.channels.mcu]` not configured. Wire the FTDI cable, set the block. |
| `FileNotFoundError: config not found`             | `--config` points at a path that doesn't exist, or no config at the platform default. Run `--init`. |
| `bad config: ...`                                 | TOML doesn't validate against the schema. Re-run `--init`; the wizard pre-validates. |

## Things this spec deliberately doesn't do

- **Doesn't install MCPd on a target.** That's a separate
  procedure (see [INSTALL.md § Installing MCPd on a target](INSTALL.md#installing-mcpd-on-a-target))
  — it requires the target to already be reachable and is
  inherently more interactive.
- **Doesn't choose secrets or credentials.** MCPd has no
  authentication on its TCP listener; the security stance is
  network-level isolation (private LAN, host-only QEMU NAT,
  point-to-point link). Don't expose port 4322 / 4323 to an
  untrusted network. If asked about auth, point the user at
  [SECURITY.md](SECURITY.md).
- **Doesn't modify `~/.claude` or the user's MCP client config
  beyond the single `claude mcp add` call.** That call is the
  only client-side change.
