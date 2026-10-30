"""Codex CLI rollouts: ``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<ts>-<uuid>.jsonl`` (and ``archived_sessions``).

A background worker compresses a rollout untouched for 7 days into ``.jsonl.zst`` and deletes the plain file; while
both exist the plain one is current. So a session is read once, plain first; a plain file that vanished between the
listing and the read is looked for again as ``.zst``; without the ``zstandard`` package a compressed session is not
read and the coverage says so.

Who speaks is told per session, by ``session_meta.source`` (the first line; the coverage counts sessions): ``cli`` and ``vscode`` are interactive (a person types);
``exec`` is a script, an object (``{"subagent": …}``) is another agent; anything else is unknown and stays out.
Inside a person's session only ``response_item`` messages of role ``user`` count — ``developer`` is the harness, and
inserted blocks (``<environment_context>``, AGENTS.md instructions) are no speech.
"""
from __future__ import annotations

import io
import json
import time
from datetime import datetime
from pathlib import Path

from . import HUMAN, MACHINE, UNKNOWN, Coverage, Message, clean_text
from .jsonl import records

CLIENT = "codex"
HUMAN_SOURCES = {"cli", "vscode"}
MACHINE_SOURCES = {"exec", "mcp"}

try:  # a declared dependency; its absence is reported, not fatal
    import zstandard as _zstd
except ImportError:  # pragma: no cover - exercised by a test that hides the module
    _zstd = None


def _ts(value) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def session_kind(meta: dict | None) -> str:
    if not isinstance(meta, dict):
        return UNKNOWN
    source = meta.get("source")
    if isinstance(source, dict):
        return MACHINE  # a subagent or another structured, non-interactive origin
    if isinstance(source, str):
        if source in HUMAN_SOURCES:
            return HUMAN
        if source in MACHINE_SOURCES:
            return MACHINE
    return UNKNOWN


def _stem(path: Path) -> str:
    name = path.name
    return name[: -len(".jsonl.zst")] if name.endswith(".jsonl.zst") else name[: -len(".jsonl")]


def _read_zst(path: Path) -> bytes:
    with path.open("rb") as f:
        return _zstd.ZstdDecompressor().stream_reader(f).read()


def _load(plain: Path | None, packed: Path | None, cov: Coverage) -> bytes | None:
    if plain is not None:
        try:
            return plain.read_bytes()
        except FileNotFoundError:
            packed = packed or plain.with_name(plain.name + ".zst")  # compressed between the listing and now
        except OSError:
            cov.partial("unreadable")
            return None
    if packed is None:
        return None
    if _zstd is None:
        cov.partial("zstd_missing")
        return None
    try:
        return _read_zst(packed)
    except FileNotFoundError:
        cov.partial("vanished")
    except (OSError, _zstd.ZstdError, io.UnsupportedOperation):
        cov.partial("bad_zstd")
    return None


def _first_meta(plain: Path | None, packed: Path | None) -> dict | None:
    """``session_meta`` of the first line, or None (unreadable, compressed without zstandard, not a meta line)."""
    try:
        if plain is not None and plain.exists():
            with plain.open("rb") as f:
                raw = f.readline()
        elif packed is not None and _zstd is not None:
            with packed.open("rb") as f, io.BufferedReader(_zstd.ZstdDecompressor().stream_reader(f)) as r:
                raw = r.readline()
        else:
            return None
        obj = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError, UnicodeDecodeError, Exception):  # noqa: BLE001 - zstd errors and the like: read whole
        return None
    if isinstance(obj, dict) and obj.get("type") == "session_meta" and isinstance(obj.get("payload"), dict):
        return obj["payload"]
    return None


def _wanted(raw: bytes) -> bool:
    return b"session_meta" in raw or b'"user"' in raw


def read(dirs: list[Path], since: float, now: float | None = None) -> tuple[list[Message], Coverage]:
    cov = Coverage(CLIENT)
    if not dirs:
        cov.note("not_used")  # Codex not installed or never run: nothing to read is not a gap
        return [], cov
    now = time.time() if now is None else now
    sessions: dict[str, dict[str, Path]] = {}
    for base in dirs:
        for path in base.rglob("rollout-*.jsonl*"):
            if not path.name.endswith((".jsonl", ".jsonl.zst")):
                continue
            try:
                if path.stat().st_mtime < since:
                    continue
            except OSError:
                cov.partial("vanished")
                continue
            sessions.setdefault(_stem(path), {})["zst" if path.name.endswith(".zst") else "plain"] = path
    out: list[Message] = []
    seen: set[str] = set()
    for stem in sorted(sessions):
        pair = sessions[stem]
        cov.files += 1
        # The kind of a session is on its first line: a script's session (most of them) is never read whole.
        meta = _first_meta(pair.get("plain"), pair.get("zst"))
        if meta is not None:
            kind = session_kind(meta)
            if kind != HUMAN:
                cov.count(kind)
                cov.read += 1
                continue
        data = _load(pair.get("plain"), pair.get("zst"), cov)
        if data is None:
            continue
        cov.read += 1
        meta, kind, cwd, bad = None, UNKNOWN, "", 0
        for obj, is_last in records(data, _wanted):
            if obj is None:
                if is_last:
                    cov.note("cut_last_line")
                else:
                    bad += 1
                continue
            payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
            if obj.get("type") == "session_meta" and meta is None:
                meta = payload
                kind = session_kind(meta)
                cwd = str(meta.get("cwd") or "")
                continue
            if obj.get("type") != "response_item" or payload.get("type") != "message" or payload.get("role") != "user":
                continue
            blocks = payload.get("content") if isinstance(payload.get("content"), list) else []
            texts = [b.get("text") for b in blocks if isinstance(b, dict) and b.get("type") == "input_text"
                     and isinstance(b.get("text"), str)]
            text = clean_text("\n".join(t for t in (clean_text(t) for t in texts) if t))
            if not text or kind != HUMAN:
                continue
            ts = _ts(obj.get("timestamp"))
            if ts is None or ts < since or ts > now + 86400:
                continue
            key = f"{stem}|{obj.get('timestamp')}|{text[:200]}"
            if key in seen:
                continue
            seen.add(key)
            sid = str((meta or {}).get("id") or (meta or {}).get("session_id") or stem)
            out.append(Message(CLIENT, sid, ts, text, cwd=cwd, key=f"{CLIENT}:{key}"))
        cov.count(kind)
        if bad:
            cov.partial("bad_lines")
            cov.note("bad_line", bad - 1)
    cov.messages = len(out)
    return out, cov.finish()
