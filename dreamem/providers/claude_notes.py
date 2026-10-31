"""Claude Code's memory notes (auto memory, subagents' memory) as answers go into them.

Claude reads and writes these files in every session, and the person may edit or delete them at any time; the review
writes them the same way — the agent of the session, on the person's answer. The pass never does. Before an edit the
agent runs ``dreamem verify <Q>``: the file must be byte for byte the one the person was shown (its sha256); then it
replaces the exact old text, which fails by itself if the file changed in between. A delete removes the file and its
line in ``MEMORY.md``. A note has no history: "outdated" deletes it, and the question says so.

Codex memories are never written: Codex calls them generated state, reset only as a whole.
"""
from __future__ import annotations

from pathlib import Path

from .base import NOTHING, Provider


def _edit(action):
    def make(q, text):
        note = q.get("note") or {}
        if action == "replace-body" and not text:
            raise ValueError("the new text of the note is needed: --text \"...\"")
        edit = {"action": action, "path": note["path"], "sha256": note.get("sha256")}
        if action == "replace-body":
            # The question showed description and body together: the old fact may sit in either.
            edit["text"] = text
            edit["description"] = " ".join(text.split())[:150]
        else:
            path = Path(note["path"])
            edit.update(index=str(path.parent / "MEMORY.md"), index_entry=path.name)
        return {"edit": edit}
    return make


DELETE, REPLACE = _edit("delete"), _edit("replace-body")
ANSWERS = {
    "note_decay": {"keep": NOTHING, "outdated": DELETE, "changed": REPLACE},
    "note_duplicate": {"drop-note": DELETE, "keep-both": NOTHING},
}
CONFLICT = {"note-changed": REPLACE, "note-outdated": DELETE}  # the note is the one that no longer holds
SECTIONS = tuple(ANSWERS)


class ClaudeNotes(Provider):
    name = "claude-notes"
    title = "Claude Code memory notes"

    def answers(self, section):
        return ANSWERS.get(section, {})

    def check(self, q):
        return {"verify": f"dreamem verify {q['qid']}"}

    def note(self, q):
        return "a note has no history: outdated deletes the file (and its line in MEMORY.md)" \
            if q.get("section") == "note_decay" else None


class WithNote(Provider):
    """A fact's question whose other side is a Claude Code note: the fact's own answers, and the note's."""

    def __init__(self, fact: Provider):
        super().__init__(fact.store, fact.state_dir)
        self.fact, self.notes = fact, ClaudeNotes(None)
        self.name, self.title, self.capabilities = fact.name, fact.title, fact.capabilities

    def heads(self, ids):
        return self.fact.heads(ids)

    def gone(self):
        return self.fact.gone()

    def answers(self, section):
        return {**self.fact.answers(section), **(CONFLICT if section == "conflict" else {})}

    def reject_call(self, q):
        return self.fact.reject_call(q)

    def finish(self, q, call):
        return self.fact.finish(q, call)

    def check(self, q):
        return self.notes.check(q) if str(q.get("answer") or "").startswith("note-") else self.fact.check(q)

    def note(self, q):
        return self.fact.note(q)
