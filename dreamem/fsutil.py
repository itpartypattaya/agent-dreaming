"""Writing our own files: atomic, private (0600 on POSIX; on Windows the user's profile ACL), never outside ~/.dreaming.
A file that is written goes to ``<name>.partial`` first and replaces the old one in one step: a pass killed half way
leaves a ``.partial`` that the next pass removes, never half a report."""
from __future__ import annotations

import json
import os
from pathlib import Path

PARTIAL = ".partial"


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + PARTIAL)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_json(path: Path, data) -> None:
    write_private(path, json.dumps(data, ensure_ascii=False, indent=1) + "\n")


def read_json(path: Path, default=None):
    """The JSON of *path*, or *default* when it is missing. A broken file is set aside as ``<name>.corrupt-<n>`` and
    *default* returned: one bad write must not stop every later pass."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    try:
        return json.loads(text)
    except ValueError:
        n = 1
        while path.with_name(f"{path.name}.corrupt-{n}").exists():
            n += 1
        try:
            os.replace(path, path.with_name(f"{path.name}.corrupt-{n}"))
        except OSError:
            pass
        return default


def remove_partials(folder: Path) -> list[Path]:
    """Remove what a killed pass left (``*.partial``, and the SQLite sidecars of a shadow database being built)."""
    gone = []
    if not folder.is_dir():
        return gone
    for path in folder.rglob(f"*{PARTIAL}*"):
        if path.is_file():
            try:
                path.unlink()
                gone.append(path)
            except OSError:
                pass
    return gone
