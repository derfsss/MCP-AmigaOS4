"""High-level chunked-upload helper that drives fs.write_chunk.

Hides the chunking + zlib + retry-on-fail mechanics so callers can
say `upload(target, local_path, remote_path)` for any file size.

Strategy:
  1. Slice local file into chunks of `chunk_size` (default ~24 MiB
     of *raw* bytes - leaves margin under the 32 MiB JSON-RPC cap
     after base64 expansion).
  2. zlib-compress each chunk if compression='zlib'/'auto'. 'auto'
     compresses only if the result is at least 5% smaller (skips
     pre-compressed payloads like .lha / .zip).
  3. Send via fs.write_chunk with monotonically increasing offsets;
     first chunk includes total_size hint so the daemon pre-extends.
  4. On per-chunk failure: retry up to `retries` times before raising.
     Resume after total failure is the caller's responsibility (use
     fs.stat to find the existing on-disk size, then call
     `chunked_upload(..., resume_from=existing_size)`).

Returns a small UploadStats record with byte / chunk / wall-time
counters useful for logging.
"""

from __future__ import annotations

import asyncio
import base64
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .errors import InvalidParams, TargetError
from .fleet import Fleet
from .tools import fs as fs_tool
from .transports.mcpd import StreamBroken

# Chunk default: 24 MiB raw bytes, as the tools document it. base64
# makes 24 MiB exactly 32 MiB, the frame limit, with no room for the
# JSON envelope, so a chunk that goes uncompressed (compression="none",
# or "auto" on data zlib cannot shrink) failed "frame too large".
# chunked_upload clamps every chunk to MAX_CHUNK_RAW instead.
DEFAULT_CHUNK_RAW = 24 * 1024 * 1024
#: The largest raw chunk whose base64 plus a 64 KiB envelope allowance
#: fits one frame (MAX_FRAME_BYTES, 32 MiB).
MAX_CHUNK_RAW = (32 * 1024 * 1024 - 64 * 1024) // 4 * 3


@dataclass
class UploadStats:
    path: str
    bytes_total: int
    bytes_sent_compressed: int  # what actually went over the wire (pre-b64)
    chunks: int
    compressed_chunks: int
    elapsed_s: float
    #: Every stream's CRC-32 matched fs.stream_status (streams only).
    crc_checked: bool = False

    @property
    def compression_ratio(self) -> float:
        if self.bytes_total == 0:
            return 1.0
        return self.bytes_sent_compressed / self.bytes_total


def _zlib_chunk(raw: bytes) -> bytes:
    """Compress with default level. Streaming would save peak RAM
    but for a single chunk (<=24 MiB) the one-shot is simpler."""
    return zlib.compress(raw, 6)


async def chunked_upload(
    fleet: Fleet,
    target: str,
    local_path: str | Path,
    remote_path: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_RAW,
    compression: Literal["none", "zlib", "auto"] = "auto",
    retries: int = 2,
    resume_from: int = 0,
    total_size_hint: bool = True,
) -> UploadStats:
    local = Path(local_path)
    if not local.is_file():
        raise FileNotFoundError(f"local file does not exist: {local}")
    total = local.stat().st_size
    chunk_size = max(1, min(chunk_size, MAX_CHUNK_RAW))

    sent_compressed = 0
    chunk_count = 0
    compressed_count = 0
    t0 = time.monotonic()

    with local.open("rb") as fh:
        if resume_from > 0:
            fh.seek(resume_from)
        offset = resume_from
        first = (resume_from == 0)
        while True:
            raw = fh.read(chunk_size)
            if not raw:
                break

            # Compression decision
            payload = raw
            mode: Literal["none", "zlib"] = "none"
            if compression == "zlib":
                payload = _zlib_chunk(raw)
                mode = "zlib"
            elif compression == "auto":
                cand = _zlib_chunk(raw)
                if len(cand) <= int(len(raw) * 0.95):
                    payload = cand
                    mode = "zlib"

            content_b64 = base64.b64encode(payload).decode("ascii")
            kwargs: dict[str, object] = {
                "compression": mode,
            }
            if mode == "zlib":
                kwargs["raw_size"] = len(raw)
            if first and total_size_hint:
                kwargs["total_size"] = total
            # First chunk: default truncate=True (matches fs.write).
            # Later chunks: leave default (False) so we don't truncate
            # mid-upload.

            last_err: Exception | None = None
            for attempt in range(retries + 1):
                try:
                    await fs_tool.fs_write_chunk(
                        fleet, target, remote_path, offset, content_b64,
                        compression=mode,
                        raw_size=(
                            int(kwargs["raw_size"])  # type: ignore[call-overload]
                            if mode == "zlib" else None
                        ),
                        total_size=(
                            int(kwargs["total_size"])  # type: ignore[call-overload]
                            if "total_size" in kwargs else None
                        ),
                    )
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    if attempt < retries:
                        # Brief backoff. `await`, not time.sleep: this
                        # runs on the event loop, and a blocking sleep
                        # here freezes every other target's calls too --
                        # exactly when one of them is already struggling.
                        await asyncio.sleep(0.5 * (attempt + 1))
            if last_err is not None:
                raise RuntimeError(
                    f"fs.write_chunk failed at offset={offset} "
                    f"after {retries+1} attempts: {last_err}"
                ) from last_err

            sent_compressed += len(payload)
            chunk_count += 1
            if mode == "zlib":
                compressed_count += 1
            offset += len(raw)
            first = False

    return UploadStats(
        path=remote_path,
        bytes_total=total,
        bytes_sent_compressed=sent_compressed,
        chunks=chunk_count,
        compressed_chunks=compressed_count,
        elapsed_s=time.monotonic() - t0,
    )


