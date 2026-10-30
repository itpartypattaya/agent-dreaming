"""JSON lines as the CLIs write them, read the way a file being written must be read."""
from __future__ import annotations

import json
from collections.abc import Iterator


def decode(raw: bytes) -> str:
    """A line in UTF-8 (a BOM dropped); a line in another encoding (cp1251 pasted from an old tool) is read as
    cp1251, and as UTF-8 with replacements when even that fails."""
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return raw.decode("cp1251")
        except UnicodeDecodeError:
            return raw.decode("utf-8", errors="replace")


def records(data: bytes, keep=None) -> Iterator[tuple[dict | None, bool]]:
    """(object, is_last) per non-empty line; object None for a line that is not a JSON object. The last line of a
    file being written may be cut off: the caller forgives it there and nowhere else. *keep*: a test on the raw bytes
    of a line; a line it refuses is not parsed at all (most of a transcript is tool output nobody needs here)."""
    lines = data.split(b"\n")
    if lines and not lines[-1].strip():
        lines.pop()
        last_complete = True
    else:
        last_complete = False
    for i, raw in enumerate(lines):
        if not raw.strip() or (keep is not None and not keep(raw)):
            continue
        is_last = i == len(lines) - 1 and not last_complete
        try:
            obj = json.loads(decode(raw))
        except ValueError:
            yield None, is_last
            continue
        yield (obj if isinstance(obj, dict) else None), is_last
