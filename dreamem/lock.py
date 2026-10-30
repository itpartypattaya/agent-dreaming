"""One process at a time over ~/.dreaming, on Windows, macOS and Linux alike.

An OS lock on a file (``msvcrt.locking`` on Windows, ``fcntl.flock`` elsewhere): the operating system releases it when
its holder exits or is killed, so there is nothing stale to take over and no age after which a live holder loses it
(the engine's original ``fcntl``-only lock did nothing on Windows; a lock directory with a stale-takeover rule let two
takers in at once — Codex review of 0.2.0). The file keeps the holder's pid and start time, for a person reading it.
"""
from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class Busy(Exception):
    """Another process holds the lock."""


def _try_lock(fd: int) -> bool:
    try:
        if sys.platform == "win32":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fd: int) -> None:
    try:
        if sys.platform == "win32":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


class Lock:
    def __init__(self, path: Path, *, wait: float = 0.0):
        self.path = Path(path)
        self.wait = wait
        self.fd: int | None = None

    @property
    def held(self) -> bool:
        return self.fd is not None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + self.wait
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                os.close(fd)
                return False
            time.sleep(0.1)
        try:  # who holds it, for a person reading the file; byte 0 is the locked one, the text follows it
            stamp = json.dumps({"pid": os.getpid(), "host": socket.gethostname(), "started": time.time()})
            os.lseek(fd, 1, os.SEEK_SET)
            os.write(fd, stamp.encode("utf-8"))
            os.ftruncate(fd, 1 + len(stamp))
        except OSError:
            pass
        self.fd = fd
        return True

    def release(self) -> None:
        if self.fd is None:
            return
        _unlock(self.fd)
        os.close(self.fd)
        self.fd = None

    def __enter__(self) -> Lock:
        if not self.acquire():
            raise Busy(str(self.path))
        return self

    def __exit__(self, *exc) -> None:
        self.release()