# ---- download path ------------------------------------------------


@dataclass
class DownloadStats:
    path: str
    bytes_total: int
    chunks: int
    elapsed_s: float
    #: Every stream's CRC-32 matched fs.stream_status (streams only).
    crc_checked: bool = False


# Conservative read default: 24 MiB raw bytes per fs.read call.
# fs.read returns raw bytes (no compression on this side yet) so
# stays comfortably under the 32 MiB framing cap after base64.
DEFAULT_READ_RAW = 24 * 1024 * 1024


async def chunked_download(
    fleet: Fleet,
    target: str,
    remote_path: str,
    local_path: str | Path,
    *,
    chunk_size: int = DEFAULT_READ_RAW,
    retries: int = 2,
    resume_from: int = 0,
) -> DownloadStats:
    """Pull a file from the target by repeated `fs.read(offset,
    length)` calls; reassembles into a single local file.

    Resumable: pass `resume_from = N` (or use `verify=True` flag in
    the wrapper tool) to start from byte N. Caller is responsible
    for opening the local file in append-binary mode if resuming.

    The remote file is read in size order via consecutive offsets;
    no out-of-order assembly is needed -- bytes land at the same
    offset they came from.
    """
    local = Path(local_path)
    local.parent.mkdir(parents=True, exist_ok=True)

    # Probe remote size via fs.stat so we know when to stop.
    t = fleet.mcpd(target)
    stat_raw = await t.request("fs.stat", {"path": remote_path}, timeout_s=30.0)
    total = int(stat_raw["size"])

    chunk_count = 0
    t0 = time.monotonic()

    mode = "ab" if resume_from > 0 else "wb"
    with local.open(mode) as fh:
        offset = resume_from
        while offset < total:
            length = min(chunk_size, total - offset)
            last_err: Exception | None = None
            for attempt in range(retries + 1):
                try:
                    raw = await t.request(
                        "fs.read",
                        {"path": remote_path, "offset": offset,
                         "length": length},
                        timeout_s=120.0,
                    )
                    last_err = None
                    break
                except Exception as e:
                    last_err = e
                    if attempt < retries:
                        await asyncio.sleep(0.5 * (attempt + 1))
            if last_err is not None:
                raise RuntimeError(
                    f"fs.read failed at offset={offset} "
                    f"after {retries+1} attempts: {last_err}"
                ) from last_err
            assert raw is not None  # for type-checker
            payload = base64.b64decode(raw["content_b64"])
            fh.write(payload)
            offset += len(payload)
            chunk_count += 1
            if len(payload) == 0:
                # Avoid infinite loop on a malformed daemon reply
                break

    return DownloadStats(
        path=str(local),
        bytes_total=total,
        chunks=chunk_count,
        elapsed_s=time.monotonic() - t0,
    )


