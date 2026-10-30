"""JSON lines as the CLIs write them, read the way a file being written must be read: line by line (a transcript can
be hundreds of megabytes), forgiving only a cut last line."""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator


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


def records(lines: Iterable[bytes], keep=None) -> Iterator[tuple[dict | None, bool]]:
    """(object, is_last) per non-empty line of *lines* (an open binary file, or any iterable of byte lines that keep
    their newline); object None for a line that is not a JSON object. `is_last` is True for a final line without its
    newline — a file being written may be cut there, and the caller forgives it there and nowhere else. *keep*: a
    test on the raw bytes of a line; a line it refuses is not parsed at all (most of a transcript is tool output)."""
    held = None
    for raw in lines:
        if held is not None:
            yield from _parse(held, False, keep)
        held = raw
    if held is not None:
        yield from _parse(held, not held.endswith(b"\n"), keep)


def _parse(raw: bytes, is_last: bool, keep) -> Iterator[tuple[dict | None, bool]]:
    if not raw.strip() or (keep is not None and not keep(raw)):
        return
    try:
        obj = json.loads(decode(raw))
    except ValueError:
        yield None, is_last
        return
    yield (obj if isinstance(obj, dict) else None), is_last
