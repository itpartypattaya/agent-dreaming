"""The shadow home: the home layout the dreaming engine reads, built from the CLIs' files.

``~/.dreaming/profiles/<profile>/home/`` holds:

- ``config.yaml`` — only the block the engine reads to know the fact store: ``plugins.hermes-memory-store.db_path`` the
  very database holographic-memory opens, ``scope: profile``. Without it the engine cannot prove that the store it reads
  is the store a "yes" will change, and turns the "merge?" questions off (``store_unknown``).
- ``dreaming.json`` — the engine's settings: the store, the windows, one time zone, trust rules (a session marked as a
  person's chat is trusted, nothing else), the judge off (no network).
- ``state.db`` — ``sessions`` + ``messages`` with the person's words only, screened before they are written. Rebuilt
  for every pass and deleted after it: the shadow home keeps no conversation between nights.
- ``memories/MEMORY.md`` — one §-entry per auto-memory note; ``memories/codex.md`` and ``memories/instructions.md`` —
  dedupe targets. ``notes.json`` maps the entries back to the files.
- ``cache/`` and ``memories/DREAMS.md`` — the engine's own state and diary, kept between nights.

A confirmation session (``source: dream-ack``) tells the engine which night a person answered: the engine looks for a
session whose user line holds that night's ``generated_at`` and an answer of the assistant (``_agent_answers``).
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from . import engineapi
from .config import Profile
from .fsutil import PARTIAL, write_json, write_private
from .notes import Note
from .sources import Message

ACK_SOURCE = "dream-ack"
DELIM = "\n§\n"


def _yaml_str(value: str) -> str:
    return json.dumps(value)  # a YAML double-quoted scalar is a JSON string for every path we write


def config_yaml(profile: Profile) -> str:
    if profile.store is None:
        return "memory:\n  provider: none\n"
    lines = ["memory:", "  provider: holographic", "plugins:", "  hermes-memory-store:",
             f"    db_path: {_yaml_str(profile.store.as_posix())}", "    scope: profile"]
    if profile.min_trust is not None:
        lines.append(f"    min_trust_threshold: {profile.min_trust}")
    return "\n".join(lines) + "\n"


def dreaming_json(profile: Profile, settings: dict, tz: str) -> dict:
    windows = settings.get("windows") or {}
    cfg = {
        "timezone": tz,
        "trusted_chat_ids": [],
        "trust_private_chats": True,
        "trust_sessions_without_chat": False,
        "untrusted_sources": ["cron", "subagent", "tool", ACK_SOURCE],
        "exclude_patterns": list(settings.get("exclude_patterns") or []),
        "agent_names": list(settings.get("agent_names") or []),
        "durable_memory_paths": ["memories/MEMORY.md", "memories/codex.md", "memories/instructions.md"],
        "fact_source": "holographic" if profile.store is not None else "none",
        "diary": {"path": "memories/DREAMS.md"},
        "windows": {"themes_days": int(windows.get("themes_days", 14)),
                    "corroboration_days": int(windows.get("corroboration_days", 30)),
                    "md_decay_days": int(windows.get("md_decay_days", 30))},
        "judge": {"mode": "off"},
        "pending": {"dir": "pending/memory"},
    }
    if profile.store is not None:
        cfg["fact_store_path"] = profile.store.as_posix()
    return cfg


_SCHEMA = """
create table sessions (id text primary key, source text, chat_type text, chat_id text, thread_id text, user_id text);
create table messages (id integer primary key autoincrement, session_id text, role text, content text,
                       timestamp real, observed integer default 0, active integer default 1, compacted integer default 0);
"""


def build_state_db(path: Path, messages: list[Message], project_of, ack: dict | None) -> None:
    """``state.db`` with *messages* (already screened) and, when *ack* is given, the confirmation session.
    Built next to its place and moved in, so a pass killed half way leaves a ``.partial``, never half a database."""
    tmp = path.with_name(path.name + PARTIAL)
    for leftover in (tmp, Path(f"{tmp}-journal"), Path(f"{tmp}-wal"), Path(f"{tmp}-shm")):
        if leftover.exists():
            leftover.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    conn = sqlite3.connect(tmp)
    try:
        conn.executescript(_SCHEMA)
        sessions = {}
        for m in messages:
            sid = f"{m.client}:{m.session_id}"
            if sid not in sessions:
                sessions[sid] = (sid, m.client, "dm", project_of(m) or m.client, None, "owner")
        conn.executemany("insert into sessions values (?,?,?,?,?,?)", sessions.values())
        conn.executemany("insert into messages (session_id, role, content, timestamp) values (?,?,?,?)",
                         [(f"{m.client}:{m.session_id}", "user", m.text, m.ts)
                          for m in sorted(messages, key=lambda m: (m.ts, m.key))])
        if ack:
            when = datetime.fromisoformat(ack["generated_at"]).timestamp() + 1
            conn.execute("insert into sessions values (?,?,?,?,?,?)", ("dream-ack", ACK_SOURCE, None, None, None, None))
            needle = json.dumps({"generated_at": ack["generated_at"]}, separators=(",", ":"))
            conn.execute("insert into messages (session_id, role, content, timestamp) values (?,?,?,?)",
                         ("dream-ack", "user", needle, when))
            conn.execute("insert into messages (session_id, role, content, timestamp) values (?,?,?,?)",
                         ("dream-ack", "assistant", ack["answer"], when + 1))
        conn.commit()
    finally:
        conn.close()
    os.replace(tmp, path)


def drop_state_db(home: Path) -> None:
    for name in ("state.db", "state.db-journal", "state.db-wal", "state.db-shm"):
        try:
            (home / name).unlink()
        except FileNotFoundError:
            pass


def build(profile_dir: Path, profile: Profile, settings: dict, tz: str, messages: list[Message], project_of,
          auto_notes: list[Note], codex_notes: list[Note], instruction_notes: list[Note], ack: dict | None) -> dict:
    """Write the shadow home; returns the index of the auto-memory entries ({entry index: note})."""
    home = profile_dir / "home"
    (home / "memories").mkdir(parents=True, exist_ok=True)
    (home / "cache").mkdir(parents=True, exist_ok=True)
    write_private(home / "config.yaml", config_yaml(profile))
    write_json(home / "dreaming.json", dreaming_json(profile, settings, tz))
    mine = auto_notes if profile.notes else []
    write_private(home / "memories" / "MEMORY.md", DELIM.join(n.text for n in mine) + ("\n" if mine else ""))
    write_private(home / "memories" / "codex.md", DELIM.join(n.text for n in codex_notes) + "\n")
    write_private(home / "memories" / "instructions.md", DELIM.join(n.text for n in instruction_notes) + "\n")
    # The engine's own parse decides what an entry is; the notes are mapped to its entries by text.
    by_text = {}
    for n in mine:
        by_text.setdefault(n.text.strip(), n)
    index = {}
    for i, entry in enumerate(engineapi.parse_entries(home / "memories" / "MEMORY.md")):
        note = by_text.get(entry.strip())
        if note is not None:
            index[i] = dict(asdict(note), entry_key=engineapi.entry_key(entry))
    write_json(profile_dir / "notes.json", {str(k): {kk: vv for kk, vv in v.items() if kk != "text"}
                                            for k, v in index.items()})
    build_state_db(home / "state.db", messages, project_of, ack)
    return index
