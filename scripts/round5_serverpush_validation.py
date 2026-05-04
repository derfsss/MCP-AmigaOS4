"""Validate server-push notifications via events.subscribe.

  1. Confirm initial state (no subscription).
  2. events.subscribe(["sys.lastalert", "debug.exception"]) - returns
     subscription mask.
  3. Read sys.lastalert baseline.
  4. Trigger an alert by calling sys.alert_decode with a known code;
     this doesn't actually post an alert (decode is read-only), so
     instead we use exec.cmd to run a command that *might* set an
     alert. Most reliable trigger: just wait and see if any kernel
     activity bumps LastAlert.
  5. Even without inducing an alert, verify the subscription state
     is queryable (events.subscribe accepts the params and returns
     the mask).
  6. events.unsubscribe - confirms tear-down.

  Notification path validation: spawn a background coroutine that
  registers a notification handler on the McpdTransport, then send
  some idle round-trips and wait briefly. We verify the queue stays
  empty under no-alert conditions (negative test - no spurious
  notifications).
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOST_SRC = HERE.parent / "host" / "src"
sys.path.insert(0, str(HOST_SRC))

from amiga_fleet_mcp.archive import Archive  # noqa: E402
from amiga_fleet_mcp.config import (  # noqa: E402
    Config, McpdChannel, PathsConfig, ServerConfig,
    TargetChannels, TargetConfig,
)
from amiga_fleet_mcp.fleet import Fleet  # noqa: E402

import os
X5000_ENDPOINT = os.environ.get("MCPD_ENDPOINT", "127.0.0.1:4322")


def make_config(archive_root: Path) -> Config:
    return Config(
        server=ServerConfig(archive_root=archive_root),
        paths=PathsConfig(),
        targets={
            "x5000-real": TargetConfig(
                type="remote", display_name="X5000",
                channels=TargetChannels(
                    mcpd=McpdChannel(endpoint=X5000_ENDPOINT),
                ),
            ),
        },
    )


async def amain() -> int:
    archive_root = HERE.parent / "tmp" / "round5-archive"
    archive_root.mkdir(parents=True, exist_ok=True)
    fleet = Fleet(make_config(archive_root))
    archive = Archive(archive_root)
    print(f"[setup] archive at {archive.run_dir}")

    target = "x5000-real"
    failures = 0

    print("\n[v] events.subscribe both topics")
    try:
        sub = await fleet.mcpd(target).request(
            "events.subscribe",
            {"topics": ["sys.lastalert", "debug.exception"]},
        )
        archive.log_call("events.subscribe", target,
                         {"topics": ["sys.lastalert", "debug.exception"]},
                         result=sub)
        print(f"    mask=0x{sub['topics_mask']:02x}  "
              f"subscribed={sub['subscribed']}")
        if sub["topics_mask"] != 0x03:
            print(f"    FAIL: expected mask=3 got {sub['topics_mask']}")
            failures += 1
        if set(sub["subscribed"]) != {"sys.lastalert", "debug.exception"}:
            print(f"    FAIL: subscribed list wrong")
            failures += 1
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    print("\n[v] negative test: no spurious notifications under idle")
    try:
        # Register a notification handler. Then sit idle for 1.5s and
        # send a couple of throwaway requests. Expect no notifications
        # since LastAlert is stable.
        seen: list[dict] = []
        def _on_note(obj: dict) -> None:
            seen.append(obj)
        unsub = fleet.mcpd(target).subscribe_notifications(_on_note)
        try:
            t0 = time.monotonic()
            for _ in range(3):
                await fleet.mcpd(target).request("proto.version")
                await asyncio.sleep(0.5)
            elapsed = time.monotonic() - t0
            print(f"    elapsed={elapsed:.2f}s  notifications={len(seen)}")
            if len(seen) != 0:
                print(f"    FAIL: spurious notifications: {seen}")
                failures += 1
        finally:
            unsub()
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    print("\n[v] subscribe with empty topics = unsubscribe-equivalent")
    try:
        sub = await fleet.mcpd(target).request(
            "events.subscribe", {"topics": []},
        )
        if sub["topics_mask"] != 0:
            print(f"    FAIL: empty subscription should be mask=0, "
                  f"got {sub['topics_mask']}")
            failures += 1
        else:
            print(f"    OK - mask=0")
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    print("\n[v] events.unsubscribe explicit")
    try:
        un = await fleet.mcpd(target).request("events.unsubscribe")
        archive.log_call("events.unsubscribe", target, {}, result=un)
        if not un.get("ok"):
            print(f"    FAIL: not ok: {un}")
            failures += 1
        else:
            print(f"    OK")
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    print("\n[v] events.test_emit synthesizes a notification end-to-end")
    try:
        seen2: list[dict] = []
        def _on_note(obj: dict) -> None:
            seen2.append(obj)
        unsub = fleet.mcpd(target).subscribe_notifications(_on_note)
        try:
            r = await fleet.mcpd(target).request(
                "events.test_emit",
                {"topic": "test.synthetic",
                 "data": {"hello": "world", "n": 42}},
            )
            archive.log_call("events.test_emit", target,
                             {"topic": "test.synthetic"}, result=r)
            # The notification fires on the next idle-poll tick (~200ms).
            # We need to give the daemon time to drain it. The cheapest
            # way is to make a request that itself triggers idle reading
            # on the connection - but `request` reads frames inline as
            # part of receiving its response. So fire one more request.
            await fleet.mcpd(target).request("proto.version")
            # The notification should have arrived as part of that
            # request's frame stream OR queued via subscribe handler.
            # Wait briefly to give the handler a chance to land.
            for _ in range(20):
                if seen2: break
                await asyncio.sleep(0.05)
            print(f"    received {len(seen2)} notification(s)")
            if not seen2:
                print("    FAIL: no notification fired after test_emit")
                failures += 1
            else:
                ev = seen2[-1]
                if ev.get("method") != "events.notify":
                    print(f"    FAIL: wrong method {ev.get('method')!r}")
                    failures += 1
                p = ev.get("params", {})
                if p.get("topic") != "test.synthetic":
                    print(f"    FAIL: wrong topic {p.get('topic')!r}")
                    failures += 1
                if p.get("data", {}).get("n") != 42:
                    print(f"    FAIL: data round-trip lost: {p.get('data')!r}")
                    failures += 1
                else:
                    print(f"    OK - topic={p.get('topic')!r}  "
                          f"data={p.get('data')!r}")
        finally:
            unsub()
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    print("\n[v] notification frame demuxing - send rapid requests "
          "while subscribed")
    try:
        # Re-subscribe, then fire many quick requests. We're testing
        # that the request/response demuxer doesn't get confused if
        # frames happen to arrive in unusual orderings. With no actual
        # alerts firing, it's effectively a stability test.
        await fleet.mcpd(target).request(
            "events.subscribe",
            {"topics": ["sys.lastalert", "debug.exception"]},
        )
        for _ in range(20):
            v = await fleet.mcpd(target).request("proto.version")
            assert "server" in v
        await fleet.mcpd(target).request("events.unsubscribe")
        print("    20 round-trips while subscribed: clean")
    except Exception as e:
        print(f"    FAIL: {e}")
        failures += 1

    await fleet.close_all()
    print(f"\n[result] failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
