---
name: Bug report
about: Something doesn't work the way the docs say it should
title: ''
labels: bug
assignees: ''
---

<!-- There is no separate channel for "security" reports — file
     anything here. The daemon ships with no auth on its TCP
     listener and is designed for trusted, network-isolated
     deployments; see SECURITY.md for the threat model. -->

## What happened?

A clear, concise description of the actual behaviour.

## What did you expect?

A clear, concise description of what you expected.

## Reproduction steps

1.
2.
3.

## Environment

- **Target**: <!-- e.g. real X5000, QEMU pegasos2, A1222 -->
- **AmigaOS version**: <!-- output of `MCPd --version` on the target,
  or `Version` from a Shell -->
- **Host OS**: <!-- e.g. Linux x86_64, macOS arm64, Windows 10 -->
- **Python**: <!-- `python --version` -->
- **`amiga-fleet-mcp` version**: <!-- `amiga-fleet-mcp --version` -->
- **MCP client**: <!-- e.g. Claude Code 1.0.x, Claude Desktop -->

## Logs

<details>
<summary>Relevant log output</summary>

```
<!-- Paste the smallest useful slice. For MCPd crashes, include
     C:DumpDebugBuffer output from the target. -->
```

</details>

## Anything else?
