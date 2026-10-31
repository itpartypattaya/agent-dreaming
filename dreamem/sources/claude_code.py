"""Claude Code transcripts: ``<config dir>/projects/<slug>/<session-id>.jsonl``.

Who wrote a ``type: user`` line is told by a combination of fields, not by one (seen on Desktop 2.1.309 / CLI 2.1.314):

- ``turnOrigin: human`` — a person typed it. In the Desktop app such lines also carry ``promptSource: sdk``: that is the
  app's transport, not a machine.
- ``promptSource: system``, ``turnOrigin`` of ``sdk`` / ``task_notification`` / ``scheduled`` — the harness, a background
  task, a scheduled job, another session.
- ``isMeta``, ``isSidechain`` (a subagent), ``isCompactSummary``, ``tool_result`` blocks — never a person.
- No ``turnOrigin`` at all — slash commands, their output, "[Request interrupted…]". Versions older than the field
  sent a person's prompt as ``promptSource: sdk`` (Desktop) or with neither field: such a line counts as a person's only
  when its ``version`` is older than every version seen writing ``turnOrigin`` — never when no such version was seen. Any other plain text without it is a
  combination we do not know, and stays out (fail-closed), counted in the coverage.

A month of transcripts is gigabytes, nearly all of it tool output, and a transcript only grows. So a pass may keep a
cache of *positions* (``cache``: offset and length of each line that may be a person's, and how far the file was read)
— never a word of the lines. A file whose first and last read bytes are still the same is read only at those
positions and from where the last pass stopped; anything else (shorter, rewritten, a position that no longer holds
such a line) is read whole again.
"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime
from pathlib import Path

from ..fsutil import read_json, write_private
from . import HUMAN, MACHINE, UNKNOWN, Coverage, Message, clean_text
from .jsonl import records

CLIENT = "claude-code"
_MACHINE_ORIGINS = {"sdk", "task_notification", "scheduled"}
CACHE_VERSION = 1
_SPAN = 4096  # bytes at the start and before the end of what was read that must not change


def _ts(value) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _text(message: dict) -> str | None:
    """The text blocks of a user message joined; None when it carries a tool result (the harness, not a person)."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "tool_result":
            return None
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


def classify(obj: dict) -> str:
    if obj.get("isMeta") or obj.get("isSidechain") or obj.get("isCompactSummary"):
        return MACHINE
    origin, source = obj.get("turnOrigin"), obj.get("promptSource")
    if source == "system" or origin in _MACHINE_ORIGINS:
        return MACHINE
    if origin == "human":
        return HUMAN
    return UNKNOWN


def _version(value) -> tuple | None:
    try:
        return tuple(int(x) for x in str(value).split(".")[:3]) if value else None
    except ValueError:
        return None


def _maybe_user(raw: bytes) -> bool:
    # A tool result is the harness's line, and the bulk of a transcript: never parsed.
    return b'"user"' in raw and b'"tool_use_id"' not in raw


def _digest(handle, start: int, end: int) -> str:
    handle.seek(start)
    return hashlib.sha1(handle.read(end - start)).hexdigest()


def _marks(handle, end: int) -> dict:
    return {"head": _digest(handle, 0, min(_SPAN, end)), "tail": _digest(handle, max(0, end - _SPAN), end)}


def _scan(handle, start: int, cov: Coverage, lines: list) -> tuple[list[bytes], int, bool]:
    """The lines from *start* that may be a person's, the end of the last whole line, and whether the read went to the
    end. Each whole line kept is added to *lines* as [offset, length]; a cut last line is returned, not remembered."""
    handle.seek(start)
    pos, kept, failed = start, [], False

    def read_all():
        nonlocal failed
        try:
            yield from handle
        except OSError:
            failed = True
            cov.partial("unreadable")

    for raw in read_all():
        if not raw.endswith(b"\n"):
            kept.append(raw)  # a line being written: read now, read again next time
            continue
        if _maybe_user(raw):
            lines.append([pos, len(raw)])
            kept.append(raw)
        pos += len(raw)
    return kept, pos, not failed


def _at(handle, entry: dict) -> tuple[list[bytes], dict] | None:
    """The lines at the remembered positions and a copy of the entry, or None when the file is no longer what was
    read (or the entry is not one this version wrote)."""
    try:
        size = handle.seek(0, 2)
        scanned = int(entry["scanned"])
        if size < scanned or _marks(handle, scanned) != {"head": entry.get("head"), "tail": entry.get("tail")}:
            return None
        copy = {"lines": [[int(o), int(n)] for o, n in entry["lines"]], "machine": int(entry.get("machine", 0)),
                "unknown": int(entry.get("unknown", 0)), "tagged": [[int(x) for x in v] for v in entry.get("tagged", [])]}
        out = []
        for offset, length in copy["lines"]:
            handle.seek(offset)
            raw = handle.read(length)
            if len(raw) != length or not raw.endswith(b"\n") or not _maybe_user(raw):
                return None
            out.append(raw)
        return out, copy
    except (KeyError, TypeError, ValueError, OSError):
        return None


def _lines_of(handle, entry: dict | None, cov: Coverage) -> tuple[list[bytes], dict, bool]:
    """The candidate lines of an open transcript, its cache entry (``lines``: the positions of the first of them;
    ``machine``, ``unknown``, ``tagged``: what earlier passes settled for good) and whether it may be remembered."""
    known = _at(handle, entry) if entry is not None else None
    if known is not None:
        kept, new = known
        more, end, whole = _scan(handle, int(entry["scanned"]), cov, new["lines"])
        kept = kept + more
    else:
        new = {"lines": [], "machine": 0, "unknown": 0, "tagged": []}
        kept, end, whole = _scan(handle, 0, cov, new["lines"])
    try:
        new.update(scanned=end, **_marks(handle, end))
    except OSError:
        whole = False
    return kept, new, whole


