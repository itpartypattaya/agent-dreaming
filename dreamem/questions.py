"""The night's questions and the protocol of an answer.

A question goes ``generated`` (the pass wrote it) → ``shown`` (``dreamem show`` printed it to a session) →
``decided`` (``dreamem decide``: the person's answer) → ``applied`` (``dreamem applied``: the memory tool call it needs
succeeded) — or ``failed`` (the call was refused: the fact changed since it was shown; the question comes back).

The engine's cooldowns follow the answers, not the showing:

- before the next pass, every question of the last night that was not decided has its cooldown mark removed from the
  engine's state (``dream-seen.json`` buckets, ``dream-asked.json`` for notes), so it is asked again;
- a night with at least one decision gets a confirmation session in the shadow ``state.db`` naming the decided
  "merge?" numbers (``D-17``) — the engine keeps the marks of an acknowledged night and reopens a merge question whose
  number the answer does not name; a night with none gets no confirmation, and the engine reopens all of it itself.

An answer never changes memory by itself: it says which memory tool call the agent makes (and what it must check
first). Only ``reject`` and ``not-duplicates`` write — the engine's reject list, in the shadow home.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import engineapi
from .fsutil import read_json, write_json

GENERATED, SHOWN, DECIDED, APPLIED, FAILED, DEFERRED = "generated", "shown", "decided", "applied", "failed", "deferred"
OPEN = (GENERATED, SHOWN, FAILED, DEFERRED)
CLOSED = (DECIDED, APPLIED)

# Asked first: what may be wrong in memory now, then what may be missing, then the notes.
ORDER = ("duplicate", "conflict", "fact_decay", "review", "new_fact", "promotion", "confirm", "trust_feedback",
         "note_decay")
BUCKET = {"duplicate": "duplicate_pairs", "conflict": "conflicts", "fact_decay": "fact_decays", "review": "promotions",
          "new_fact": "new_facts", "promotion": "promotions", "confirm": "confirm", "trust_feedback": "trust_feedback"}


def _feedback(action):
    return lambda q, text: {"tool": "memory_feedback", "args": {"fact_id": q["fact_id"], "action": action}}


def _change(tool):
    def make(q, text):
        if not text:
            raise ValueError(f"'{tool}' needs the new text: --text \"...\"")
        return {"tool": tool, "args": {"fact_id": q["fact_id"], "content": text}}
    return make


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


NOTHING = None          # decided, nothing to call
REJECT = "reject"       # the engine's reject list (dream-reject.py <fact_id>)
NOT_DUPLICATES = "not-duplicates"
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
    "conflict": {"changed": _change("memory_change"), "outdated": _end, "wrong": _change("memory_correct"),
                 "keep": NOTHING},
    "duplicate": {"yes": _merge, "not-duplicates": NOT_DUPLICATES},
    # auto-memory notes are Claude's files: the pass and the agent of the review never edit them (v1)
    "note_decay": {"keep": NOTHING, "outdated": NOTHING},
}
LATER = "later"  # any section: not now, ask again tomorrow


def _seen_key(item: dict) -> str:
    return item.get("fingerprint") or engineapi.fingerprint(item.get("content") or "")


def build(result: dict, *, store: str | None, facts: dict, notes_index: dict, coverage_ok: bool,
          max_questions: int, profile: str) -> tuple[list[dict], dict]:
    """The night's questions from the engine's JSON, and what was left out and why."""
    qs: list[dict] = []
    skipped: dict[str, int] = {}

    def skip(why):
        skipped[why] = skipped.get(why, 0) + 1

    def fact_q(section, item, **extra):
        fid = item.get("fact_id")
        state = facts.get(int(fid)) if str(fid).isdigit() else None
        q = {"section": section, "bucket": BUCKET[section], "seen_key": _seen_key(item), "fact_id": fid,
             "version_id": (state or {}).get("version_id"), "origin": (state or {}).get("origin"),
             "content": item.get("content"), "why": item.get("why"), "store": store}
        q.update(extra)
        qs.append(q)

    for item in result.get("duplicate_pairs") or []:
        qs.append({"section": "duplicate", "bucket": "duplicate_pairs", "seen_key": None, "proposal": item["proposal"],
                   "keep": item["keep"], "copy": item["copy"], "keywords_after": item.get("keywords_after"),
                   "why": item.get("why"), "store": item.get("store") or store})
    for item in result.get("conflicts") or []:
        fact_q("conflict", item, conflicts=item.get("conflicts"))
    for item in result.get("fact_decays") or []:
        if not coverage_ok:
            skip("fact_decay:coverage")
            continue
        fact_q("fact_decay", item)
    for item in result.get("promotions") or []:
        state = facts.get(int(item["fact_id"])) if str(item.get("fact_id")).isdigit() else None
        origin = (state or {}).get("origin")
        if origin is None:
            # Without a known origin the engine lets corroboration alone decide; here a person decides.
            fact_q("review", item, days=item.get("ref_days"), mentions=item.get("mentions"))
        else:
            fact_q("promotion", item, days=item.get("ref_days"), mentions=item.get("mentions"))
    for item in result.get("new_facts") or []:
        state = facts.get(int(item["fact_id"])) if str(item.get("fact_id")).isdigit() else None
        if (state or {}).get("origin") in ("said", "confirmed"):
            skip("new_fact:person_stands_behind")
            continue
        fact_q("new_fact", item)
    for item in result.get("confirm") or []:
        fact_q("confirm", item, days=item.get("days"))
    for item in result.get("trust_feedback") or []:
        fact_q("trust_feedback", item)
    for item in result.get("md_decays") or []:
        if not coverage_ok:
            skip("note_decay:coverage")
            continue
        note = notes_index.get(int(item["index"])) if "index" in item else None
        if note is None:
            skip("note_decay:unmapped")
            continue
        qs.append({"section": "note_decay", "bucket": None, "seen_key": None, "entry_key": note["entry_key"],
                   "note": {k: note[k] for k in ("id", "project", "path", "name")}, "content": item.get("entry"),
                   "why": item.get("reason"), "last_mention_days": item.get("last_mention_days")})
    qs.sort(key=lambda q: ORDER.index(q["section"]))
    for i, q in enumerate(qs, 1):
        q["qid"] = f"Q{i}"
        q["profile"] = profile
        q["status"] = GENERATED if i <= max_questions else DEFERRED
        q["answer"] = None
    return qs, skipped


# -- between nights ------------------------------------------------------------------------------------------------


def reopen_undecided(home: Path, previous: dict | None) -> dict:
    """Remove the engine's cooldown marks of the last night's questions nobody answered; returns the counts."""
    out = {"seen": 0, "asked": 0}
    if not previous:
        return out
    open_qs = [q for q in previous.get("questions") or [] if q.get("status") in OPEN]
    if not open_qs:
        return out
    seen_path, asked_path = home / "cache" / "dream-seen.json", home / "cache" / "dream-asked.json"
    seen = read_json(seen_path, {}) or {}
    asked = read_json(asked_path, {}) or {}
    pending = (seen.get("_pending") or {}).get("seen") or {}
    for q in open_qs:
        bucket, key = q.get("bucket"), q.get("seen_key")
        if bucket and key and isinstance(seen.get(bucket), dict) and seen[bucket].pop(key, None) is not None:
            out["seen"] += 1
            if isinstance(pending.get(bucket), list) and key in pending[bucket]:
                pending[bucket].remove(key)
        if q.get("section") == "note_decay" and asked.pop(q.get("entry_key"), None) is not None:
            out["asked"] += 1
    if out["seen"]:
        write_json(seen_path, seen)
    if out["asked"]:
        write_json(asked_path, asked)
    return out


def ack_for(home: Path, previous: dict | None) -> dict | None:
    """The confirmation the engine looks for, or None: only for the night the engine last published (its ``_pending``)
    and only when a person decided at least one of its questions."""
    if not previous:
        return None
    pending = (read_json(home / "cache" / "dream-seen.json", {}) or {}).get("_pending") or {}
    generated = previous.get("generated_at")
    if not generated or pending.get("generated_at") != generated:
        return None
    decided = [q for q in previous.get("questions") or [] if q.get("status") in CLOSED]
    if not decided:
        return None
    parts = [f"{q['qid']} {q.get('answer')}" for q in decided]
    named = [q["proposal"] for q in decided if q.get("section") == "duplicate"]
    answer = "Answered by the person: " + ", ".join(parts) + "."
    if named:
        answer += " Merge proposals answered: " + ", ".join(named) + "."
    return {"generated_at": generated, "answer": answer}


# -- the review ----------------------------------------------------------------------------------------------------


def find(doc: dict, qid: str) -> dict:
    qid = qid.strip().upper()
    for q in doc.get("questions") or []:
        if q["qid"] == qid:
            return q
    raise KeyError(f"no question {qid} in the night of {doc.get('generated_at')}")


def decide(q: dict, answer: str, text: str | None = None) -> dict:
    """Record the person's *answer*; returns what to do: ``{"call": {...}, "check": {...}}`` for a memory tool call,
    ``{"local": "reject" | "not-duplicates"}`` for the engine's reject list, ``{}`` when nothing is to be done."""
    answer = answer.strip().lower()
    if answer == LATER:
        q["status"], q["answer"] = SHOWN, None
        return {}
    table = ANSWERS[q["section"]]
    if answer not in table:
        raise ValueError(f"{q['qid']} ({q['section']}) takes: {', '.join([*table, LATER])}")
    if q.get("status") in CLOSED:
        raise ValueError(f"{q['qid']} is already {q['status']} ({q.get('answer')}); 'dreamem undo {q['qid']}' first")
    action = table[answer]
    if action is REJECT:
        # "Never ask again" is also "this fact is not useful": recall should stop offering it too (the engine would ask
        # for this very call as `trust_feedback` the next night).
        call, todo = _feedback("unhelpful")(q, text), {"local": REJECT}
    elif action is NOT_DUPLICATES:
        call, todo = None, {"local": NOT_DUPLICATES}
    elif action is NOTHING:
        call, todo = None, {}
    else:
        call, todo = action(q, text), {}  # raises before anything is recorded when the answer lacks its text
    q["answer"], q["text"] = answer, text
    q["decided_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    q["status"] = DECIDED
    q["call"] = call
    if call is None:
        return todo
    check = {"database": q.get("store")}
    if call["tool"] != "memory_merge":
        # Until holographic-memory takes `expect` on every write, the agent checks the version itself, right before.
        check["version"] = {"fact_id": q.get("fact_id"), "version_id": q.get("version_id")}
    return {**todo, "call": call, "check": check}


def applied(q: dict, result: str | None = None) -> None:
    if q.get("status") != DECIDED or not q.get("call"):
        raise ValueError(f"{q['qid']} has no decided call to mark applied (status {q.get('status')})")
    q["status"], q["result"] = APPLIED, result


def failed(q: dict, reason: str) -> None:
    q["status"], q["failure"] = FAILED, reason


def undo(q: dict) -> None:
    """Cancel the recorded decision (memory is not touched: a call already made is undone with memory_revert)."""
    for k in ("answer", "text", "call", "decided_at", "result", "failure"):
        q.pop(k, None)
    q["answer"] = None
    q["status"] = SHOWN
