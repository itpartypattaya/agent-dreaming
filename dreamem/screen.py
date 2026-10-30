"""The filter before anything is written: every message and note goes through the engine's screen (its own rules and
the holographic provider's ``holo_screen``) before the engine sees it.

A message with a secret is not stored at all — not raw, not cut. One with an instruction to an agent is not stored
either; only the counts are. A note (auto-memory) with either is left out of the questions. In a dedupe target
(CLAUDE.md, AGENTS.md, Codex memory), which is all instructions by nature, only paragraphs holding a secret go.
"""
from __future__ import annotations

from dataclasses import replace

from . import engineapi
from .notes import Note
from .sources import Message


def messages(items: list[Message], max_chars: int) -> tuple[list[Message], dict]:
    kept, counts = [], {}

    def drop(why):
        counts[why] = counts.get(why, 0) + 1

    for m in items:
        if len(m.text) > max_chars:
            drop("long")
            continue
        if engineapi.is_envelope(m.text):
            drop("envelope")
            continue
        unsafe = engineapi.classify(m.text)
        if unsafe:
            drop(unsafe)
            continue
        kept.append(m)
    return kept, counts


def notes(items: list[Note]) -> tuple[list[Note], dict]:
    kept, counts = [], {}
    for n in items:
        unsafe = engineapi.classify(n.text)
        if unsafe:
            counts[unsafe] = counts.get(unsafe, 0) + 1
            continue
        kept.append(n)
    return kept, counts


def dedupe_targets(items: list[Note]) -> tuple[list[Note], int]:
    out, dropped = [], 0
    for n in items:
        paragraphs = [p for p in n.text.split("\n\n")]
        safe = [p for p in paragraphs if engineapi.classify(p) != "secret"]
        dropped += len(paragraphs) - len(safe)
        text = "\n\n".join(safe).strip()
        if text:
            out.append(replace(n, text=text))
    return out, dropped
