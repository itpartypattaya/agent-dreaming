"""Memory systems the night reads and the review answers into (see ``base.py``)."""
from __future__ import annotations

from .base import Provider
from .holographic import Holographic
from .mcp_memory import McpMemory

PROVIDERS: dict[str, type[Provider]] = {Holographic.name: Holographic, McpMemory.name: McpMemory}
DEFAULT = Holographic.name


def register(cls: type[Provider]) -> type[Provider]:
    PROVIDERS[cls.name] = cls
    return cls


def get(name: str | None, store=None, state_dir=None) -> Provider:
    cls = PROVIDERS.get(name or DEFAULT)
    if cls is None:
        raise KeyError(f"unknown memory system {name!r}")
    return cls(store, state_dir)


def for_question(q: dict, state_dir=None) -> Provider:
    """The provider of a question's store (a question of a night before 0.6.0 names none: holographic-memory); a
    question about a Claude Code note answers into the note, a conflict with one into the fact or the note."""
    from .claude_notes import SECTIONS, ClaudeNotes, WithNote
    if q.get("section") in SECTIONS:
        return ClaudeNotes(None, state_dir)
    fact = get(q.get("provider"), q.get("store"), state_dir)
    return WithNote(fact) if q.get("section") == "conflict" and q.get("note") else fact