# ---- raw streams (fs.stream_put / fs.stream_get, MCPd 2.3+) --------

#: Bytes per write/read on a stream: matches the daemon's 1 MiB buffer.
STREAM_BLOCK = 1024 * 1024
#: How long one block may take before the stream counts as broken. The
#: daemon's own limits are SO_RCVTIMEO / SO_SNDTIMEO, 120 s
#: (mcpd/src/main.c).
STREAM_BLOCK_TIMEOUT_S = 130.0
#: Before resuming a broken upload: how long to wait for the target's
#: file to stop growing. The daemon may still be writing what was in
#: its buffers when the host gave up, and holds the file open until it
#: notices the connection has gone (at once on a reset, up to its
#: receive timeout when the host vanished).
RESUME_SETTLE_S = 1.0
RESUME_SETTLE_MAX_S = 150.0


class StreamCorrupt(Exception):
    """fs.stream_status's CRC-32 of a stream differs from the host's:
    the bytes changed on the way. The transfer redoes that stream."""


async def _caps(t: object) -> list[str]:
    """proto.capabilities' method names, cached on the transport; any
    failure counts as none (older daemons keep the chunked path)."""
    cached = getattr(t, "_method_names", None)
    if isinstance(cached, list):
        return cached
    names: list[str] = []
    if hasattr(t, "open_stream"):
        try:
            caps = await t.request("proto.capabilities", timeout_s=10.0)  # type: ignore[attr-defined]
            got = caps.get("method_names", []) if isinstance(caps, dict) else []
            names = [str(n) for n in got]
        except Exception:
            names = []
    try:
        t._method_names = names  # type: ignore[attr-defined]
    except AttributeError:
        pass
    return names


async def stream_supported(t: object) -> bool:
    """Whether this target's MCPd has the raw stream methods."""
    names = await _caps(t)
    return "fs.stream_put" in names and "fs.stream_get" in names


async def _stream_crc_supported(t: object) -> bool:
    """Whether this target's MCPd reports each stream's CRC-32
    (fs.stream_status); without it a stream is checked by size only."""
    return "fs.stream_status" in await _caps(t)


async def _check_crc(s: object, crc: int, nbytes: int, path: str) -> None:
    st = await s.call("fs.stream_status", {}, timeout_s=30.0)  # type: ignore[attr-defined]
    their_crc = int(st["crc32"]) & 0xFFFFFFFF
    if not st.get("complete") or int(st["bytes"]) != nbytes or their_crc != crc:
        raise StreamCorrupt(
            f"stream of {path!r} failed its check: host {nbytes} bytes crc32 {crc:08x}, "
            f"target {st.get('bytes')} bytes crc32 {int(st.get('crc32', 0)):08x} "
            f"complete={st.get('complete')}"
        )


async def _settled_size(t: object, path: str) -> int:
    """The target file's size once it has stopped changing (or the
    wait ran out: then the last size seen)."""
    last = -1
    waited = 0.0
    while True:
        try:
            st = await t.request("fs.stat", {"path": path}, timeout_s=30.0)  # type: ignore[attr-defined]
            size = int(st["size"])
        except Exception:
            size = -1
        if size == last:
            return max(size, 0)  # -1 twice: no file yet, start at 0
        if waited >= RESUME_SETTLE_MAX_S:
            return max(size, 0)
        last = size
        await asyncio.sleep(RESUME_SETTLE_S)
        waited += RESUME_SETTLE_S


