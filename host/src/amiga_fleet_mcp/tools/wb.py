"""wb.* introspection: public screens + their windows (phase 5b)."""

from __future__ import annotations

import base64
import os
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from ..fleet import Fleet


class ScreenInfo(BaseModel):
    index: int
    title: str
    default_title: str
    left: int
    top: int
    width: int
    height: int
    bar_height: int
    flags: int
    window_count: int


class WindowInfo(BaseModel):
    title: str
    screen: str
    screen_index: int
    left: int
    top: int
    width: int
    height: int
    flags: int


class ScreensResult(BaseModel):
    screens: list[ScreenInfo]


class WindowsResult(BaseModel):
    windows: list[WindowInfo]


async def wb_screens(fleet: Fleet, target: str) -> ScreensResult:
    raw = await fleet.mcpd(target).request("wb.screens")
    return ScreensResult(
        screens=[ScreenInfo.model_validate(e) for e in raw]
    )


async def wb_windows(fleet: Fleet, target: str) -> WindowsResult:
    raw = await fleet.mcpd(target).request("wb.windows")
    return WindowsResult(
        windows=[WindowInfo.model_validate(e) for e in raw]
    )


class PublicScreenInfo(BaseModel):
    name: str
    priority: int


class PublicScreensResult(BaseModel):
    public_screens: list[PublicScreenInfo]


class FrontmostScreenInfo(BaseModel):
    title: str
    width: int
    height: int


class FrontmostWindowInfo(BaseModel):
    title: str
    width: int
    height: int
    left: int
    top: int


class FrontmostResult(BaseModel):
    frontmost_screen: FrontmostScreenInfo | None = None
    active_screen: dict[str, Any] | None = None
    active_window: FrontmostWindowInfo | None = None


async def wb_publicscreens(
    fleet: Fleet, target: str
) -> PublicScreensResult:
    raw = await fleet.mcpd(target).request("wb.publicscreens")
    return PublicScreensResult(
        public_screens=[PublicScreenInfo.model_validate(e) for e in raw]
    )


async def wb_frontmost(fleet: Fleet, target: str) -> FrontmostResult:
    raw = await fleet.mcpd(target).request("wb.frontmost")
    return FrontmostResult.model_validate(raw)


class ScreenshotResult(BaseModel):
    target: str
    screen_index: int
    width: int
    height: int
    depth: int
    size: int                 # bytes of the PNG
    format: str = "png"
    remote_path: str          # where the daemon wrote it on the Amiga
    saved_to: str | None = None   # host path it was downloaded to
    image_b64: str | None = None  # inline PNG (omitted when inline=False)


async def wb_screenshot(
    fleet: Fleet,
    target: str,
    *,
    screen_index: int = 0,
    save_path: str | None = None,
    remote_path: str = "T:mcpd-shot.png",
    keep_remote: bool = False,
    inline: bool = True,
) -> ScreenshotResult:
    """Capture a target screen to a PNG and bring it back to the host.

    Unlike ``qemu.screenshot`` (QMP ``screendump``, QEMU-only), this
    drives MCPd's on-Amiga ``wb.screenshot`` so it also works on real
    X5000 / A1222 hardware. The daemon grabs the screen via
    ``graphics.library`` ReadPixelArray and PNG-encodes it with
    ``z.library``; this wrapper then ``fs.download``s the file.

    `screen_index`: 0 = frontmost; higher = further back in the screen
    list. `save_path`: host path to keep the PNG (a tempfile is used
    otherwise). `keep_remote`: leave the file on the Amiga (default
    deletes the daemon-written temp). `inline`: embed the PNG bytes as
    base64 in the result (default True, mirroring qemu.screenshot).
    """
    from .fs import fs_download

    mcpd = fleet.mcpd(target)
    raw = await mcpd.request(
        "wb.screenshot",
        {"screen_index": screen_index, "path": remote_path},
    )

    if save_path:
        out = Path(save_path)
        out.parent.mkdir(parents=True, exist_ok=True)
    else:
        fd, tmp_path = tempfile.mkstemp(
            prefix="amiga_fleet_wb_", suffix=".png",
        )
        os.close(fd)
        out = Path(tmp_path)

    await fs_download(
        fleet, target, remote_path=raw["path"], local_path=str(out),
    )
    data = out.read_bytes()

    if not keep_remote:
        # Best-effort cleanup of the daemon-written temp; a failure
        # here must not fail the screenshot itself.
        try:
            await mcpd.request("fs.delete", {"path": raw["path"]})
        except Exception:
            pass

    return ScreenshotResult(
        target=target,
        screen_index=int(raw["screen_index"]),
        width=int(raw["width"]),
        height=int(raw["height"]),
        depth=int(raw["depth"]),
        size=len(data),
        format=str(raw.get("format", "png")),
        remote_path=str(raw["path"]),
        saved_to=str(out),
        image_b64=base64.b64encode(data).decode("ascii") if inline else None,
    )
