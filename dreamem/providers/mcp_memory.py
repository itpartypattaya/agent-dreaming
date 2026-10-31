"""The reference MCP ``memory`` server (``@modelcontextprotocol/server-memory``, 2.0.1): a knowledge graph in one JSONL
file — entities with observations, and relations.

Read here, never written: each observation is a fact (relations say how entities relate, not something a person
said about themselves). The file has no ids, no versions, no times and no history, so:

- a fact's id is a surrogate (``ids.json``: a digest of the entity and the text, never the words);
- "first seen" comes from our own journal (``first-seen.json``: digests and dates); "still true?" of a fact nobody
  mentioned is asked only once that journal is two windows old (60 days) — before that, age means nothing;
- a write is checked by its exact text: ``delete_observations`` removes only a word-for-word match, and the agent
  first finds the observation in ``open_nodes``. "Changed" and "wrong" are one answer here: the new text is added,
  then the old deleted (the other order could lose the fact). "Outdated" deletes: there is no history to keep it in.

The file is found in ``[providers.mcp-memory] path``, or as ``MEMORY_FILE_PATH`` of a ``server-memory`` MCP server in
Claude Code's ``.claude.json`` or Codex's ``config.toml``; ``enabled = false`` turns it off.
"""
from __future__ import annotations

import json
import os
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

from .. import engineapi
from ..fsutil import read_json, write_json
from .base import NOTHING, REJECT, Provider
from .ids import Ids, digest

PACKAGE = "server-memory"
JOURNAL_FILE = "first-seen.json"
DECAY_AFTER_DAYS = 60


def _delete(q, text):
    loc = q["locator"]
    return {"tool": "delete_observations",
            "args": {"deletions": [{"entityName": loc["entity"], "observations": [loc["text"]]}]}}


def _add(entity, text):
    return {"tool": "add_observations", "args": {"observations": [{"entityName": entity, "contents": [text]}]}}


def _replace(q, text):
    if not text:
        raise ValueError("the new text is needed: --text \"...\"")
    if text == q["locator"]["text"]:
        return NOTHING  # adding the same text is a no-op there, and the delete after it would remove the fact
    return [_add(q["locator"]["entity"], text), _delete(q, None)]


def _save(q, text):
    if not text:
        raise ValueError("'save' needs the fact as one short sentence: --text \"...\"")
    if not q.get("entity"):
        raise ValueError("'save' needs the entity the fact belongs to: find the person's entity with search_nodes "
                         "(ask the person for its name if there is none) and add --entity \"<name>\"; to keep it, "
                         "the person sets entity = \"<name>\" under [providers.mcp-memory] in ~/.dreaming/config.toml")
    return _add(q["entity"], text)


_FACT = {"yes": NOTHING, "no": NOTHING, "outdated": _delete, "wrong": _replace, "forget": _delete, "reject": REJECT}
ANSWERS = {
    "review": dict(_FACT),
    "new_fact": dict(_FACT),
    "fact_decay": {"keep": NOTHING, "outdated": _delete, "changed": _replace, "forget": _delete},
    "conflict": {"changed": _replace, "outdated": _delete, "keep": NOTHING},
    "new_candidate": {"save": _save, "no": NOTHING, "not-a-fact": NOTHING, "never": NOTHING,
                      "already-stored": NOTHING},
}


def _servers_in(config: dict) -> list[dict]:
    out = [config.get("mcpServers")]
    for project in (config.get("projects") or {}).values() if isinstance(config.get("projects"), dict) else []:
        if isinstance(project, dict):
            out.append(project.get("mcpServers"))
    return [s for group in out if isinstance(group, dict) for s in group.values() if isinstance(s, dict)]


def _file_of(server: dict) -> str | None:
    words = [str(server.get("command") or "")] + [str(a) for a in server.get("args") or []]
    env = server.get("env") if isinstance(server.get("env"), dict) else {}
    if any(PACKAGE in w for w in words) and isinstance(env.get("MEMORY_FILE_PATH"), str):
        return env["MEMORY_FILE_PATH"]
    return None


def configured_files(homes) -> list[Path]:
    """``MEMORY_FILE_PATH`` of every ``server-memory`` server the CLIs are configured with (read only)."""
    found: list[str] = []
    for path in (homes.claude / ".claude.json", homes.claude.parent / ".claude.json"):
        try:
            found += [f for f in map(_file_of, _servers_in(json.loads(path.read_text(encoding="utf-8")))) if f]
        except (OSError, ValueError, AttributeError):
            continue
    try:
        codex = tomllib.loads((homes.codex / "config.toml").read_text(encoding="utf-8"))
        found += [f for f in map(_file_of, (codex.get("mcp_servers") or {}).values()) if f]
    except (OSError, ValueError, AttributeError):
        pass
    out = []
    for raw in found:
        p = Path(os.path.expandvars(raw)).expanduser()
        if p not in out:
            out.append(p)
    return out