async def stream_upload(
    fleet: Fleet,
    target: str,
    local_path: str | Path,
    remote_path: str,
    *,
    resume_from: int = 0,
    retries: int = 3,
) -> UploadStats:
    """Send a file as raw bytes after one fs.stream_put.

    The upload is confirmed by fs.stat on the stream connection itself
    (the daemon closes the file before it reads that request) and, when
    the daemon has fs.stream_status, by the CRC-32 of the stream.

    If the connection breaks, the stream is closed first, then the
    target file's size is read once it has stopped growing, and a new
    stream continues from there (up to `retries` times). While
    resuming, a refused put (the file still held open by the old
    stream, or its size moved on) is retried the same way. A CRC
    mismatch starts the file again from 0.
    """
    local = Path(local_path)
    total = local.stat().st_size
    t = fleet.mcpd(target)
    with_crc = await _stream_crc_supported(t)
    offset = resume_from
    attempts = 0
    crc_checked = False
    t0 = time.monotonic()
    while True:
        resuming = attempts > 0
        s = None
        try:
            s = await t.open_stream()
            await s.call("fs.stream_put", {
                "path": remote_path, "offset": offset,
                "length": total - offset,
            }, timeout_s=30.0)
            t.last_write_at = time.monotonic()
            crc = 0
            with local.open("rb") as fh:
                fh.seek(offset)
                while True:
                    blk = fh.read(STREAM_BLOCK)
                    if not blk:
                        break
                    crc = zlib.crc32(blk, crc)
                    await s.send_raw(blk, timeout_s=STREAM_BLOCK_TIMEOUT_S)
            st = await s.call("fs.stat", {"path": remote_path},
                              timeout_s=STREAM_BLOCK_TIMEOUT_S)
            t.last_write_at = time.monotonic()
            if int(st["size"]) != total:
                raise RuntimeError(
                    f"stream upload of {remote_path!r} ended at "
                    f"{st['size']} bytes, expected {total}"
                )
            if with_crc:
                await _check_crc(s, crc, total - offset, remote_path)
                crc_checked = True
            break
        except Exception as e:
            retryable = isinstance(e, (StreamBroken, StreamCorrupt)) or (
                # A resume refused because the old stream still holds
                # the file, or it grew after the size was read.
                resuming and isinstance(e, (TargetError, InvalidParams))
            )
            if not retryable or attempts >= retries:
                raise
            attempts += 1
            if s is not None:
                await s.close()
                s = None
            if isinstance(e, StreamCorrupt):
                offset = 0
            else:
                await asyncio.sleep(0.5 * attempts)
                offset = await _settled_size(t, remote_path)
                offset = min(offset, total)
        finally:
            if s is not None:
                await s.close()
    return UploadStats(
        path=remote_path,
        bytes_total=total,
        bytes_sent_compressed=total - resume_from,
        chunks=1 + attempts,
        compressed_chunks=0,
        elapsed_s=time.monotonic() - t0,
        crc_checked=crc_checked,
    )


async def stream_download(
    fleet: Fleet,
    target: str,
    remote_path: str,
    local_path: str | Path,
    *,
    resume_from: int = 0,
    retries: int = 3,
) -> DownloadStats:
    """Receive a file as raw bytes after one fs.stream_get. A broken
    connection is resumed from the local file's size on a new stream
    (up to `retries` times). With fs.stream_status each stream's CRC-32
    is checked; a mismatch drops what that stream wrote and fetches it
    again."""
    local = Path(local_path)
    local.parent.mkdir(parents=True, exist_ok=True)
    t = fleet.mcpd(target)
    with_crc = await _stream_crc_supported(t)
    offset = resume_from
    attempts = 0
    total = 0
    crc_checked = False
    t0 = time.monotonic()
    while True:
        s = None
        start = offset
        try:
            s = await t.open_stream()
            r = await s.call("fs.stream_get", {
                "path": remote_path, "offset": offset,
            }, timeout_s=30.0)
            n = int(r["length"])
            total = int(r["total_size"])
            crc = 0
            with local.open("ab" if offset > 0 else "wb") as fh:
                if offset > 0:
                    fh.truncate(offset)
                got = 0
                try:
                    while got < n:
                        data = await s.read_raw(min(STREAM_BLOCK, n - got),
                                                timeout_s=STREAM_BLOCK_TIMEOUT_S)
                        crc = zlib.crc32(data, crc)
                        fh.write(data)
                        got += len(data)
                finally:
                    offset += got
            if with_crc:
                await _check_crc(s, crc, n, remote_path)
                crc_checked = True
            break
        except Exception as e:
            if not isinstance(e, (StreamBroken, StreamCorrupt)) or attempts >= retries:
                raise
            attempts += 1
            if isinstance(e, StreamCorrupt):
                offset = start  # drop this stream's bytes, fetch them again
            await asyncio.sleep(0.5 * attempts)
        finally:
            if s is not None:
                await s.close()
    return DownloadStats(
        path=str(local),
        bytes_total=total,
        chunks=1 + attempts,
        elapsed_s=time.monotonic() - t0,
        crc_checked=crc_checked,
    )
