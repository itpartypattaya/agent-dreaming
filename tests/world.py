"""A machine in a temporary folder: Claude Code, Codex and holographic-memory homes with files in their real formats
(the line shapes seen on Claude Code 2.1.309 / Codex 0.163 / holographic 0.25), synthetic content only."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from dreamem.paths import homes

HISTORY = """
CREATE TABLE facts (fact_id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT, category TEXT DEFAULT 'general',
    tags TEXT DEFAULT '', trust_score REAL DEFAULT 0.5, retrieval_count INTEGER DEFAULT 0,
    helpful_count INTEGER DEFAULT 0, created_at TIMESTAMP, updated_at TIMESTAMP, hrr_vector BLOB);
CREATE TABLE fact_versions (version_id INTEGER PRIMARY KEY AUTOINCREMENT, fact_id INTEGER NOT NULL, seq INTEGER,
    content TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'current', recorded_at TEXT, closed_at TEXT, src_origin TEXT);
CREATE TABLE fact_confirmations (fact_id INTEGER NOT NULL, confirmed_at TEXT NOT NULL, session_id TEXT,
    user_id TEXT, via TEXT);
CREATE TABLE fact_merges (from_fact_id INTEGER PRIMARY KEY, into_fact_id INTEGER NOT NULL,
    into_version_id INTEGER NOT NULL, merged_at TEXT NOT NULL);
