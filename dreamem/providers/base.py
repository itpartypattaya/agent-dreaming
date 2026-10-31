"""What a memory system offers the night and the review.

A provider reads one store of one memory system for the pass (its facts, the head version of each, what was forgotten)
and says how a person's answer becomes calls of that system's own tools. The pass and the agent of the review never
write a store themselves: the provider only prints the calls; the agent makes them in a session, on the person's word.

Facts of a system without numeric ids get surrogate ones (``ids.py``), so the engine and the questions keep their
integers. Capabilities say what the system can tell or do; a section or an answer that needs a missing one is not
asked or not offered.
"""
from __future__ import annotations

# What a system may have.
VERSIONS = "versions"            # a version per change, and a head to check a write against
HISTORY = "history"              # an old text survives a change (change / correct / end differ)
ORIGIN = "origin"                # who stands behind a text: said / confirmed / inferred
TRUST = "trust"                  # trust scores and retrieval counts (trust_feedback, decay by retrievals)
CONFIRMATIONS = "confirmations"  # a person's confirmations are kept
JOURNAL = "journal"              # an outcome journal of answers that went wrong
MERGES = "merges"                # duplicates merge into one fact
EXPECT = "expect"                # a write carries the version shown and the store refuses a changed fact

NOTHING = None          # decided, nothing to call
REJECT = "reject"       # the engine's reject list
NOT_DUPLICATES = "not-duplicates"


class Provider:
    """A memory system's store, as the night and the review use it. The defaults: no facts, nothing forgotten,
    nothing to answer."""

    name = ""
    capabilities: frozenset = frozenset()
    title = ""  # the system as people call it, for the review's lines

    def __init__(self, store: str | None, state_dir=None):
        self.store = str(store) if store else None
        self.state_dir = state_dir

    @classmethod
    def locate(cls, homes, table: dict):
        """The store this system keeps on this machine, or None (then it has no profile). The default: the ``path``
        of an enabled ``[providers.<name>]`` table."""
        raw = str(table.get("path") or "").strip() if table.get("enabled") is True else ""
        if not raw:
            return None
        import os
        from pathlib import Path
        return Path(os.path.expandvars(raw)).expanduser()

    def can_decay(self) -> bool:
        """Whether "still true?" may be asked of a fact nobody mentioned: only with retrieval counts to go by (a
        system without them adds its own clock of first sightings)."""
        return TRUST in self.capabilities

    # -- the pass ----------------------------------------------------------------------------------------------
    def facts(self) -> list[dict] | None:
        """Every fact that holds now, as the engine reads them (``fact_id`` an int, ``content``, ``created_at``, …);
        None when the engine reads this store itself (holographic-memory)."""
        return []

    def heads(self, ids) -> dict[int, dict]:
        """{fact_id: {version_id, content, origin, status[, locator]}} as the store holds them now."""
        return {}

    def gone(self) -> set[int]:
        """Facts forgotten (or removed) since: a question about one is never shown again."""
        return set()

    def went_wrong(self) -> list[dict]:
        return []

    def coverage(self) -> dict | None:
        """``{"status": "partial", …}`` when the store could be read only in part (after ``facts``)."""
        return None

    def current_texts(self) -> list[str]:
        return [t for _, t in self.current_facts()]

    def current_facts(self) -> list[tuple[int, str]]:
        return [(f["fact_id"], f["content"]) for f in self.facts() or []]

    # -- the review --------------------------------------------------------------------------------------------
    def answers(self, section: str) -> dict:
        """{answer: action} this system can carry out for *section*. An action is NOTHING, REJECT, NOT_DUPLICATES or
        ``f(q, text)`` returning a call ``{"tool", "args"}``, a list of calls made in that order, or ``{"edit": …}``."""
        return {}

    def reject_call(self, q: dict) -> dict | None:
        """The call that goes with "never ask again", if the system can record that a fact is not useful."""
        return None

    def finish(self, q: dict, call: dict) -> dict:
        """The call as it is printed (holographic-memory adds the version the person was shown)."""
        return call

    def check(self, q: dict) -> dict:
        """What the agent checks before the calls."""
        return {}

    needs_fact_id = True  # a saved new fact gets an id from the store (`applied --fact-id`)

    def note(self, q: dict) -> str | None:
        """A line the review prints with the question (what this system loses, what to check)."""
        return None
