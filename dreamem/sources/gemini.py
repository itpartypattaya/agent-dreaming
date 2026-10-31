"""Google's agents, which keep their sessions under ``~/.gemini`` (``GEMINI_HOME``): Antigravity and Gemini CLI.

- **Antigravity** (the agy CLI, the app): ``antigravity*/brain/<conversation>/.system_generated/logs/transcript.jsonl``
  — one step a line (``step_index, source, type, status, created_at, content``). A person's turn is ``source
  USER_EXPLICIT`` with ``type USER_INPUT``; its words are inside ``<USER_REQUEST>`` — the rest of the step
  (``<ADDITIONAL_METADATA>``, ``<USER_SETTINGS_CHANGE>``) is the harness. Every other source (MODEL, SYSTEM) is not
  the person. Seen on agy 1.4.3 (2026-10).
- **Gemini CLI**: ``tmp/<project>/chats/session-*.jsonl`` — a header line, then messages (``type user | gemini``) and
  ``$set`` lines that list messages again. A user message whose parts are ``text`` is the person; parts holding a
  ``functionResponse`` are tool results. Seen on Gemini CLI 0.64.

Both formats are internal and undocumented: a step or a message in a shape we do not know is unknown, never the
person's words, and many of them make the coverage partial.
"""
from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

from . import HUMAN, MACHINE, UNKNOWN, Coverage, Message, clean_text
from .jsonl import records

ANTIGRAVITY, GEMINI = "antigravity", "gemini-cli"
_REQUEST_RE = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.DOTALL)


def _ts(value) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _files(paths, since: float, cov: Coverage):
    for path in sorted(paths):
        try:
            if path.stat().st_mtime < since:
                continue
        except OSError:
            cov.partial("vanished")
            continue
        yield path


def _open(path: Path, cov: Coverage):
    try:
        return path.open("rb")
    except OSError:
        cov.partial("unreadable")
        return None


def antigravity_kind(step: dict) -> str:
    source, kind = step.get("source"), step.get("type")
    if source == "USER_EXPLICIT" and kind == "USER_INPUT":
        return HUMAN
    if source in ("MODEL", "SYSTEM"):
        return MACHINE
    return UNKNOWN


def antigravity_text(content: str) -> str:
    """The person's words of a USER_INPUT step: what is inside ``<USER_REQUEST>``; a step without it holds none."""
    parts = _REQUEST_RE.findall(content or "")
    return clean_text("\n".join(p.strip() for p in parts if p.strip()))


def read_antigravity(home: Path, since: float, now: float | None = None) -> tuple[list[Message], Coverage]:
    cov = Coverage(ANTIGRAVITY)
    paths = list(home.glob("antigravity*/brain/*/.system_generated/logs/transcript.jsonl")) if home.is_dir() else []
    if not paths:
        cov.note("not_used")
        return [], cov
    now = time.time() if now is None else now
    out: list[Message] = []
    for path in _files(paths, since, cov):
        cov.files += 1
        stream = _open(path, cov)
        if stream is None:
            continue
        cov.read += 1
        conversation = path.parents[2].name
        bad = 0
        with stream:
            for obj, is_last in records(stream):
                if obj is None:
                    if is_last:
                        cov.note("cut_last_line")
                    else:
                        bad += 1
                    continue
                kind = antigravity_kind(obj)
                cov.count(kind)
                if kind != HUMAN:
                    continue
                text = antigravity_text(obj.get("content") if isinstance(obj.get("content"), str) else "")
                ts = _ts(obj.get("created_at"))
                if not text or ts is None or ts < since or ts > now + 86400:
                    continue
                key = f"{conversation}|{obj.get('step_index')}"
                out.append(Message(ANTIGRAVITY, conversation, ts, text, key=f"{ANTIGRAVITY}:{key}",
                                   source=f"{path.parents[4].name}/{conversation}"))
        if bad:
            cov.partial("bad_lines")
    cov.messages = len(out)
    return out, cov.finish()


def gemini_kind(message: dict) -> str:
    kind, parts = message.get("type"), message.get("content")
    if kind in ("gemini", "info", "error", "warning"):
        return MACHINE
    if kind != "user":
        return UNKNOWN
    if isinstance(parts, str):
        return HUMAN
    if isinstance(parts, list) and parts and all(isinstance(p, dict) for p in parts):
        if any("functionResponse" in p for p in parts):
            return MACHINE
        if all(set(p) <= {"text"} for p in parts):
            return HUMAN
    return UNKNOWN


def gemini_text(message: dict) -> str:
    parts = message.get("content")
    if isinstance(parts, str):
        return clean_text(parts)
    return clean_text("\n".join(p["text"] for p in parts if isinstance(p.get("text"), str)))


def read_gemini(home: Path, since: float, now: float | None = None) -> tuple[list[Message], Coverage]:
    cov = Coverage(GEMINI)
    paths = list(home.glob("tmp/*/chats/session-*.jsonl")) if home.is_dir() else []
    if not paths:
        cov.note("not_used")
        return [], cov
    now = time.time() if now is None else now
    out: list[Message] = []
    seen: set[str] = set()
    for path in _files(paths, since, cov):
        cov.files += 1
        stream = _open(path, cov)
        if stream is None:
            continue
        cov.read += 1
        session, bad = path.stem, 0
        with stream:
            for obj, is_last in records(stream):
                if obj is None:
                    if is_last:
                        cov.note("cut_last_line")
                    else:
                        bad += 1
                    continue
                if "sessionId" in obj and "type" not in obj:
                    session = str(obj["sessionId"])
                    continue
                listed = (obj.get("$set") or {}).get("messages") if isinstance(obj.get("$set"), dict) else None
                for message in listed if isinstance(listed, list) else [obj] if "type" in obj else []:
                    if not isinstance(message, dict):
                        continue
                    key = f"{session}|{message.get('id')}"
                    if key in seen:
                        continue  # a `$set` lists the messages again
                    seen.add(key)
                    kind = gemini_kind(message)
                    cov.count(kind)
                    if kind != HUMAN:
                        continue
                    text, ts = gemini_text(message), _ts(message.get("timestamp"))
                    if not text or ts is None or ts < since or ts > now + 86400:
                        continue
                    out.append(Message(GEMINI, session, ts, text, key=f"{GEMINI}:{key}",
                                       source=f"{path.parents[1].name}/{path.stem}"))
        if bad:
            cov.partial("bad_lines")
    cov.messages = len(out)
    return out, cov.finish()
