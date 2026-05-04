"""Filesystem methods backed by MCPd (phase 4)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..fleet import Fleet


class DirEntry(BaseModel):
    name: str
    type: Literal["file", "dir"]
    size: int = 0


class FsListResult(BaseModel):
    path: str
    entries: list[DirEntry]


class StatResult(BaseModel):
    name: str
    type: Literal["file", "dir"]
    size: int = 0
    modified: str | None = None


class FsReadResult(BaseModel):
    path: str
    size: int
    content_b64: str = Field(description="base64 of the bytes returned")
    total_size: int | None = None
    offset: int | None = None


class FsWriteResult(BaseModel):
    path: str
    size: int


class FsOk(BaseModel):
    path: str
    ok: Literal[True] = True


# ---------- public methods -----------------------------------------


async def fs_list(
    fleet: Fleet,
    target: str,
    path: str,
    recursive: bool = False,
    max_depth: int = 8,
) -> FsListResult:
    t = fleet.mcpd(target)
    params: dict[str, object] = {"path": path}
    if recursive:
        params["recursive"] = True
        params["max_depth"] = int(max_depth)
    raw = await t.request("fs.list", params)
    return FsListResult(
        path=path,
        entries=[DirEntry.model_validate(e) for e in raw],
    )


async def fs_stat(fleet: Fleet, target: str, path: str) -> StatResult:
    t = fleet.mcpd(target)
    raw = await t.request("fs.stat", {"path": path})
    return StatResult.model_validate(raw)


async def fs_read(
    fleet: Fleet,
    target: str,
    path: str,
    offset: int | None = None,
    length: int | None = None,
) -> FsReadResult:
    t = fleet.mcpd(target)
    params: dict[str, object] = {"path": path}
    if offset is not None:
        params["offset"] = int(offset)
    if length is not None:
        params["length"] = int(length)
    raw = await t.request("fs.read", params, timeout_s=120.0)
    return FsReadResult.model_validate(raw)


async def fs_write(
    fleet: Fleet, target: str, path: str, content_b64: str,
    compression: Literal["none", "zlib"] = "none",
    raw_size: int | None = None,
) -> FsWriteResult:
    """Write a file in one shot.

    Set compression='zlib' if `content_b64` is base64 of zlib-compressed
    bytes; pass `raw_size` (decompressed length) so the daemon
    pre-allocates exactly. Effective single-frame size limit stays at
    32 MiB *post-base64* - so for files >24 MiB raw use fs.write_chunk
    instead.
    """
    t = fleet.mcpd(target)
    params: dict[str, object] = {"path": path, "content_b64": content_b64}
    if compression != "none":
        params["compression"] = compression
    if raw_size is not None:
        params["raw_size"] = int(raw_size)
    raw = await t.request("fs.write", params, timeout_s=120.0)
    return FsWriteResult.model_validate(raw)


class FsWriteChunkResult(BaseModel):
    path: str
    offset: int
    written: int
    total_so_far: int


async def fs_write_chunk(
    fleet: Fleet, target: str, path: str, offset: int, content_b64: str,
    compression: Literal["none", "zlib"] = "none",
    raw_size: int | None = None,
    truncate: bool | None = None,
    total_size: int | None = None,
) -> FsWriteChunkResult:
    """Write a chunk of a file at a given offset (option B).

    First chunk (offset=0) truncates by default; subsequent chunks
    seek-and-write. Pass truncate=False on the first chunk to opt
    into "patch in place". Set compression='zlib' to ship each chunk
    pre-compressed (option F); pair with raw_size for exact-size
    decoding. Set total_size on the first chunk to let the daemon
    pre-extend the file (single ChangeFileSize, avoids repeated
    metadata updates).

    Resume semantics: a partial file from a crashed upload is left
    on disk. Caller can retry with the appropriate next offset.
    """
    t = fleet.mcpd(target)
    params: dict[str, object] = {
        "path": path, "offset": int(offset), "content_b64": content_b64,
    }
    if compression != "none":
        params["compression"] = compression
    if raw_size is not None:
        params["raw_size"] = int(raw_size)
    if truncate is not None:
        params["truncate"] = bool(truncate)
    if total_size is not None:
        params["total_size"] = int(total_size)
    # Timeout is 300s rather than 120s because cJSON_Parse on a multi-
     # MiB JSON body can be slow on AOS4 - the daemon isn't hung, just
     # busy. Test runs have shown >2 min per 10 MiB chunk on first
     # call after restart.
    raw = await t.request("fs.write_chunk", params, timeout_s=300.0)
    return FsWriteChunkResult.model_validate(raw)


async def fs_delete(
    fleet: Fleet, target: str, path: str, recursive: bool = False,
) -> FsOk:
    t = fleet.mcpd(target)
    params: dict[str, object] = {"path": path}
    if recursive:
        params["recursive"] = True
    raw = await t.request("fs.delete", params, timeout_s=60.0)
    return FsOk(path=raw.get("path", path))


async def fs_makedir(fleet: Fleet, target: str, path: str) -> FsOk:
    t = fleet.mcpd(target)
    raw = await t.request("fs.makedir", {"path": path})
    return FsOk(path=raw.get("path", path))


# ---------- phase 1+: rename / protect / copy ---------------------


class FsRenameResult(BaseModel):
    src: str
    dst: str
    ok: Literal[True] = True


class FsProtectResult(BaseModel):
    path: str
    bits: int
    ok: Literal[True] = True


class FsCopyResult(BaseModel):
    src: str
    dst: str
    ok: Literal[True] = True
    output: str = ""


async def fs_rename(
    fleet: Fleet, target: str, src: str, dst: str
) -> FsRenameResult:
    t = fleet.mcpd(target)
    raw = await t.request("fs.rename", {"src": src, "dst": dst})
    return FsRenameResult(src=raw.get("src", src), dst=raw.get("dst", dst))


async def fs_protect(
    fleet: Fleet, target: str, path: str, bits: int
) -> FsProtectResult:
    t = fleet.mcpd(target)
    raw = await t.request("fs.protect", {"path": path, "bits": bits})
    return FsProtectResult(path=raw.get("path", path),
                           bits=raw.get("bits", bits))


class FsHashResult(BaseModel):
    path: str
    algo: str
    hash: str
    size: int


async def fs_hash(
    fleet: Fleet, target: str, path: str, algo: str = "sha256",
) -> FsHashResult:
    """Compute a digest of a file. Currently only `sha256` is
    implemented daemon-side (md5/sha1 raise NotCapable)."""
    raw = await fleet.mcpd(target).request(
        "fs.hash", {"path": path, "algo": algo}, timeout_s=120.0,
    )
    return FsHashResult.model_validate(raw)


async def fs_copy(
    fleet: Fleet, target: str, src: str, dst: str
) -> FsCopyResult:
    t = fleet.mcpd(target)
    raw = await t.request("fs.copy", {"src": src, "dst": dst}, timeout_s=60.0)
    return FsCopyResult(
        src=raw.get("src", src),
        dst=raw.get("dst", dst),
        output=raw.get("output", ""),
    )
