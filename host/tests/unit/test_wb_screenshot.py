"""wb.screenshot host wrapper against a fake MCPd transport.

The daemon-side capture/encode is exercised on hardware; here we only
verify the host orchestration: it asks MCPd to capture to a remote
path, downloads that file, optionally inlines it as base64, and cleans
up the remote temp unless asked to keep it.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from amiga_fleet_mcp.config import (
    Config,
    McpdChannel,
    TargetChannels,
    TargetConfig,
)
from amiga_fleet_mcp.fleet import Fleet
from amiga_fleet_mcp.tools import fs as fs_tool
from amiga_fleet_mcp.tools import wb as wb_tool

# A tiny stand-in PNG (signature + filler). Content is opaque to the
# host wrapper, which just moves the bytes around.
FAKE_PNG = bytes([137, 80, 78, 71, 13, 10, 26, 10]) + b"\x00fake-idat\xff"


class FakeMcpd:
    """Stub MCPd transport: programmable per-method responses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []
        self.responses: dict[str, Any] = {}

    async def request(self, method: str, params: dict | None = None,
                      timeout_s: float = 30.0) -> Any:
        self.calls.append((method, params))
        return self.responses.get(method)


@pytest.fixture
def fleet_with_fake() -> tuple[Fleet, FakeMcpd]:
    cfg = Config(
        targets={
            "qemu-pegasos2": TargetConfig(
                type="qemu",
                channels=TargetChannels(
                    mcpd=McpdChannel(endpoint="127.0.0.1:4322"),
                ),
            )
        }
    )
    fleet = Fleet(cfg)
    fake = FakeMcpd()
    fleet._mcpd["qemu-pegasos2"] = fake  # type: ignore[assignment]
    return fleet, fake


@pytest.fixture
def stub_download(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace fs_download with a local writer so the test never hits a
    real transport. wb.screenshot imports fs_download at call time, so
    patching the module attribute is enough."""

    async def _dl(fleet: Any, target: str, *, remote_path: str,
                  local_path: str, **kw: Any) -> None:
        Path(local_path).write_bytes(FAKE_PNG)

    monkeypatch.setattr(fs_tool, "fs_download", _dl)


def _shot_response(**over: Any) -> dict[str, Any]:
    base = {
        "path": "T:mcpd-shot.png",
        "format": "png",
        "width": 640,
        "height": 480,
        "depth": 24,
        "screen_index": 0,
        "bytes": len(FAKE_PNG),
    }
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_screenshot_default_inlines_and_cleans_up(
    fleet_with_fake: Any, stub_download: None
) -> None:
    fleet, fake = fleet_with_fake
    fake.responses["wb.screenshot"] = _shot_response()

    res = await wb_tool.wb_screenshot(fleet, "qemu-pegasos2")

    # capture request shape
    assert fake.calls[0] == (
        "wb.screenshot", {"screen_index": 0, "path": "T:mcpd-shot.png"}
    )
    # result reflects the daemon metadata + downloaded bytes
    assert (res.width, res.height, res.depth) == (640, 480, 24)
    assert res.size == len(FAKE_PNG)
    assert res.image_b64 == base64.b64encode(FAKE_PNG).decode("ascii")
    assert res.remote_path == "T:mcpd-shot.png"
    assert res.saved_to is not None and Path(res.saved_to).exists()
    # remote temp is cleaned up by default
    assert ("fs.delete", {"path": "T:mcpd-shot.png"}) in fake.calls


@pytest.mark.asyncio
async def test_screenshot_save_path_keep_remote_no_inline(
    fleet_with_fake: Any, stub_download: None, tmp_path: Path
) -> None:
    fleet, fake = fleet_with_fake
    fake.responses["wb.screenshot"] = _shot_response(screen_index=2)
    out = tmp_path / "nested" / "shot.png"

    res = await wb_tool.wb_screenshot(
        fleet,
        "qemu-pegasos2",
        screen_index=2,
        save_path=str(out),
        keep_remote=True,
        inline=False,
    )

    # screen_index threaded into the capture call
    assert fake.calls[0][1]["screen_index"] == 2
    # saved to the requested host path (parent dirs created)
    assert out.read_bytes() == FAKE_PNG
    assert res.saved_to == str(out)
    assert res.screen_index == 2
    # inline disabled -> no base64 payload
    assert res.image_b64 is None
    # keep_remote -> no fs.delete issued
    assert all(c[0] != "fs.delete" for c in fake.calls)


@pytest.mark.asyncio
async def test_screenshot_in_fanout_allowlist() -> None:
    from amiga_fleet_mcp.tools.fleet import fanout_methods

    assert "wb.screenshot" in fanout_methods()
