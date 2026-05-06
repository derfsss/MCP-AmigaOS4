"""fs.upload / fs.download tests with a fake MCPd transport.

The fake transport buffers writes by offset (mimicking the daemon's
`fs.write_chunk` behaviour) and serves them back on `fs.read`,
giving a round-trippable in-memory filesystem the wrappers can
upload to and download from.

Verifies:
- single-chunk file uploads + downloads (size < chunk_size)
- multi-chunk file uploads + downloads (size > chunk_size)
- auto-zlib kicks in on compressible payload, skips on already-
  compressed payload
- resume=True picks up from the on-target / on-disk size
- verify=True succeeds on integrity match, raises on mismatch
"""

from __future__ import annotations

import base64
import hashlib
import zlib
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


class FakeMcpd:
    """In-memory file store keyed by path. Implements the daemon
    surface our wrappers use: fs.write_chunk, fs.read, fs.stat,
    fs.hash."""

    def __init__(self) -> None:
        self.files: dict[str, bytearray] = {}
        self.calls: list[tuple[str, dict | None]] = []
        # Optional: corrupt a single file's hash to test verify
        self.fake_hash_for: dict[str, str] = {}

    async def request(self, method: str, params: dict | None = None,
                      timeout_s: float = 30.0) -> Any:
        self.calls.append((method, params))
        assert params is not None
        if method == "fs.write_chunk":
            path = params["path"]
            offset = int(params["offset"])
            data = base64.b64decode(params["content_b64"])
            comp = params.get("compression", "none")
            if comp == "zlib":
                data = zlib.decompress(data)
            buf = self.files.setdefault(path, bytearray())
            # First-chunk semantics: total_size hint pre-extends; we
            # also start from scratch on the first chunk if offset 0.
            if offset == 0 and "total_size" in params:
                # Pre-extend with zeros up to total_size, then we'll
                # overwrite as chunks arrive.
                total = int(params["total_size"])
                if len(buf) < total:
                    buf.extend(b"\x00" * (total - len(buf)))
            # Overwrite-at-offset
            end = offset + len(data)
            if end > len(buf):
                buf.extend(b"\x00" * (end - len(buf)))
            buf[offset:end] = data
            return {
                "path": path,
                "offset": offset,
                "written": len(data),
                "total_so_far": len(buf),
            }
        if method == "fs.read":
            path = params["path"]
            buf = bytes(self.files[path])
            offset = int(params.get("offset") or 0)
            length = params.get("length")
            length_int: int = (
                int(length) if length is not None else len(buf) - offset
            )
            slc = buf[offset:offset + length_int]
            return {
                "path": path,
                "size": len(slc),
                "content_b64": base64.b64encode(slc).decode("ascii"),
                "total_size": len(buf),
                "offset": offset,
            }
        if method == "fs.stat":
            path = params["path"]
            if path not in self.files:
                from amiga_fleet_mcp.errors import TargetError
                raise TargetError(f"path not found: {path!r}",
                                  data={"path": path})
            return {"name": path, "type": "file",
                    "size": len(self.files[path])}
        if method == "fs.hash":
            path = params["path"]
            if path in self.fake_hash_for:
                h = self.fake_hash_for[path]
            else:
                h = hashlib.sha256(bytes(self.files[path])).hexdigest()
            return {"path": path, "algo": "sha256", "hash": h,
                    "size": len(self.files[path])}
        raise NotImplementedError(method)


@pytest.fixture
def fleet_with_fake() -> tuple[Fleet, FakeMcpd]:
    cfg = Config(targets={
        "x5000": TargetConfig(
            type="remote",
            channels=TargetChannels(
                mcpd=McpdChannel(endpoint="192.168.0.10:4322"),
            ),
        ),
    })
    fleet = Fleet(cfg)
    fake = FakeMcpd()
    fleet._mcpd["x5000"] = fake  # type: ignore[assignment]
    return fleet, fake


# ---- upload -------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_small_file_single_chunk(
    fleet_with_fake: tuple[Fleet, FakeMcpd], tmp_path: Path,
) -> None:
    fleet, fake = fleet_with_fake
    src = tmp_path / "small.bin"
    src.write_bytes(b"hello world\n" * 100)
    res = await fs_tool.fs_upload(
        fleet, "x5000",
        local_path=str(src),
        remote_path="RAM:small.bin",
    )
    assert res.bytes_total == src.stat().st_size
    assert res.chunks == 1
    assert fake.files["RAM:small.bin"] == src.read_bytes()