def read_graph(path) -> tuple[list[tuple[str, str]], list[str]]:
    """([(entity, observation)], problems). A line that does not parse is a problem (``unreadable``), and the facts of
    the other lines still count — the server itself keeps such a line as it is."""
    facts, problems = [], []
    try:
        raw = Path(path).read_bytes()
    except OSError as exc:
        return [], [f"unreadable: {type(exc).__name__}"]
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            problems.append("unreadable line")
            continue
        if not isinstance(rec, dict) or rec.get("type") != "entity" or not isinstance(rec.get("name"), str):
            continue
        for obs in rec.get("observations") or []:
            if isinstance(obs, str) and obs.strip():
                facts.append((rec["name"], obs))
    return facts, problems


class McpMemory(Provider):
    name = "mcp-memory"
    title = "MCP memory server"

    def __init__(self, store, state_dir=None):
        super().__init__(store, state_dir)
        self.problems: list[str] = []
        self.entity: str | None = None

    @classmethod
    def locate(cls, homes, table):
        if table.get("enabled") is False:
            return None
        raw = str(table.get("path") or "").strip()
        if raw:
            return Path(os.path.expandvars(raw)).expanduser()
        for path in configured_files(homes):
            if path.is_file():
                return path
        return None

    def _graph(self):
        facts, self.problems = read_graph(self.store) if self.store else ([], [])
        # An entity name that holds a secret or an instruction is never shown: its facts are left out.
        return [(e, t) for e, t in facts if not engineapi.classify(e)]

    def _journal(self, keys: list[str]) -> dict:
        path = Path(self.state_dir) / JOURNAL_FILE if self.state_dir else None
        data = (read_json(path, {}) or {}) if path else {}
        data = data if isinstance(data, dict) else {}
        seen = data.get("seen") if isinstance(data.get("seen"), dict) else {}
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        new = [k for k in keys if k not in seen]
        for k in new:
            seen[k] = today
        since = data.get("since") or today
        if path is not None and (new or "since" not in data):
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json(path, {"since": since, "seen": seen})
        self._since = since
        return seen

    def facts(self):
        graph = self._graph()
        ids = Ids(self.state_dir)
        keys = [digest(e, t) for e, t in graph]
        seen = self._journal(keys)
        out = []
        for (_, text), key in zip(graph, keys):
            stamp = f"{seen.get(key)} 00:00:00"
            out.append({"fact_id": ids.of(key), "content": text, "category": "general", "tags": "",
                        "trust_score": None, "retrieval_count": None, "helpful_count": None,
                        "created_at": stamp, "updated_at": stamp})
        ids.save()
        return out

    def _where(self) -> dict[int, tuple[str, str]]:
        known = Ids(self.state_dir).known()
        return {known[digest(e, t)]: (e, t) for e, t in self._graph() if digest(e, t) in known}

    def heads(self, ids):
        where = self._where()
        return {int(i): {"version_id": None, "content": where[int(i)][1], "origin": None, "status": "current",
                         "locator": {"entity": where[int(i)][0], "text": where[int(i)][1]}}
                for i in ids if str(i).isdigit() and int(i) in where}

    def gone(self):
        if not self.store or not Path(self.store).is_file():
            return set()
        present = set(self._where())
        return {i for i in Ids(self.state_dir).known().values() if i not in present}

    def current_facts(self):
        where = self._where()
        return [(i, t) for i, (_, t) in where.items()]

    def can_decay(self):
        if self.problems or not getattr(self, "_since", None):
            return False
        try:
            since = datetime.strptime(self._since, "%Y-%m-%d")
        except ValueError:
            return False
        return datetime.now() - since >= timedelta(days=DECAY_AFTER_DAYS)

    def coverage(self):
        return {"status": "partial", "problems": self.problems} if self.problems else None

    def answers(self, section):
        return ANSWERS.get(section, {})

    def check(self, q):
        """The file the night read (the session's memory server must be configured with it: the server has no
        tool that names its file) and the observation, word for word."""
        loc = q.get("locator") or {}
        if q.get("answer") == "save":
            return {"memory_file": q.get("store"), "entity": q.get("entity")}
        return {"memory_file": q.get("store"), "entity": loc.get("entity"), "observation": loc.get("text")}

    needs_fact_id = False  # a saved observation has no id to give back

    def note(self, q):
        if q.get("section") == "new_candidate":
            return None
        who = (q.get("locator") or {}).get("entity")
        return (f"in the MCP memory server, entity {who!r}; it keeps no history: outdated deletes the observation, "
                "and changed or wrong adds the new text, then deletes the old")
