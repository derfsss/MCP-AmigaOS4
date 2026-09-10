# Security policy

MCP-AmigaOS4 is a developer / lab tool. Both pieces — the host MCP
server and the AmigaOS-side daemon `MCPd` — expose a broad
capability surface (filesystem read / write, AmigaDOS shell
execution, kernel-state introspection, supervisor-mode physical-
memory reads on real hardware). The project ships with **no
built-in authentication, encryption, or access control on the
daemon's TCP listener**, and is designed to be operated on a
trusted, network-isolated link between the host running the MCP
server and the Amiga running `MCPd`.

## Operator responsibility

Responsibility for deploying this software safely rests with the
operator. In particular:

- **Network exposure**: `MCPd` listens on TCP port 4322 (and a UDP
  discovery responder on 4323) without authentication. **Do not
  expose those ports to an untrusted network.** Run targets on a
  private LAN, behind a host-only QEMU SLIRP NAT, or over an
  explicit point-to-point link to the host workstation. Anyone who
  can reach the listener can read and write any file accessible to
  the AmigaOS process and run arbitrary AmigaDOS commands.
- **Deployment context**: the daemon, by design, lets a remote
  client read `SYS:` (and any other mounted volume), overwrite
  files, run `exec.cmd` against the AmigaDOS shell, modify task
  state via `debug.write_register` / `debug.write_memory`, and
  trigger a soft power-off (`sys.cold_reboot`, `sys.mcu_cmd
  cmd="s"`). Treat a target running `MCPd` as fully owned by
  whoever holds the network path to it.
- **Out-of-band power control**: when the operator has wired the
  FTDI USB-TTL cable to the target's internal MCU header (X5000
  P18 / A1222 P15) and configured `[targets.<name>.channels.mcu]`,
  the host-side `power.*` tools (`power.on`, `power.off`,
  `power.toggle_stream`, `power.shell`) can boot, hard-reset, and
  shut down the box from MCP. This bypasses the daemon entirely
  and works regardless of AOS state. Treat the host workstation
  running `amiga-fleet-mcp` with cable attached as having physical
  power-button access to the target.
- **Keyboard / mouse injection is off by default**: the `input.*`
  methods (`input.type`, `input.key`, `input.click`, `input.drag`,
  `input.mouse_move`, `input.scroll`, `input.state`) let a remote
  client type and click on the target as the logged-in user, in
  whatever window happens to be focused. They are **disabled at the
  daemon** and return `-32003` until the operator either starts
  `MCPd` with `--enable-input` or creates the sentinel file
  `SYS:System/MCPd/ENABLE-INPUT` (see `MCPd-Enable-Input`) and
  restarts the daemon. The gate is read once at startup, so **no RPC
  method can switch it on** — enabling it needs filesystem access to
  the target. The host-side `[targets.<name>.input] enabled` flag is
  a wrong-target guard for the operator's convenience, **not** an
  access control: anything that can reach TCP 4322 bypasses the host
  entirely. Once the daemon gate is open, treat the machine as one
  whose keyboard and mouse are on the network. Do not enable it on a
  target reachable from an untrusted network.
- **Confirm-gated destructive operations**: `sys.cold_reboot`,
  `sys.mcu_cmd cmd="s"`, the mutating `installer.*` /
  `debug.write_*` tools, the destructive `power.*` tools
  (`power.on` / `power.off` / `power.toggle_stream` /
  `power.shell`), and the committing `input.*` tools
  (`input.type` / `input.key` / `input.click` / `input.drag`) all
  require an explicit `confirm: true` parameter. This is a guardrail against accidental fire, **not**
  an authentication mechanism. A connected client can always pass
  `confirm: true`.
- **Per-connection fault isolation is not a security boundary**:
  `MCPd` spawns a fresh AmigaOS Process per accepted connection so
  one buggy client can't take down the daemon's main listener.
  This is an availability property; it does not isolate clients
  from each other's filesystem effects.
- **Sensitive material**: do not rely on the project for secrets
  hygiene. Anything readable on the target Amiga is reachable via
  `fs.read`. Do not store credentials, keys, or other sensitive
  material on a volume `MCPd` can see. This extends to the host-side
  run archive: every tool call is recorded with its parameters, so
  **any string typed through `input.type` is written to the archive
  JSONL in cleartext**. That is intended — it is an audit log, and
  silently redacting one is worse than not keeping it — but it means
  you must not type passwords through `input.type`. The
  `text_sha256` field in the result exists so a reviewer can confirm
  what was typed without re-reading the plaintext.

## Design limits worth knowing

These are documented behaviours, not vulnerabilities:

- `sys.read_pa` performs an unchecked supervisor-mode read at any
  36-bit physical address. A bad address takes a DSI exception
  that crashes the connection-handler task. The daemon's main
  listener stays up by design.
- The `exec.cmd` argument quoting is best-effort. Pass user-
  supplied strings through your own shell quoting before handing
  them to the tool.
- The MCP framing layer caps individual frames at 32 MiB. Larger
  payloads must use `fs.write_chunk` (resumable, optionally
  zlib-compressed).
- `input.type` maps characters through the target's own
  `keymap.library`, so it is layout-correct. If `keymap.library`
  cannot be opened it silently falls back to a built-in US table;
  the result's `keymap` field reports which path ran, and callers
  who care should check it.
- MCPd announces the gate at startup in the kernel debug ring
  (`[MCPd] input_gate state=... source=...`, plus `input=on|off` on
  the readiness beacon), because its stdout banner goes to `NIL:` on
  the watchdog auto-start path. **Do not treat the ring as the
  authoritative reading**: AmigaOS's debug buffer does not wrap, so on
  a machine whose buffer is already full when MCPd starts -- a real
  X5000 with a verbose graphics driver fills ~80 KB during boot -- the
  daemon's lines never land there at all. `proto.capabilities`
  (`input.enabled`) is the reading that is always correct.
- `input.*` calls are capped per call (256 events, 20 s wall clock,
  512 characters of text) and the daemon always releases any held
  modifier or mouse button before returning, including on the abort
  path — a truncated `input.drag` cannot leave the target with a
  stuck mouse button.

## Supported versions

The latest tagged release on `main` is the only supported version.
There is no managed-disclosure process and no fix SLA — this is a
solo-maintained project. If you believe you've found a behaviour
that materially differs from the documented design, please open
an issue at <https://github.com/derfsss/MCP-AmigaOS4/issues>.
