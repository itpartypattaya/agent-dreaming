"""One process at a time over ~/.dreaming, on Windows, macOS and Linux alike.

The engine's own ``_sweep_lock`` is ``fcntl.flock`` and does nothing on Windows, where two sessions opening at once would
both start the lazy night. This lock is a directory: ``os.mkdir`` is atomic on NTFS, APFS and ext4. Inside it,
``owner.json`` names the holder (pid, host, start time). A lock whose holder is gone — a dead pid on this host, or older
than ``stale_after`` seconds — is taken over: a pass killed half way must not lock the night forever.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import time
import uuid
from pathlib import Path

STALE_AFTER = 30 * 60  # a pass takes seconds; half an hour is a dead one


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # Never os.kill(pid, 0) here: on Windows it TERMINATES the process.
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # access denied: it exists, it is someone else's
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Busy(Exception):
    """Another process holds the lock."""


class Lock:
    def __init__(self, path: Path, *, wait: float = 0.0, stale_after: float = STALE_AFTER):
        self.path = Path(path)
        self.wait = wait
        self.stale_after = stale_after
        self.token = uuid.uuid4().hex
        self.held = False

    def _owner(self) -> dict | None:
        try:
            return json.loads((self.path / "owner.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _stale(self) -> bool:
        owner = self._owner()
        if owner is None:
            # Created but not yet stamped (the holder is between mkdir and its write), or stamped by a process killed
            # in that instant: judged by the directory's age.
            try:
                return time.time() - self.path.stat().st_mtime > 10
            except OSError:
                return False
        if time.time() - float(owner.get("started") or 0) > self.stale_after:
            return True
        return owner.get("host") == socket.gethostname() and not pid_alive(int(owner.get("pid") or 0))

    def _take_over(self) -> None:
        aside = self.path.with_name(f"{self.path.name}.stale-{uuid.uuid4().hex[:8]}")
        try:
            os.replace(self.path, aside)  # one taker wins the rename; the others find the path gone or fresh
        except OSError:
            return
        shutil.rmtree(aside, ignore_errors=True)

    def acquire(self) -> bool:
        deadline = time.monotonic() + self.wait
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                os.mkdir(self.path)
            except FileExistsError:
                if self._stale():
                    self._take_over()
                    continue
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.1)
                continue
            owner = {"pid": os.getpid(), "host": socket.gethostname(), "started": time.time(), "token": self.token}
            (self.path / "owner.json").write_text(json.dumps(owner), encoding="utf-8")
            self.held = True
            return True

    def release(self) -> None:
        if not self.held:
            return
        owner = self._owner()
        if owner is None or owner.get("token") == self.token:  # never remove a lock someone took over from us
            shutil.rmtree(self.path, ignore_errors=True)
        self.held = False

    def __enter__(self) -> Lock:
        if not self.acquire():
            raise Busy(str(self.path))
        return self

    def __exit__(self, *exc) -> None:
        self.release()
