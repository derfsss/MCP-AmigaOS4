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


# ---- whole-file transfer wrappers ---------------------------------
#
# fs.upload / fs.download hide the chunk-size + zlib decisions so a
# user just points to a single file, and any size works. There's no
# user-visible "reassembly" -- chunks are written at byte offsets
# within the SAME destination file (uploads) or pasted in arrival
# order into a single output file (downloads). Both are resumable
# and offer optional SHA-256 verify.


class UploadResult(BaseModel):
    target: str
    local_path: str
    remote_path: str
    bytes_total: int
    bytes_sent_compressed: int
    chunks: int
    compressed_chunks: int
    elapsed_s: float
    speed_mib_s: float
    compression_ratio: float
    resumed_from: int = 0
    sha256_verified: bool = False
    sha256: str | None = None


class DownloadResult(BaseModel):
    target: str
    remote_path: str
    local_path: str
    bytes_total: int
    chunks: int
    elapsed_s: float
    speed_mib_s: float
    resumed_from: int = 0
    sha256_verified: bool = False
    sha256: str | None = None


async def fs_upload(
    fleet: Fleet,
    target: str,
    *,
    local_path: str,
    remote_path: str,
    chunk_size: int = 24 * 1024 * 1024,
    compression: Literal["none", "zlib", "auto"] = "auto",
    verify: bool = False,
    resume: bool = False,
) -> UploadResult:
    """Transfer a host-side file to the target. Works for any size
    and any byte content: NUL bytes, 0xFF, UTF-8 sequences,
    control characters all round-trip exactly. The wrapper:

    1. **Auto-chunks** files larger than will fit in one JSON-RPC
       frame, sending each chunk via `fs.write_chunk` at
       consecutive byte offsets in the same destination file.
       Files smaller than `chunk_size` go in one chunk.
    2. **Auto-base64-encodes** each chunk on the way out (the
       JSON-RPC envelope can't carry raw bytes). The daemon
       decodes on receipt; you give the wrapper a path, you get
       the bytes on the target.
    3. **Auto-zlib-compresses** each chunk when
       `compression="auto"` (default) and the result is at least
       5% smaller. Skips pre-compressed payloads (.lha / .zip /
       .iso) because their compressed form is no smaller.

    `resume=True` probes the remote with `fs.stat` and continues
    upload from the existing on-target size. Useful after a
    network drop. Does **not** verify that the partial bytes
    match -- pair with `verify=True` for end-to-end integrity.

    `verify=True` runs `fs.hash` (SHA-256) on the target plus a
    local hashlib walk, and raises `RuntimeError` if they
    disagree. Adds one extra round-trip + a full local read.

    No reassembly step: `fs.write_chunk` writes each chunk at its
    byte offset within the destination file, so the file is
    intact on disk the moment the last chunk lands.
    """
    import hashlib
    from pathlib import Path
    from time import monotonic

    from ..upload import chunked_upload  # local import to avoid cycle

    local = Path(local_path)
    if not local.is_file():
        raise FileNotFoundError(f"local file does not exist: {local}")
    total = local.stat().st_size

    resume_from = 0
    if resume:
        try:
            stat = await fs_stat(fleet, target, remote_path)
            resume_from = int(stat.size)
            if resume_from >= total:
                # Already complete -- short-circuit.
                ratio = 1.0
                return UploadResult(
                    target=target, local_path=str(local),
                    remote_path=remote_path,
                    bytes_total=total, bytes_sent_compressed=0,
                    chunks=0, compressed_chunks=0,
                    elapsed_s=0.0, speed_mib_s=0.0,
                    compression_ratio=ratio,
                    resumed_from=resume_from,
                )
        except Exception:
            resume_from = 0  # remote file doesn't exist; full upload

    t0 = monotonic()
    stats = await chunked_upload(
        fleet, target, local, remote_path,
        chunk_size=chunk_size, compression=compression,
        resume_from=resume_from,
    )

    sha = None
    sha_ok = False
    if verify:
        h = hashlib.sha256()
        with local.open("rb") as fh:
            for blk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(blk)
        sha = h.hexdigest()
        remote_hash = await fs_hash(fleet, target, remote_path, "sha256")
        if remote_hash.hash.lower() != sha.lower():
            raise RuntimeError(
                f"upload SHA-256 mismatch for {remote_path!r}: "
                f"local={sha} remote={remote_hash.hash}"
            )
        sha_ok = True

    elapsed = monotonic() - t0
    speed = (total / (1024 * 1024)) / elapsed if elapsed > 0 else 0.0
    return UploadResult(
        target=target,
        local_path=str(local),
        remote_path=remote_path,
        bytes_total=stats.bytes_total,
        bytes_sent_compressed=stats.bytes_sent_compressed,
        chunks=stats.chunks,
        compressed_chunks=stats.compressed_chunks,
        elapsed_s=stats.elapsed_s,
        speed_mib_s=round(speed, 2),
        compression_ratio=round(stats.compression_ratio, 3),
        resumed_from=resume_from,
        sha256_verified=sha_ok,
        sha256=sha,
    )


async def fs_download(
    fleet: Fleet,
    target: str,
    *,
    remote_path: str,
    local_path: str,
    chunk_size: int = 24 * 1024 * 1024,
    verify: bool = False,
    resume: bool = False,
) -> DownloadResult:
    """Transfer a target file to the host. Works for any size and
    any byte content. The wrapper:

    1. **Auto-pages** via repeated `fs.read(offset, length)`
       calls; one round-trip per `chunk_size` slice.
    2. **Auto-base64-decodes** each chunk on receipt (the daemon
       always returns base64 over the wire) before writing to
       the local file. Binary-clean: NUL bytes / 0xFF / arbitrary
       byte sequences all round-trip exactly.

    `resume=True`: continues append-mode from the existing local
    file size if a partial download is on disk.

    `verify=True`: SHA-256 both sides (target via `fs.hash`,
    local via hashlib) and raises `RuntimeError` on mismatch.

    Bytes land in arrival order at the matching offsets in the
    output file -- no separate reassembly step.
    """
    import hashlib
    from pathlib import Path
    from time import monotonic

    from ..upload import chunked_download

    local = Path(local_path)
    resume_from = 0
    if resume and local.is_file():
        resume_from = local.stat().st_size

    t0 = monotonic()
    stats = await chunked_download(
        fleet, target, remote_path, local,
        chunk_size=chunk_size, resume_from=resume_from,
    )

    sha = None
    sha_ok = False
    if verify:
        h = hashlib.sha256()
        with local.open("rb") as fh:
            for blk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(blk)
        sha = h.hexdigest()
        remote_hash = await fs_hash(fleet, target, remote_path, "sha256")
        if remote_hash.hash.lower() != sha.lower():
            raise RuntimeError(
                f"download SHA-256 mismatch for {remote_path!r}: "
                f"local={sha} remote={remote_hash.hash}"
            )
        sha_ok = True

    elapsed = monotonic() - t0
    speed = (stats.bytes_total / (1024 * 1024)) / elapsed if elapsed > 0 else 0.0
    return DownloadResult(
        target=target,
        remote_path=remote_path,
        local_path=str(local),
        bytes_total=stats.bytes_total,
        chunks=stats.chunks,
        elapsed_s=stats.elapsed_s,
        speed_mib_s=round(speed, 2),
        resumed_from=resume_from,
        sha256_verified=sha_ok,
        sha256=sha,
    )
