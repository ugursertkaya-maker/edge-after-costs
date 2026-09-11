"""Live market-data recorder.

Maker-first execution cannot be evaluated on 1-minute OHLCV: a resting order
lives or dies on queue position, trade-through and adverse selection, none of
which exist in a bar.  So we record the raw stream — every book update, every
print, every quote change, with sequence numbers and receive timestamps — and
replay it later against a real queue model.

Design constraints, in the order that mattered:

* **It must never slow the trading loop.**  Recording happens on the hot path,
  so it does nothing but append bytes to a buffer; the flush is batched and the
  file handle is gzip-buffered.  A recorder failure is logged once and then
  disables itself rather than taking the worker down with it.
* **It must not fill the disk.**  The VPS has ~50 GB free and runs twenty other
  containers.  Files roll hourly, old ones are deleted past the retention
  window, and a disk-space guard stops recording rather than wedging the box.
* **What is written must be replayable exactly.**  Each line carries the
  receive time (``r``) separately from the exchange time (``E``), because the
  gap between them *is* the latency a maker order actually faces.
"""

from __future__ import annotations

import gzip
import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("cryptobot.recorder")

# Roll a file every hour: small enough to copy or delete individually, large
# enough that the gzip dictionary is worth having.
ROLL_SECONDS = 3600
FLUSH_EVERY = 200          # messages
FLUSH_SECONDS = 5.0
# gzip buffers internally, so bytes written are not bytes on disk. Sync the
# stream periodically: a reader can then tail a live file, and a crash costs
# seconds of tape instead of minutes. Syncing on every write would cost ~2x the
# file size in flush markers, which is why this is time-based.
SYNC_SECONDS = 30.0
MIN_FREE_BYTES = 3 * 1024**3   # stop recording below 3 GB free


@dataclass
class RecorderStats:
    messages: int = 0
    bytes_written: int = 0
    files: int = 0
    dropped: int = 0
    disabled_reason: str | None = None
    started_ms: int = 0
    last_write_ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "messages": self.messages,
            "bytes_written": self.bytes_written,
            "mb_written": round(self.bytes_written / 1e6, 2),
            "files": self.files,
            "dropped": self.dropped,
            "enabled": self.disabled_reason is None,
            "disabled_reason": self.disabled_reason,
            "started_ms": self.started_ms,
            "last_write_ms": self.last_write_ms,
        }