CREATE TABLE forgotten (fact_id INTEGER PRIMARY KEY, forgotten_at TEXT NOT NULL, digests TEXT NOT NULL);
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
INSERT INTO meta VALUES ('history_schema', '2');
"""


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def days_ago(d: float) -> float:
    return time.time() - d * 86400


class World:
    def __init__(self, root: Path, *, store: bool = True, scope: str = "global"):
        self.root = Path(root)
        self.env = {"DREAMEM_HOME": str(self.root / "dreaming"), "CLAUDE_CONFIG_DIR": str(self.root / "claude"),
                    "CODEX_HOME": str(self.root / "codex"), "HOLOMEM_HOME": str(self.root / "holo")}
        self.homes = homes(self.env)
        for p in (self.homes.claude / "projects", self.homes.codex / "sessions", self.homes.holomem):
            p.mkdir(parents=True, exist_ok=True)
        self.project = self.root / "work" / "shop"
        (self.project / ".git").mkdir(parents=True)
        self.db = None
        (self.homes.holomem / "config.toml").write_text(f'scope = "{scope}"\n', encoding="utf-8")
        if store and scope == "global":
            self.db = self.make_store(self.homes.holomem / "memory_store.db")

    # -- holographic-memory ---------------------------------------------------------------------------------------
    @staticmethod
    def make_store(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path)
        con.executescript(HISTORY)
        con.close()
        return path

    def sql(self, query, args=(), db=None):
        con = sqlite3.connect(db or self.db)
        try:
            rows = con.execute(query, args).fetchall()
            con.commit()
            return rows
        finally:
            con.close()

    def fact(self, content, *, origin=None, days_old=1.0, trust=0.6, rc=0, helpful=0, db=None):
        stamp = datetime.fromtimestamp(days_ago(days_old), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        self.sql("insert into facts (content, trust_score, retrieval_count, helpful_count, created_at, updated_at)"
                 " values (?,?,?,?,?,?)", (content, trust, rc, helpful, stamp, stamp), db)
        (fid,) = self.sql("select max(fact_id) from facts", db=db)[0]
        self.sql("insert into fact_versions (fact_id, seq, content, status, src_origin) values (?, 1, ?, 'current', ?)",
                 (fid, content, origin), db)
        return fid

    def version(self, fid, db=None):
        return self.sql("select version_id from fact_versions where fact_id=? and status='current'", (fid,), db)[0][0]

    def change(self, fid, content, db=None):
        self.sql("update fact_versions set status='superseded' where fact_id=? and status='current'", (fid,), db)
        self.sql("insert into fact_versions (fact_id, seq, content, status) values (?, 2, ?, 'current')",
                 (fid, content), db)
        self.sql("update facts set content=? where fact_id=?", (content, fid), db)

    def forget(self, fid, db=None):
        self.sql("delete from facts where fact_id=?", (fid,), db)
        self.sql("delete from fact_versions where fact_id=?", (fid,), db)
        self.sql("insert into forgotten values (?, '2026-10-30', '[]')", (fid,), db)

    # -- Claude Code -----------------------------------------------------------------------------------------------
    def claude_line(self, text, *, ts=None, origin="human", source="sdk", session="s1", version="2.1.309",
                    cwd=None, uid=None, **flags):
        line = {"parentUuid": None, "isSidechain": False, "promptSource": source, "turnOrigin": origin,
                "userType": "external", "entrypoint": "claude-desktop", "cwd": str(cwd or self.project),
                "sessionId": session, "version": version, "gitBranch": "main", "type": "user",
                "message": {"role": "user", "content": [{"type": "text", "text": text}] if isinstance(text, str)
                            else text},
                "uuid": uid or str(uuid.uuid4()), "timestamp": iso(ts if ts is not None else days_ago(1))}
        if origin is None:
            line.pop("turnOrigin")
        if source is None:
            line.pop("promptSource")
        line.update(flags)
        return line

    def claude_file(self, lines, *, session="s1", slug="C--work-shop", raw_tail: bytes = b"") -> Path:
        path = self.homes.claude / "projects" / slug / f"{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        filler = {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}
        body = b"".join(json.dumps(x, ensure_ascii=False).encode("utf-8") + b"\n" if isinstance(x, dict) else x
                        for x in [*lines, filler])
        path.write_bytes(body + raw_tail)
        return path

    def say(self, texts_by_day: dict, *, session="talk"):
        """Human lines in Claude Code: {days_ago: [text, …]}."""
        lines = []
        for d, texts in texts_by_day.items():
            for i, t in enumerate(texts):
                lines.append(self.claude_line(t, ts=days_ago(d) + i, session=session))
        return self.claude_file(lines, session=session)

    def note(self, name, description, body, *, project="C--work-shop"):
        folder = self.homes.claude / "projects" / project / "memory"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}.md"
        path.write_text(f"---\nname: {name}\ndescription: {description}\nmetadata:\n  type: project\n---\n\n{body}\n",
                        encoding="utf-8")
        index = folder / "MEMORY.md"
        with index.open("a", encoding="utf-8") as f:
            f.write(f"- [{name}]({name}.md) — {description}\n")
        return path

    # -- Codex -----------------------------------------------------------------------------------------------------
    def rollout(self, messages, *, source="cli", stem=None, zst=False, plain=True, day="2026/10/29", cwd=None):
        stem = stem or f"rollout-2026-10-29T10-00-00-{uuid.uuid4()}"
        folder = self.homes.codex / "sessions" / day
        folder.mkdir(parents=True, exist_ok=True)
        lines = [{"timestamp": iso(days_ago(1)), "type": "session_meta",
                  "payload": {"id": stem[-36:], "cwd": str(cwd or self.project), "originator": "codex-tui",
                              "cli_version": "0.163.0", "source": source}}]
        for ts, role, text in messages:
            lines.append({"timestamp": iso(ts), "type": "response_item",
                          "payload": {"type": "message", "role": role,
                                      "content": [{"type": "input_text" if role != "assistant" else "output_text",
                                                   "text": text}]}})
        data = b"".join(json.dumps(x, ensure_ascii=False).encode("utf-8") + b"\n" for x in lines)
        paths = []
        if plain:
            p = folder / f"{stem}.jsonl"
            p.write_bytes(data)
            paths.append(p)
        if zst:
            import zstandard

            p = folder / f"{stem}.jsonl.zst"
            p.write_bytes(zstandard.ZstdCompressor().compress(data))
            paths.append(p)
        return paths

    # -- after a pass ----------------------------------------------------------------------------------------------
    def tree(self, *roots) -> dict:
        """{relative path: sha256} of every file under *roots* (default: the homes the pass may only read)."""
        roots = roots or (self.homes.claude, self.homes.codex, self.homes.holomem, self.project)
        out = {}
        for root in roots:
            for p in sorted(Path(root).rglob("*")):
                if p.is_file():
                    out[str(p.relative_to(self.root))] = hashlib.sha256(p.read_bytes()).hexdigest()
        return out

    def our_bytes(self) -> bytes:
        """Every byte the pass left in its own home."""
        return b"\n".join(p.read_bytes() for p in sorted(self.homes.dreaming.rglob("*")) if p.is_file())

    def questions(self, profile="global") -> dict:
        return json.loads((self.homes.profile(profile) / "questions.json").read_text(encoding="utf-8"))


def environ(world: World):
    """A context manager: the process environment pointed at *world* (the CLI reads it)."""
    from unittest import mock

    return mock.patch.dict(os.environ, world.env)
