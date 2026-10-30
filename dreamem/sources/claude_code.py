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
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from . import HUMAN, MACHINE, UNKNOWN, Coverage, Message, clean_text
from .jsonl import records

CLIENT = "claude-code"
_MACHINE_ORIGINS = {"sdk", "task_notification", "scheduled"}


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


def _lines(handle, cov):
    """The lines of an open transcript; a read that fails half way makes the coverage partial."""
    try:
        yield from handle
    except OSError:
        cov.partial("unreadable")


def _maybe_user(raw: bytes) -> bool:
    # A tool result is the harness's line, and the bulk of a transcript: never parsed.
    return b'"user"' in raw and b'"tool_use_id"' not in raw


def read(projects: Path, since: float, now: float | None = None) -> tuple[list[Message], Coverage]:
    cov = Coverage(CLIENT)
    if not projects.is_dir():
        cov.note("not_used")  # nothing to read is not a gap
        return [], cov
    now = time.time() if now is None else now
    out: list[Message] = []
    seen: set[str] = set()
    tagged: set[tuple] = set()
    legacy: list[tuple] = []

    def take(obj, text, path):
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
                           cwd=str(obj.get("cwd") or ""), key=f"{CLIENT}:{key}"))

    for path in sorted(projects.glob("*/*.jsonl")):
        try:
            if path.stat().st_mtime < since:
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
        with handle:
            for obj, is_last in records(_lines(handle, cov), _maybe_user):
                if obj is None:
                    if is_last:
                        cov.note("cut_last_line")
                    else:
                        bad += 1
                    continue
                if obj.get("type") != "user":
                    continue
                message = obj.get("message")
                if not isinstance(message, dict) or message.get("role") != "user":
                    continue
                raw = _text(message)
                if raw is None or not raw.strip():
                    continue
                text = clean_text(raw)
                if not text:
                    cov.count(MACHINE)  # a notice, a command, a reminder: nothing of a person left
                    continue
                kind = classify(obj)
                version = _version(obj.get("version"))
                if "turnOrigin" in obj and version:
                    tagged.add(version)
                if kind == UNKNOWN and "turnOrigin" not in obj and obj.get("promptSource") in (None, "sdk") and version:
                    legacy.append((version, obj, text, path))  # decided once every file is read
                    continue
                cov.count(kind)
                if kind == HUMAN:
                    take(obj, text, path)
        if bad:
            cov.partial("bad_lines")
            cov.note("bad_line", bad - 1)
    # Versions before `turnOrigin` existed sent a person's prompt as promptSource sdk (or none) and nothing more:
    # such a line is a person's when its version is older than every version seen writing turnOrigin.
    first_tagged = min(tagged) if tagged else None
    for version, obj, text, path in legacy:
        if first_tagged is not None and version < first_tagged:
            cov.count(HUMAN)
            take(obj, text, path)
        else:
            cov.count(UNKNOWN)
    cov.messages = len(out)
    return out, cov.finish()
