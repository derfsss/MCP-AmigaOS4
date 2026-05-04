"""wb.* introspection: public screens + their windows (phase 5b)."""

from __future__ import annotations

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
