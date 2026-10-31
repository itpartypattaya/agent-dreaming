"""Surrogate integer ids for facts of a system whose ids are strings or none at all.

``profiles/<p>/state/ids.json`` maps a key — the system's own id, or a digest of where the fact lives and its text —
to an int that stays the fact's id from night to night. No text is kept: a digest, never the words.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from ..fsutil import read_json, write_json

FILE = "ids.json"


def digest(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()[:32]


class Ids:
    def __init__(self, state_dir: Path | None):
        self.path = Path(state_dir) / FILE if state_dir else None
        data = read_json(self.path, {}) if self.path else {}
        data = data if isinstance(data, dict) else {}
        ids = data.get("ids")
        self.ids: dict[str, int] = {k: v for k, v in (ids or {}).items() if isinstance(v, int)} \
            if isinstance(ids, dict) else {}
        nxt = data.get("next")
        self.next = max(nxt if isinstance(nxt, int) else 1, max(self.ids.values(), default=0) + 1)
        self.changed = False

    def of(self, key: str) -> int:
        if key not in self.ids:
            self.ids[key] = self.next
            self.next += 1
            self.changed = True
        return self.ids[key]

    def known(self) -> dict[str, int]:
        return dict(self.ids)

    def save(self) -> None:
        if self.path is not None and self.changed:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_json(self.path, {"next": self.next, "ids": self.ids})
            self.changed = False
