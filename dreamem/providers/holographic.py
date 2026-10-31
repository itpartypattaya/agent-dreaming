"""holographic-memory: the reference system. Everything the night asks and every call the review prints were made for
it; this module only gathers them behind the provider seam, unchanged (``tests/fixtures/holomem-0.7.0-calls.json``)."""
from __future__ import annotations

from .. import store as db
from .base import (CONFIRMATIONS, EXPECT, HISTORY, JOURNAL, MERGES, NOT_DUPLICATES, NOTHING, ORIGIN, REJECT, TRUST,
                   VERSIONS, Provider)


def _feedback(action):
    return lambda q, text: {"tool": "memory_feedback", "args": {"fact_id": q["fact_id"], "action": action}}


def _change(tool):
    def make(q, text):
        if not text:
            raise ValueError(f"'{tool}' needs the new text: --text \"...\"")
        return {"tool": tool, "args": {"fact_id": q["fact_id"], "content": text}}
    return make


def _add(q, text):
    if not text:
        raise ValueError("'save' needs the fact as one short sentence: --text \"...\"")
    return {"tool": "memory_add", "args": {"content": text, "origin": "said"}}


def _end(q, text):
    return {"tool": "memory_end", "args": {"fact_id": q["fact_id"]}}


def _forget(q, text):
    return {"tool": "memory_forget", "args": {"fact_id": q["fact_id"]}}


def _merge(q, text):
    keep, copy = q["keep"], q["copy"]
    args = {"fact_id": keep["fact_id"], "from": [copy["fact_id"]],
            "expect": {str(keep["fact_id"]): keep["version_id"], str(copy["fact_id"]): copy["version_id"]}}
    if q.get("keywords_after"):
        args["keywords"] = q["keywords_after"]
    return {"tool": "memory_merge", "args": args}


_FACT_ANSWERS = {"yes": _feedback("confirmed"), "no": _feedback("unhelpful"), "outdated": _end,
                 "wrong": _change("memory_correct"), "forget": _forget, "reject": REJECT}
ANSWERS = {
    # a fact people stand behind, said again on different days
    "promotion": {"yes": _feedback("confirmed"), "no": REJECT, "outdated": _end, "forget": _forget},
    # a fact without a known origin, corroborated: only a person's "yes" gives it one
    "review": dict(_FACT_ANSWERS),
    # an agent stored it lately and nobody stood behind it yet
    "new_fact": dict(_FACT_ANSWERS),
    "confirm": {"yes": _feedback("confirmed"), "no": NOTHING},
    "trust_feedback": {"yes": _feedback("unhelpful"), "no": NOTHING},
    "fact_decay": {"keep": _feedback("confirmed"), "outdated": _end, "changed": _change("memory_change"),
                   "wrong": _change("memory_correct"), "forget": _forget},
    # an answer that used it went wrong, by the agent's own report in holographic-memory's journal
    "outcome": {"keep": _feedback("confirmed"), "outdated": _end, "changed": _change("memory_change"),
                "wrong": _change("memory_correct"), "forget": _forget},
    "conflict": {"changed": _change("memory_change"), "outdated": _end, "wrong": _change("memory_correct"),
                 "keep": NOTHING},
    "duplicate": {"yes": _merge, "not-duplicates": NOT_DUPLICATES},
    # a possible new fact: the review agent judges it first; "save" adds the fact the agent worded and the person
    # approved; the rest are kept so the same clause is not offered again (`no` for CANDIDATE_REST_DAYS)
    "new_candidate": {"save": _add, "no": NOTHING, "not-a-fact": NOTHING, "never": NOTHING,
                      "already-stored": NOTHING},
}


class Holographic(Provider):
    name = "holographic"
    title = "holographic-memory"
    capabilities = frozenset({VERSIONS, HISTORY, ORIGIN, TRUST, CONFIRMATIONS, JOURNAL, MERGES, EXPECT})

    def facts(self):
        return None  # the engine reads the store itself, read-only

    def heads(self, ids):
        return db.facts_state(self.store, ids)

    def gone(self):
        return db.forgotten(self.store)

    def went_wrong(self):
        return db.went_wrong(self.store)

    def current_texts(self):
        return db.current_texts(self.store)

    def current_facts(self):
        return db.current_facts(self.store)

    def answers(self, section):
        return ANSWERS.get(section, {})

    def reject_call(self, q):
        # "Never ask again" is also "this fact is not useful": recall should stop offering it too (the engine would
        # ask for this very call as `trust_feedback` the next night).
        return _feedback("unhelpful")(q, None)

    def finish(self, q, call):
        if call["tool"] != "memory_merge" and q.get("version_id") is not None:
            # The store refuses the write when the fact is no longer at the version shown (holographic-memory 0.5.0+;
            # an older one ignores the argument and writes unchecked).
            call["args"]["expect"] = {str(q.get("fact_id")): q["version_id"]}
        return call

    def check(self, q):
        return {"database": q.get("store")}