class TapeRecorder:
    """Appends raw stream messages to hourly gzip NDJSON files.

    One file per hour per process: ``tape-YYYYMMDD-HH.ndjson.gz``.  Every line is
    ``{"r": <receive_ms>, "m": <raw message>}`` — the raw message is stored
    untouched so a replay is not limited to whatever fields today's code happens
    to care about.
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        enabled: bool = True,
        retention_hours: int = 168,
        roll_seconds: int = ROLL_SECONDS,
    ) -> None:
        self.dir = Path(directory)
        self.enabled = enabled
        self.retention_hours = retention_hours
        self.roll_seconds = max(60, roll_seconds)
        self.stats = RecorderStats(started_ms=int(time.time() * 1000))
        self._fh: gzip.GzipFile | None = None
        self._current_key: str | None = None
        self._buffer: list[str] = []
        self._last_flush = time.time()
        self._last_sync = time.time()
        self._last_sweep = 0.0

        if self.enabled:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                self._disable(f"cannot create {self.dir}: {exc}")

    # ------------------------------------------------------------------ #

    def _disable(self, reason: str) -> None:
        if self.stats.disabled_reason is None:
            log.warning("recorder disabled: %s", reason)
        self.stats.disabled_reason = reason
        self.enabled = False
        self._close()

    def _file_key(self, ts_ms: int) -> str:
        return time.strftime("%Y%m%d-%H", time.gmtime(ts_ms / 1000.0))

    def _open(self, key: str) -> None:
        self._close()
        path = self.dir / f"tape-{key}.ndjson.gz"
        # Append, so a restart inside the same hour extends the file instead of
        # truncating an hour of history.
        self._fh = gzip.open(path, "at", compresslevel=6)
        self._current_key = key
        self.stats.files += 1

    def _close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except Exception:
                pass
        self._fh = None
        self._current_key = None

    # ------------------------------------------------------------------ #

    def record(self, message: Any, ts_ms: int) -> None:
        """Hot path. Serialise and buffer; never raise, never block."""
        if not self.enabled:
            return
        try:
            line = json.dumps({"r": int(ts_ms), "m": message}, separators=(",", ":"))
        except (TypeError, ValueError):
            self.stats.dropped += 1
            return

        self._buffer.append(line)
        self.stats.messages += 1
        now = time.time()
        if len(self._buffer) >= FLUSH_EVERY or (now - self._last_flush) >= FLUSH_SECONDS:
            self.flush(ts_ms)

    def flush(self, ts_ms: int | None = None) -> None:
        if not self.enabled or not self._buffer:
            self._last_flush = time.time()
            return
        ts_ms = ts_ms or int(time.time() * 1000)
        key = self._file_key(ts_ms)
        try:
            if key != self._current_key:
                self._open(key)
                self._sweep(force=True)
            payload = "\n".join(self._buffer) + "\n"
            assert self._fh is not None
            self._fh.write(payload)
            self.stats.bytes_written += len(payload)
            self.stats.last_write_ms = ts_ms
            now_wall = time.time()
            if (now_wall - self._last_sync) >= SYNC_SECONDS:
                self._fh.flush()
                self._last_sync = now_wall
        except Exception as exc:
            self._disable(f"write failed: {exc}")
            return
        finally:
            self._buffer.clear()
            self._last_flush = time.time()

        self._sweep()

    # ------------------------------------------------------------------ #

    def _sweep(self, *, force: bool = False) -> None:
        """Delete expired files and stop recording if the disk is filling."""
        now = time.time()
        if not force and (now - self._last_sweep) < 600:
            return
        self._last_sweep = now

        try:
            free = shutil.disk_usage(self.dir).free
        except Exception:
            free = None
        if free is not None and free < MIN_FREE_BYTES:
            self._disable(f"only {free / 1e9:.1f} GB free — refusing to fill the disk")
            return

        cutoff = now - self.retention_hours * 3600
        try:
            for path in self.dir.glob("tape-*.ndjson.gz"):
                if path.stat().st_mtime < cutoff and path.name != (
                    f"tape-{self._current_key}.ndjson.gz"
                ):
                    path.unlink(missing_ok=True)
        except Exception as exc:
            log.debug("recorder sweep failed: %s", exc)

    def close(self) -> None:
        self.flush()
        self._close()

    # ------------------------------------------------------------------ #

    def inventory(self) -> dict[str, Any]:
        """What is on disk — the dashboard shows this as research progress."""
        try:
            files = sorted(self.dir.glob("tape-*.ndjson.gz"))
        except Exception:
            return {"files": 0, "mb": 0.0, "hours": 0.0, "oldest": None, "newest": None}
        total = sum(f.stat().st_size for f in files)
        return {
            "files": len(files),
            "mb": round(total / 1e6, 1),
            "hours": round(len(files), 1),
            "oldest": files[0].name if files else None,
            "newest": files[-1].name if files else None,
        }


def replay(directory: str | Path, *, start_ms: int = 0, end_ms: int = 0):
    """Yield ``(receive_ms, message)`` in recorded order.

    Kept next to the writer on purpose: a recorder whose format drifts from its
    reader produces a dataset nobody can use.
    """
    d = Path(directory)
    for path in sorted(d.glob("tape-*.ndjson.gz")):
        try:
            with gzip.open(path, "rt") as fh:
                # The newest file may still be open in a writer, so its final
                # gzip member is unterminated and the reader hits EOF partway.
                # That is expected, not corruption: read what is there and stop.
                while True:
                    try:
                        line = fh.readline()
                    except EOFError:
                        break
                    if not line:
                        break
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    r = int(row.get("r", 0))
                    if start_ms and r < start_ms:
                        continue
                    if end_ms and r > end_ms:
                        return
                    yield r, row.get("m")
        except (OSError, EOFError) as exc:
            log.warning("replay skipped %s: %s", path.name, exc)