def _load_cache(cache: Path | None) -> dict:
    data = read_json(cache, {}) if cache is not None else {}
    if not isinstance(data, dict) or data.get("v") != CACHE_VERSION or not isinstance(data.get("files"), dict):
        return {}
    return data["files"]


def read(projects: Path, since: float, now: float | None = None,
         cache: Path | None = None, only: set[str] | None = None,
         boundary: tuple | None = None, keep: bool = True) -> tuple[list[Message], Coverage]:
    """*only*: the session files to read (``<project>/<session>.jsonl``), every one when None — read whatever their
    mtime. *boundary*: a version seen writing turnOrigin elsewhere (a message read again alone keeps the evidence
    that made its line count). *keep*: False reads the cache but leaves it as it was (a dry run keeps nothing)."""
    cov = Coverage(CLIENT)
    if not projects.is_dir():
        cov.note("not_used")  # nothing to read is not a gap
        return [], cov
    now = time.time() if now is None else now
    out: list[Message] = []
    seen: set[str] = set()
    tagged: set[tuple] = set()
    legacy: list[tuple] = []
    known = _load_cache(cache)
    remembered: dict = {}

    def take(obj, text, path, below=""):
        ts = _ts(obj.get("timestamp"))
        if ts is None or ts < since or ts > now + 86400:
            return
        # A resumed or forked session copies earlier lines into a new file under the same uuid.
        key = str(obj.get("uuid") or f"{obj.get('timestamp')}|{text[:200]}")
        if key in seen:
            cov.note("duplicate_line")
            return
        seen.add(key)
        out.append(Message(CLIENT, str(obj.get("sessionId") or path.stem), ts, text,
                           cwd=str(obj.get("cwd") or ""), key=f"{CLIENT}:{key}",
                           source=path.relative_to(projects).as_posix(), below=below))

    def judge(obj, path):
        """Count one line; returns how it is settled for good — MACHINE, UNKNOWN, "none" (counts nothing) — or None
        (a person's words, or a line decided once every file is read), with the version it shows writing turnOrigin."""
        if obj.get("type") != "user":
            return "none", None
        message = obj.get("message")
        if not isinstance(message, dict) or message.get("role") != "user":
            return "none", None
        raw = _text(message)
        if raw is None or not raw.strip():
            return "none", None
        text = clean_text(raw)
        if not text:
            cov.count(MACHINE)  # a notice, a command, a reminder: nothing of a person left
            return MACHINE, None
        kind = classify(obj)
        version = _version(obj.get("version"))
        shown = version if "turnOrigin" in obj and version else None
        if shown:
            tagged.add(shown)
        if kind == UNKNOWN and "turnOrigin" not in obj and obj.get("promptSource") in (None, "sdk") and version:
            legacy.append((version, obj, text, path))  # decided once every file is read
            return None, None
        cov.count(kind)
        if kind == HUMAN:
            take(obj, text, path)
            return None, shown
        return kind, shown

    for path in sorted(projects.glob("*/*.jsonl")):
        if only is not None and path.relative_to(projects).as_posix() not in only:
            continue
        try:
            if only is None and path.stat().st_mtime < since:
                continue
        except OSError:
            cov.partial("vanished")
            continue
        cov.files += 1
        try:
            handle = path.open("rb")
        except FileNotFoundError:
            cov.partial("vanished")
            continue
        except OSError:
            cov.partial("unreadable")
            continue
        cov.read += 1
        bad = 0
        name = path.relative_to(projects).as_posix()
        with handle:
            lines, entry, whole = _lines_of(handle, known.get(name), cov)
        for kind in (MACHINE, UNKNOWN):  # lines earlier passes settled: counted, not read
            if entry[kind]:
                cov.kinds[kind] = cov.kinds.get(kind, 0) + entry[kind]
        tagged.update(tuple(v) for v in entry["tagged"])
        positions, entry["lines"] = entry["lines"], []
        for i, line in enumerate(lines):
            settled = None
            for obj, is_last in records([line], _maybe_user):
                if obj is None:
                    if is_last:
                        cov.note("cut_last_line")
                    else:
                        bad += 1
                    continue
                settled, version = judge(obj, path)
                if settled and version and list(version) not in entry["tagged"]:
                    entry["tagged"].append(list(version))
            if i >= len(positions):
                continue  # the line being written has no position yet
            if settled in (MACHINE, UNKNOWN):
                entry[settled] += 1
            elif settled is None:
                entry["lines"].append(positions[i])  # a person's line or one decided later: read every pass
        if whole:
            remembered[name] = entry
        if bad:
            cov.partial("bad_lines")
            cov.note("bad_line", bad - 1)
    # Versions before `turnOrigin` existed sent a person's prompt as promptSource sdk (or none) and nothing more:
    # such a line is a person's when its version is older than every version seen writing turnOrigin.
    if boundary:
        tagged.add(tuple(boundary))
    first_tagged = min(tagged) if tagged else None
    for version, obj, text, path in legacy:
        if first_tagged is not None and version < first_tagged:
            cov.count(HUMAN)
            take(obj, text, path, below=".".join(map(str, first_tagged)))
        else:
            cov.count(UNKNOWN)
    cov.messages = len(out)
    if cache is not None and keep:
        try:
            write_private(cache, json.dumps({"v": CACHE_VERSION, "files": remembered}, separators=(",", ":")))
        except OSError:
            pass  # a cache not written costs the next pass time, nothing else
    return out, cov.finish()
