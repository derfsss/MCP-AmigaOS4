"""Per-(target, channel) host-side serial capture.

Each capture is a `SerialCapture`: opens a COM port via pyserial, runs
a background thread that reads bytes and appends them to a log file.
MCP tools start/stop captures and read back chunks of the log.

The capture writes raw bytes (no decode), so binary streams (e.g. an
MCU UART that emits non-ASCII status) round-trip cleanly. Tool callers
that want text decode it themselves.

Logs land under `<server.log_dir>/serial/<target>_<channel>.log`. They
are append-only by default; pass `truncate=True` to `start` to wipe
the previous run before opening.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import serial


@dataclass
class CaptureInfo:
    target: str
    channel: str
    port: str
    baud: int
    log_path: Path
    started_at: float
    running: bool
    total_bytes: int
    last_error: str | None = None


class SerialCapture:
    """Thread-backed serial port -> log file tailer."""

    def __init__(
        self, *,
        target: str,
        channel: str,
        port: str,
        baud: int,
        log_path: Path,
    ) -> None:
        self.target = target
        self.channel = channel
        self.port = port
        self.baud = baud
        self.log_path = log_path
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ser: serial.Serial | None = None
        self._lock = threading.Lock()
        self._total = 0
        self._started_at = 0.0
        self._error: str | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def total_bytes(self) -> int:
        return self._total

    @property
    def started_at(self) -> float:
        return self._started_at

    @property
    def last_error(self) -> str | None:
        return self._error

    def start(self, *, truncate: bool = False) -> None:
        if self.running:
            return
        self._stop.clear()
        self._error = None
        self._total = 0
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if truncate and self.log_path.exists():
            self.log_path.unlink()
        # Open the port up-front so config errors surface synchronously.
        self._ser = serial.Serial(
            port=self.port,
            baudrate=self.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.2,
            rtscts=False,
            dsrdtr=False,
            xonxoff=False,
        )
        self._ser.reset_input_buffer()
        self._started_at = time.time()
        self._thread = threading.Thread(
            target=self._run, name=f"serial-{self.target}-{self.channel}",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        ser = self._ser
        assert ser is not None
        try:
            with self.log_path.open("ab") as fh:
                while not self._stop.is_set():
                    try:
                        n = ser.in_waiting
                    except OSError as e:
                        self._error = f"in_waiting: {e}"
                        break
                    if n:
                        try:
                            data = ser.read(n)
                        except OSError as e:
                            self._error = f"read: {e}"
                            break
                        if data:
                            fh.write(data)
                            fh.flush()
                            with self._lock:
                                self._total += len(data)
                    else:
                        self._stop.wait(0.05)
        finally:
            try:
                ser.close()
            except Exception:
                pass
            self._ser = None

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)
        self._thread = None

    def info(self) -> CaptureInfo:
        return CaptureInfo(
            target=self.target,
            channel=self.channel,
            port=self.port,
            baud=self.baud,
            log_path=self.log_path,
            started_at=self._started_at,
            running=self.running,
            total_bytes=self._total,
            last_error=self._error,
        )

    def read_at(self, offset: int, max_bytes: int) -> bytes:
        if not self.log_path.exists():
            return b""
        with self.log_path.open("rb") as fh:
            fh.seek(max(0, offset))
            return fh.read(max(0, max_bytes))

    def file_size(self) -> int:
        try:
            return self.log_path.stat().st_size
        except FileNotFoundError:
            return 0

    def clear_log(self) -> None:
        if self.running:
            raise RuntimeError("cannot clear log while capture is running")
        if self.log_path.exists():
            self.log_path.unlink()
        self._total = 0


@dataclass
class SerialCaptureRegistry:
    """Process-wide registry of active SerialCapture instances."""
    log_root: Path
    _captures: dict[tuple[str, str], SerialCapture] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def log_path_for(self, target: str, channel: str) -> Path:
        return self.log_root / "serial" / f"{target}_{channel}.log"

    def get(self, target: str, channel: str) -> SerialCapture | None:
        with self._lock:
            return self._captures.get((target, channel))

    def get_or_create(
        self, *, target: str, channel: str, port: str, baud: int,
    ) -> SerialCapture:
        with self._lock:
            key = (target, channel)
            cap = self._captures.get(key)
            if cap is None:
                cap = SerialCapture(
                    target=target, channel=channel,
                    port=port, baud=baud,
                    log_path=self.log_path_for(target, channel),
                )
                self._captures[key] = cap
            else:
                # Refresh port/baud if config changed between calls.
                cap.port = port
                cap.baud = baud
            return cap

    def remove(self, target: str, channel: str) -> None:
        with self._lock:
            self._captures.pop((target, channel), None)

    def list_active(self) -> list[CaptureInfo]:
        with self._lock:
            return [c.info() for c in self._captures.values()]

    def stop_all(self) -> None:
        with self._lock:
            caps = list(self._captures.values())
        for c in caps:
            try:
                c.stop()
            except Exception:
                pass
