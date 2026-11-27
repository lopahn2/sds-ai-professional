"""Tail bounded JSON records from a local observation file, including rotation."""
from __future__ import annotations

import json
import logging
import os
import stat
import threading
from pathlib import Path


_MAX_LINE = 32 * 1024
_MAX_LINES = 100
log = logging.getLogger(__name__)


class ObservationLog:
    def __init__(self, path: Path, source: str):
        self.path, self.source = path, source
        self.status = {"state": "starting"}
        self._file = None
        self._skip_until_newline = False
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name=self.source, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        elif self._file is not None:
            self._file.close()

    def _open(self):
        fd = os.open(self.path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise OSError("observation source must be a regular file")
        return os.fdopen(fd, "rb")

    def _poll(self, consume) -> int:
        if self._file is None:
            self._file = self._open()
            end = self._file.seek(0, os.SEEK_END)
            if end:
                self._file.seek(-1, os.SEEK_END)
                self._skip_until_newline = self._file.read(1) != b"\n"
        emitted = 0
        for _ in range(_MAX_LINES):
            if self._stop.is_set():
                break
            pos = self._file.tell()
            line = self._file.readline(_MAX_LINE + 1)
            partial = bool(line) and not self._skip_until_newline and len(line) <= _MAX_LINE and not line.endswith(b"\n")
            if not line or partial:
                # Drain the old inode before following the replacement.
                current = self.path.stat()
                opened = os.fstat(self._file.fileno())
                if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                    new = self._open()
                    self._file.close()
                    self._file = new
                    self._skip_until_newline = False
                    continue
                if current.st_size < pos:
                    self._file.seek(0)
                    self._skip_until_newline = False
                    continue
                if partial:
                    self._file.seek(pos)
                break
            if self._skip_until_newline or len(line) > _MAX_LINE:
                self._skip_until_newline = not line.endswith(b"\n")
                continue
            try:
                event = json.loads(line)
            except (ValueError, RecursionError):
                continue
            try:
                emitted += bool(consume(event))
            except Exception:
                self._file.seek(pos)
                raise
        return emitted

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    self.poll_once()
                except Exception as exc:
                    if self.status.get("state") != "degraded":
                        log.warning("%s observation unavailable (%s)", self.source, type(exc).__name__)
                    self.status = {"state": "degraded", "error": type(exc).__name__}
                self._stop.wait(1)
        finally:
            if self._file is not None:
                self._file.close()