@pytest.mark.asyncio
async def test_upload_large_file_multi_chunk(
    fleet_with_fake: tuple[Fleet, FakeMcpd], tmp_path: Path,
) -> None:
    fleet, fake = fleet_with_fake
    src = tmp_path / "large.bin"
    # 5 MiB of pseudo-random-but-compressible data, chunked at 2 MiB
    src.write_bytes(b"abcd" * (5 * 1024 * 1024 // 4))
    res = await fs_tool.fs_upload(
        fleet, "x5000",
        local_path=str(src),
        remote_path="RAM:large.bin",
        chunk_size=2 * 1024 * 1024,
    )
    assert res.bytes_total == src.stat().st_size
    assert res.chunks >= 3  # 5 MiB / 2 MiB = 3 chunks
    assert fake.files["RAM:large.bin"] == src.read_bytes()


@pytest.mark.asyncio
async def test_upload_auto_zlib_compresses_compressible(
    fleet_with_fake: tuple[Fleet, FakeMcpd], tmp_path: Path,
) -> None:
    fleet, fake = fleet_with_fake
    src = tmp_path / "zeros.bin"
    src.write_bytes(b"\x00" * (1024 * 1024))   # super-compressible
    res = await fs_tool.fs_upload(
        fleet, "x5000",
        local_path=str(src),
        remote_path="RAM:zeros.bin",
    )
    assert res.compressed_chunks == 1
    assert res.compression_ratio < 0.05    # >95% saving on zeros
    assert fake.files["RAM:zeros.bin"] == src.read_bytes()


@pytest.mark.asyncio
async def test_upload_auto_zlib_skips_incompressible(
    fleet_with_fake: tuple[Fleet, FakeMcpd], tmp_path: Path,
) -> None:
    fleet, fake = fleet_with_fake
    import os
    src = tmp_path / "rand.bin"
    src.write_bytes(os.urandom(256 * 1024))
    res = await fs_tool.fs_upload(
        fleet, "x5000",
        local_path=str(src),
        remote_path="RAM:rand.bin",
    )
    assert res.compressed_chunks == 0   # auto-mode skipped zlib
    assert fake.files["RAM:rand.bin"] == src.read_bytes()


@pytest.mark.asyncio
async def test_upload_resume_skips_existing(
    fleet_with_fake: tuple[Fleet, FakeMcpd], tmp_path: Path,
) -> None:
    fleet, fake = fleet_with_fake
    src = tmp_path / "resume.bin"
    src.write_bytes(b"X" * (4 * 1024 * 1024))
    # Pre-populate the remote with the full file ("simulates a
    # completed upload" -- resume should short-circuit)
    fake.files["RAM:resume.bin"] = bytearray(src.read_bytes())
    fake.calls.clear()
    res = await fs_tool.fs_upload(
        fleet, "x5000",
        local_path=str(src),
        remote_path="RAM:resume.bin",
        resume=True,
    )
    assert res.resumed_from == src.stat().st_size
    assert res.chunks == 0
    # Only the fs.stat call should have been made
    methods = [m for m, _ in fake.calls]
    assert methods == ["fs.stat"]


@pytest.mark.asyncio
async def test_upload_verify_succeeds_on_match(
    fleet_with_fake: tuple[Fleet, FakeMcpd], tmp_path: Path,
) -> None:
    fleet, _ = fleet_with_fake
    src = tmp_path / "ok.bin"
    src.write_bytes(b"verify me\n" * 1000)
    res = await fs_tool.fs_upload(
        fleet, "x5000",
        local_path=str(src),
        remote_path="RAM:ok.bin",
        verify=True,
    )
    assert res.sha256_verified is True
    expected = hashlib.sha256(src.read_bytes()).hexdigest()
    assert res.sha256 == expected


@pytest.mark.asyncio
async def test_upload_verify_raises_on_mismatch(
    fleet_with_fake: tuple[Fleet, FakeMcpd], tmp_path: Path,
) -> None:
    fleet, fake = fleet_with_fake
    src = tmp_path / "tamper.bin"
    src.write_bytes(b"original")
    fake.fake_hash_for["RAM:tamper.bin"] = "0" * 64
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        await fs_tool.fs_upload(
            fleet, "x5000",
            local_path=str(src),
            remote_path="RAM:tamper.bin",
            verify=True,
        )


@pytest.mark.asyncio
async def test_upload_missing_local_file_raises(
    fleet_with_fake: tuple[Fleet, FakeMcpd], tmp_path: Path,
) -> None:
    fleet, _ = fleet_with_fake
    with pytest.raises(FileNotFoundError):
        await fs_tool.fs_upload(
            fleet, "x5000",
            local_path=str(tmp_path / "does-not-exist.bin"),
            remote_path="RAM:nope.bin",
        )


# ---- download -----------------------------------------------------


@pytest.mark.asyncio
async def test_download_single_chunk(
    fleet_with_fake: tuple[Fleet, FakeMcpd], tmp_path: Path,
) -> None:
    fleet, fake = fleet_with_fake
    fake.files["RAM:src.bin"] = bytearray(b"hello\n" * 1000)
    dst = tmp_path / "out.bin"
    res = await fs_tool.fs_download(
        fleet, "x5000",
        remote_path="RAM:src.bin",
        local_path=str(dst),
    )
    assert dst.read_bytes() == bytes(fake.files["RAM:src.bin"])
    assert res.chunks == 1
    assert res.bytes_total == len(fake.files["RAM:src.bin"])


@pytest.mark.asyncio
async def test_download_multi_chunk(
    fleet_with_fake: tuple[Fleet, FakeMcpd], tmp_path: Path,
) -> None:
    fleet, fake = fleet_with_fake
    payload = b"X" * (5 * 1024 * 1024)
    fake.files["RAM:big.bin"] = bytearray(payload)
    dst = tmp_path / "out.bin"
    res = await fs_tool.fs_download(
        fleet, "x5000",
        remote_path="RAM:big.bin",
        local_path=str(dst),
        chunk_size=2 * 1024 * 1024,
    )
    assert dst.read_bytes() == payload
    assert res.chunks >= 3
    assert res.bytes_total == len(payload)


@pytest.mark.asyncio
async def test_download_resume_appends(
    fleet_with_fake: tuple[Fleet, FakeMcpd], tmp_path: Path,
) -> None:
    fleet, fake = fleet_with_fake
    payload = b"abcdefghij" * 100  # 1000 bytes
    fake.files["RAM:src.bin"] = bytearray(payload)
    dst = tmp_path / "partial.bin"
    # Pretend we already have the first 400 bytes locally
    dst.write_bytes(payload[:400])
    res = await fs_tool.fs_download(
        fleet, "x5000",
        remote_path="RAM:src.bin",
        local_path=str(dst),
        resume=True,
    )
    assert dst.read_bytes() == payload
    assert res.resumed_from == 400


@pytest.mark.asyncio
async def test_download_verify_succeeds_on_match(
    fleet_with_fake: tuple[Fleet, FakeMcpd], tmp_path: Path,
) -> None:
    fleet, fake = fleet_with_fake
    payload = b"verify-me\n" * 200
    fake.files["RAM:src.bin"] = bytearray(payload)
    dst = tmp_path / "out.bin"
    res = await fs_tool.fs_download(
        fleet, "x5000",
        remote_path="RAM:src.bin",
        local_path=str(dst),
        verify=True,
    )
    assert res.sha256_verified is True
    assert res.sha256 == hashlib.sha256(payload).hexdigest()


@pytest.mark.asyncio
async def test_download_verify_raises_on_mismatch(
    fleet_with_fake: tuple[Fleet, FakeMcpd], tmp_path: Path,
) -> None:
    fleet, fake = fleet_with_fake
    fake.files["RAM:src.bin"] = bytearray(b"real bytes")
    fake.fake_hash_for["RAM:src.bin"] = "0" * 64
    dst = tmp_path / "out.bin"
    with pytest.raises(RuntimeError, match="SHA-256 mismatch"):
        await fs_tool.fs_download(
            fleet, "x5000",
            remote_path="RAM:src.bin",
            local_path=str(dst),
            verify=True,
        )


# ---- binary-clean round-trip --------------------------------------
#
# These tests prove the upload/download path is binary-clean -- it
# transparently base64-encodes on the way out and base64-decodes on
# the way in, regardless of the bytes in the payload. NUL bytes,
# 0xFF, UTF-8 sequences, control characters, every byte 0..255 must
# round-trip without modification.


@pytest.mark.asyncio
async def test_round_trip_all_byte_values(
    fleet_with_fake: tuple[Fleet, FakeMcpd], tmp_path: Path,
) -> None:
    """Upload bytes(range(256)) repeated, download it back, byte-
    compare. Catches any text-decode / latin-1 / NUL-truncate bug
    in the auto-b64 path."""
    fleet, _fake = fleet_with_fake
    src = tmp_path / "all_bytes.bin"
    payload = bytes(range(256)) * 8 * 1024     # 2 MiB of every byte
    src.write_bytes(payload)

    up = await fs_tool.fs_upload(
        fleet, "x5000",
        local_path=str(src),
        remote_path="RAM:all_bytes.bin",
    )
    assert up.bytes_total == len(payload)

    dst = tmp_path / "all_bytes_back.bin"
    down = await fs_tool.fs_download(
        fleet, "x5000",
        remote_path="RAM:all_bytes.bin",
        local_path=str(dst),
    )
    assert down.bytes_total == len(payload)
    assert dst.read_bytes() == payload      # byte-exact


@pytest.mark.asyncio
async def test_round_trip_with_zlib_and_verify(
    fleet_with_fake: tuple[Fleet, FakeMcpd], tmp_path: Path,
) -> None:
    """Same round-trip with auto-zlib + sha256 verify on both ends."""
    fleet, _fake = fleet_with_fake
    src = tmp_path / "mixed.bin"
    # Mix compressible + incompressible regions
    import os
    payload = (b"\x00" * 512 * 1024) + os.urandom(512 * 1024) + (b"A" * 1024 * 1024)
    src.write_bytes(payload)

    await fs_tool.fs_upload(
        fleet, "x5000",
        local_path=str(src),
        remote_path="RAM:mixed.bin",
        chunk_size=512 * 1024,            # forces multi-chunk
        compression="auto",
        verify=True,
    )

    dst = tmp_path / "mixed_back.bin"
    down = await fs_tool.fs_download(
        fleet, "x5000",
        remote_path="RAM:mixed.bin",
        local_path=str(dst),
        chunk_size=512 * 1024,
        verify=True,
    )
    assert dst.read_bytes() == payload
    assert down.sha256_verified is True
