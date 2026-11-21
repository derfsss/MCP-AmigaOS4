"""events.* - long-poll event channel (phase 5 minimum).

`events.wait(topics, timeout_ms)` snapshots a few exec-level state
sources at call start, polls every 200ms in the daemon, and returns
the first list of deltas it sees (or [] on timeout).

Topics:
- `sys.lastalert`    - ExecBase->LastAlert[0] changed (raw).
- `sys.task`         - task added or removed (by name).
- `debug.exception`  - new dead-end / crash alert (decoded).

MCPd also has a server-push mode (`events.subscribe` /
`events.unsubscribe`, sys.lastalert and debug.exception only): the
daemon sends JSON-RPC notifications on that connection's idle-poll
path. McpdTransport passes such frames to `subscribe_notifications()`
handlers, but no MCP tool exposes the subscription; this module is
long-poll only.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from ..fleet import Fleet


class EventEntry(BaseModel):
    topic: str
    data: dict[str, Any]


class EventsWaitResult(BaseModel):
    events: list[EventEntry]
    elapsed_ms: int


async def events_wait(
    fleet: Fleet, target: str,
    topics: list[
        Literal["sys.lastalert", "sys.task", "debug.exception"]
    ] | None = None,
    timeout_ms: int = 5000,
) -> EventsWaitResult:
    params: dict[str, Any] = {"timeout_ms": timeout_ms}
    if topics is not None:
        params["topics"] = topics
    raw = await fleet.mcpd(target).request(
        "events.wait", params,
        timeout_s=max(timeout_ms / 1000.0 + 5.0, 30.0),
    )
    return EventsWaitResult.model_validate(raw)
