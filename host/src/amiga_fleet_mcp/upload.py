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

import base64
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .fleet import Fleet
from .tools import fs as fs_tool

# Conservative chunk default: 24 MiB raw bytes.
#   - base64 inflates by 4/3, so 24 MiB raw -> 32 MiB encoded
#   - leaves ~0 MiB headroom for the JSON envelope (path, offsets etc.)
#   - safer in practice if compression='auto' falls back to raw
DEFAULT_CHUNK_RAW = 24 * 1024 * 1024


@dataclass
class UploadStats:
    path: str
    bytes_total: int
    bytes_sent_compressed: int  # what actually went over the wire (pre-b64)
    chunks: int
    compressed_chunks: int
    elapsed_s: float

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
                        # Brief backoff
                        time.sleep(0.5 * (attempt + 1))
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
                        time.sleep(0.5 * (attempt + 1))
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
