"""The night's questions and the protocol of an answer.

A question goes ``generated`` (the pass wrote it) → ``shown`` (``dreamem show`` printed it to a session) →
``decided`` (``dreamem decide``: the person's answer) → ``applied`` (``dreamem applied``: the memory tool call it needs
succeeded) — or ``failed`` (the call was refused: the fact changed since it was shown; the question comes back).

The engine's cooldowns follow the answers, not the showing:

- before the next pass, every question of the last night that was not decided has its cooldown mark removed from the
  engine's state (``seen.json`` buckets, ``asked.json`` for notes), so it is asked again;
- a night with at least one decision is handed to the engine as acknowledged, with an answer naming the decided
  "merge?" numbers (``D-17``) — the engine keeps the marks of an acknowledged night and reopens a merge question whose
  number the answer does not name; a night with none is not acknowledged, and the engine reopens all of it itself.

An answer never changes memory by itself: it says which memory tool call the agent makes (and what it must check
first). Only ``reject`` and ``not-duplicates`` write — the engine's reject list and proposals, in the profile's state.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from . import engineapi, providers
from .providers import holographic
from .providers.base import NOT_DUPLICATES, NOTHING, REJECT
from .fsutil import read_json, write_json

GENERATED, SHOWN, DECIDED, APPLIED, FAILED, DEFERRED = "generated", "shown", "decided", "applied", "failed", "deferred"
GONE = "gone"  # a candidate whose message is no longer there as it was: never shown, nothing to answer
OPEN = (GENERATED, SHOWN, FAILED, DEFERRED)
CLOSED = (DECIDED, APPLIED)

# Asked first: what may be wrong in memory now, then what may be missing, then the notes.
ORDER = ("duplicate", "conflict", "outcome", "fact_decay", "review", "new_fact", "promotion", "confirm", "trust_feedback",
         "note_duplicate", "note_decay", "new_candidate")
NOTE_KEYS = ("id", "project", "path", "name", "sha256")
BUCKET = {"duplicate": "duplicate_pairs", "conflict": "conflicts", "fact_decay": "fact_decays", "review": "promotions",
          "new_fact": "new_facts", "promotion": "promotions", "confirm": "confirm", "trust_feedback": "trust_feedback"}


# The answers each section takes and the call each makes are the store's own (``providers``); holographic-memory's
# table, the reference, is kept here under its old name.
ANSWERS = holographic.ANSWERS
LATER = "later"  # any section: not now, ask again tomorrow


def _same_text(item: dict, current) -> bool:
    """Whether the text the night judged is the current version's. An item the engine cut (200 characters) or hid
    carries the fingerprint of its whole text; the rest are compared whole. A prefix is never enough: a change past
    the cut is a change (Codex review of 0.2.0)."""
    if current is None:
        return True
    if item.get("fingerprint"):
        return item["fingerprint"] == engineapi.fingerprint(current)
    return (item.get("content") or "") == current


def _seen_key(item: dict) -> str:
    return item.get("fingerprint") or engineapi.fingerprint(item.get("content") or "")


def build(result: dict, *, store: str | None, facts: dict, notes_index: dict, coverage_ok: bool,
          max_questions: int, profile: str, night_tag: str | None = None, went_wrong: list[dict] | None = None,
          answered: set | None = None, rejected: set | None = None,
          candidates: list[dict] | None = None, provider=None, note_pairs: list[dict] | None = None,
          notes: list | None = None) -> tuple[list[dict], dict]:
    # Question ids carry the night: an answer meant for "Q1" of one night never lands on "Q1" of the next. The tag is
    # the caller's (unique per published night; two nights may share a `generated_at` second).
    night = night_tag or hashlib.sha1(str(result.get("generated_at") or "").encode("utf-8")).hexdigest()[:4]
    """The night's questions from the engine's JSON, and what was left out and why."""
    qs: list[dict] = []
    skipped: dict[str, int] = {}
    provider = provider or providers.get(None, store)

    def skip(why):
        skipped[why] = skipped.get(why, 0) + 1

    def fact_q(section, item, **extra):
        fid = item.get("fact_id")
        state = facts.get(int(fid)) if str(fid).isdigit() else None
        if state is not None and state.get("status") == "ended":
            skip("ended_since")  # it no longer holds, by the person's own answer or an agent's: nothing to ask
            return
        if state is not None and not _same_text(item, state.get("content")):
            # Changed by another session while the night ran: the version read now is not the text the night
            # judged. Asked about tomorrow, with the text and the version that go together.
            skip("changed_during_pass")
            return
        q = {"section": section, "bucket": BUCKET[section], "seen_key": _seen_key(item), "fact_id": fid,
             "version_id": (state or {}).get("version_id"), "origin": (state or {}).get("origin"),
             "content": item.get("content"), "why": item.get("why"), "store": store}
        if (state or {}).get("locator") is not None:
            q["locator"] = state["locator"]  # where the fact lives in its system (no numeric id there)
        q.update(extra)
        qs.append(q)

    for item in result.get("duplicate_pairs") or []:
        qs.append({"section": "duplicate", "bucket": "duplicate_pairs", "seen_key": None, "proposal": item["proposal"],
                   "keep": item["keep"], "copy": item["copy"], "keywords_after": item.get("keywords_after"),
                   "why": item.get("why"), "store": item.get("store") or store})
    for item in result.get("conflicts") or []:
        fact_q("conflict", item, conflicts=item.get("conflicts"))
    for item in went_wrong or []:
        state = facts.get(int(item["fact_id"]))
        if set(item["events"]) <= (answered or set()):
            skip("outcome:answered")
        elif state is None or state.get("content") is None or state.get("status") == "ended":
            skip("outcome:gone")  # ended or forgotten since
        elif state.get("version_id") != item["version_id"]:
            skip("outcome:changed_since")  # changed after the answer went wrong: the problem is likely handled
        elif engineapi.fingerprint(state["content"]) in (rejected or set()):
            skip("outcome:rejected")
        else:
            correction = item.get("correction")
            if correction and engineapi.classify(correction):
                correction = None  # a secret or an instruction to an agent in the agent's line: not shown
            qs.append({"section": "outcome", "bucket": None, "seen_key": None, "fact_id": item["fact_id"],
                       "version_id": state.get("version_id"), "origin": state.get("origin"),
                       "content": state["content"], "store": store,
                       "outcome": {"events": item["events"], "named": item["named"], "attempts": item["attempts"],
                                   "last_at": item["last_at"], "correction": correction}})
    for item in result.get("fact_decays") or []:
        if not coverage_ok:
            skip("fact_decay:coverage")
            continue
        if not provider.can_decay():
            skip("fact_decay:no_retrievals")  # nothing tells "nobody used it" from "nobody needed it yet"
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
                   "note": {k: note.get(k) for k in NOTE_KEYS}, "content": item.get("entry"),
                   "why": item.get("reason"), "last_mention_days": item.get("last_mention_days")})
    for pair in note_pairs or []:
        a = pair["note"]
        other = pair.get("other_note")
        q = {"section": "note_duplicate", "bucket": None, "seen_key": None, "pair": pair["key"],
             "note": {k: getattr(a, k) for k in NOTE_KEYS}, "content": a.text}
        if other is not None:
            q.update(other={k: getattr(other, k) for k in NOTE_KEYS}, other_content=other.text)
        else:
            q.update(fact=pair["fact"], store=store)
        qs.append(q)
    for q in qs:  # a conflict with a Claude Code note: the note may be what no longer holds
        if q["section"] == "conflict" and notes:
            note = note_of(q.get("conflicts") or [], notes)
            if note is not None:
                q["note"] = {k: getattr(note, k) for k in NOTE_KEYS}
    for item in candidates or []:  # references and digests only: the review reads the words again
        qs.append({"section": "new_candidate", "bucket": None, "seen_key": None, "ref": item["ref"],
                   "clause": item["clause"], "days": item["days"], "store": store})
    qs.sort(key=lambda q: ORDER.index(q["section"]))
    # One question about a fact a night: the engine may offer a fact both as "people keep saying it" and as "said again
    # on different days". The first by ORDER is asked; the other waits (deferred: it comes back if not answered).
    first_of: dict = {}
    shown = 0
    for i, q in enumerate(qs, 1):
        q["qid"] = f"Q{i}-{night}"
        q["profile"] = profile
        if q["section"] not in ("note_decay", "note_duplicate"):
            q["provider"] = provider.name
        q["answer"] = None
        if q["section"] == "new_candidate":
            q["status"] = GENERATED  # outside max_questions: the agent asks about one at most
            continue
        fid = q.get("fact_id")
        if fid is not None and fid in first_of:
            q["status"], q["same_fact_as"] = DEFERRED, first_of[fid]
            skipped["same_fact"] = skipped.get("same_fact", 0) + 1
            continue
        if fid is not None:
            first_of[fid] = q["qid"]
        shown += 1
        q["status"] = GENERATED if shown <= max_questions else DEFERRED
    return qs, skipped


def note_of(hits: list[dict], notes: list):
    """The Claude Code note a conflict's memory entry was cut from (its preview is the start of a chunk of it)."""
    def flat(t):
        return " ".join((t or "").split())
    for hit in hits:
        entry = flat(hit.get("entry")).rstrip("…").strip()
        if len(entry) < 20:
            continue
        for n in notes:
            if entry in flat(n.text):
                return n
    return None


PAIRS_FILE = "note-pairs.json"  # in a profile's state: pairs of notes a person kept both of (digests only)


def pair_key(a: str, b: str) -> str:
    return hashlib.sha256("\x00".join(sorted([entry_key(a), entry_key(b)])).encode("utf-8")).hexdigest()[:32]


def pair_rests(state_dir: Path, key: str) -> bool:
    data = read_json(state_dir / PAIRS_FILE, {})
    return isinstance(data, dict) and key in data


def answer_pair(state_dir: Path, q: dict, answered: bool) -> None:
    """Record (or, on undo, drop) a "keep both": the same two texts are not offered again."""
    if q.get("section") != "note_duplicate" or not q.get("pair"):
        return
    data = read_json(state_dir / PAIRS_FILE, {}) or {}
    data = data if isinstance(data, dict) else {}
    if answered and q.get("answer") == "keep-both":
        data[q["pair"]] = {"at": datetime.now().astimezone().isoformat(timespec="seconds")}
    else:
        data.pop(q["pair"], None)
    write_json(state_dir / PAIRS_FILE, data)


def fact_ids(q: dict) -> set[int]:
    """Every fact a question names: its own, a merge's two, a note's duplicate."""
    ids = {q.get("fact_id")} | {(q.get(s) or {}).get("fact_id") for s in ("keep", "copy", "fact")}
    return {i for i in ids if isinstance(i, int)}


# -- between nights ------------------------------------------------------------------------------------------------


def entry_key(entry: str) -> str:
    return engineapi.entry_key(entry)


def open_sections(qs: list[dict]) -> dict[str, int]:
    """Open questions by section, as the session line counts them: the candidates as one (one is asked at most)."""
    sections: dict[str, int] = {}
    for q in qs:
        if q["status"] in OPEN and q["status"] != DEFERRED:
            sections[q["section"]] = sections.get(q["section"], 0) + 1
    if sections.get("new_candidate"):
        sections["new_candidate"] = 1
    return sections


CANDIDATES_FILE = "candidates.json"  # in a profile's state: clause digests a person answered about
CANDIDATE_REST_DAYS = 30


def candidate_rests(state_dir: Path, clause: str) -> bool:
    """Whether the clause with this digest is not to be offered: answered for good, or "no" less than
    CANDIDATE_REST_DAYS ago."""
    data = read_json(state_dir / CANDIDATES_FILE, {})
    rec = data.get(clause) if isinstance(data, dict) else None
    if not isinstance(rec, dict):
        return False
    if rec.get("answer") != "no":
        return True
    at = _stamp(rec.get("at"))
    return at is not None and (datetime.now().astimezone() - at).days < CANDIDATE_REST_DAYS


def answer_candidate(state_dir: Path, q: dict, answered: bool) -> None:
    """Record (or, on undo, drop) the answer about a candidate's clause — its digest, never its words."""
    if q.get("section") != "new_candidate" or not q.get("clause"):
        return
    data = read_json(state_dir / CANDIDATES_FILE, {}) or {}
    data = data if isinstance(data, dict) else {}
    if answered:
        data[q["clause"]] = {"answer": q.get("answer"), "at": datetime.now().astimezone().isoformat(timespec="seconds")}
    else:
        data.pop(q["clause"], None)
    write_json(state_dir / CANDIDATES_FILE, data)


OUTCOMES_FILE = "outcomes.json"  # in a profile's state: journal events a person has answered about


def outcomes_answered(state_dir: Path) -> set:
    data = read_json(state_dir / OUTCOMES_FILE, {}) or {}
    return set(data) if isinstance(data, dict) else set()


def answer_outcomes(state_dir: Path, q: dict, answered: bool) -> None:
    """Record (or, on undo, drop) the journal events an ``outcome`` question was about: an event a person answered is
    not asked about again; a new one about the same fact is."""
    events = (q.get("outcome") or {}).get("events") or []
    if q.get("section") != "outcome" or not events:
        return
    data = read_json(state_dir / OUTCOMES_FILE, {}) or {}
    data = data if isinstance(data, dict) else {}
    for e in events:
        if answered:
            data[e] = {"qid": q["qid"], "answer": q.get("answer"), "at": q.get("decided_at")}
        else:
            data.pop(e, None)
    write_json(state_dir / OUTCOMES_FILE, data)


def _stamp(value):
    try:
        when = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.astimezone()


def reopen_undecided(state_dir: Path, previous: dict | None) -> dict:
    """Keep only the rests a person earned. A cooldown mark of the engine stays when it predates the last published
    night, or belongs to a question of that night the person decided; every other one goes — the marks of questions
    left open, of items the night held back (partial coverage, the same fact twice) and of a night that died before it
    was published (its marks are newer than the last published one). Returns the counts of marks removed."""
    out = {"seen": 0, "asked": 0}
    seen_path, asked_path = state_dir / "seen.json", state_dir / "asked.json"
    seen = read_json(seen_path, {}) or {}
    asked = read_json(asked_path, {}) or {}
    published = _stamp((previous or {}).get("generated_at"))
    decided = [q for q in (previous or {}).get("questions") or [] if q.get("status") in CLOSED]
    keep_seen = {(q.get("bucket"), q.get("seen_key")) for q in decided if q.get("seen_key")}
    keep_pairs = {q.get("proposal") for q in decided if q.get("section") == "duplicate"}
    keep_asked = {q.get("entry_key") for q in decided if q.get("section") == "note_decay"}

    def earned(record) -> bool:
        at = _stamp(record.get("at")) if isinstance(record, dict) else None
        return at is None or (published is not None and at < published)

    for bucket, records in seen.items():
        if bucket.startswith("_") or bucket == "dup_not" or not isinstance(records, dict):
            continue
        for key in list(records):
            rec = records[key]
            if earned(rec) or (bucket, key) in keep_seen or (
                    bucket == "duplicate_pairs" and isinstance(rec, dict) and rec.get("proposal") in keep_pairs):
                continue
            records.pop(key)
            out["seen"] += 1
    for key in list(asked):
        if earned(asked[key]) or key in keep_asked:
            continue
        asked.pop(key)
        out["asked"] += 1
    if out["seen"]:
        write_json(seen_path, seen)
    if out["asked"]:
        write_json(asked_path, asked)
    return out


def acks_for(state_dir: Path, previous: dict | None) -> dict | None:
    """{generated_at: [answer]} for the engine, or None: only for the night the engine last published (its
    ``_pending``) and only when a person decided at least one of its questions."""
    if not previous:
        return None
    pending = (read_json(state_dir / "seen.json", {}) or {}).get("_pending") or {}
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
    return {generated: [answer]}


# -- the review ----------------------------------------------------------------------------------------------------


def find(doc: dict, qid: str) -> dict:
    """The question with exactly this id (night tag included)."""
    qid = qid.strip()
    for q in doc.get("questions") or []:
        if q["qid"].lower() == qid.lower():
            return q
    raise KeyError(f"no question {qid} in the night of {doc.get('generated_at')}")


def answers(q: dict) -> dict:
    """{answer: action} a question takes: what its store can carry out (``later`` is always taken too)."""
    return providers.for_question(q).answers(q["section"])


def decide(q: dict, answer: str, text: str | None = None) -> dict:
    """Record the person's *answer*; returns what to do: ``{"call": {...}, "check": {...}}`` for a memory tool call
    (``"calls"``: several, in that order; ``"edit"``: a change of a file the agent makes), ``{"local": "reject" |
    "not-duplicates"}`` for the engine's reject list, ``{}`` when nothing is to be done."""
    answer = answer.strip().lower()
    if q.get("status") == GONE:
        raise ValueError(f"{q['qid']} is gone: its message changed or went since the night; nothing to answer")
    if q.get("section") == "new_candidate" and answer != "save":
        text = None  # only a fact to save is worded; any other text would keep the person's words here
    if answer == LATER:
        q["status"], q["answer"] = SHOWN, None
        return {}
    writer = providers.for_question(q)
    table = writer.answers(q["section"])
    if answer not in table:
        raise ValueError(f"{q['qid']} ({q['section']}) takes: {', '.join([*table, LATER])}")
    if q.get("status") in CLOSED:
        raise ValueError(f"{q['qid']} is already {q['status']} ({q.get('answer')}); 'dreamem undo {q['qid']}' first")
    action = table[answer]
    if action is REJECT:
        made, todo = writer.reject_call(q), {"local": REJECT}
    elif action is NOT_DUPLICATES:
        made, todo = None, {"local": NOT_DUPLICATES}
    elif action is NOTHING:
        made, todo = None, {}
    else:
        made, todo = action(q, text), {}  # raises before anything is recorded when the answer lacks its text
    q["answer"], q["text"] = answer, text
    q["decided_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    q["status"] = DECIDED
    for k in ("call", "calls", "edit"):
        q.pop(k, None)
    q["call"] = None
    if made is None:
        return todo
    if isinstance(made, dict) and "edit" in made:
        q["edit"] = made["edit"]
        return {**todo, "edit": made["edit"], "check": writer.check(q)}
    if isinstance(made, list):
        q.pop("call")
        q["calls"] = [writer.finish(q, c) for c in made]
        return {**todo, "calls": q["calls"], "check": writer.check(q)}
    q["call"] = writer.finish(q, made)
    return {**todo, "call": q["call"], "check": writer.check(q)}


def to_apply(q: dict) -> bool:
    """Whether a decided question has something for the agent to make: a call, calls or an edit."""
    return bool(q.get("call") or q.get("calls") or q.get("edit"))


def applied(q: dict, result: str | None = None, fact_id: int | None = None) -> None:
    if q.get("status") != DECIDED or not to_apply(q):
        raise ValueError(f"{q['qid']} has no decided call to mark applied (status {q.get('status')})")
    q["status"], q["result"] = APPLIED, result
    if (q.get("edit") or {}).get("action") == "delete":
        # The note is gone at the person's word: its text leaves this file too (path and name stay, to say which).
        for k in ("content", "other_content", "conflicts", "why"):
            q.pop(k, None)
    if q.get("section") == "new_candidate":
        # The fact now lives in memory; the person's words leave this file: only the new fact's id stays (so a forget
        # can find it), not the text, the call or what the tool answered.
        q["fact_id"] = fact_id
        drop_words(q)


WORDS = ("text", "call", "calls", "edit", "result", "failure")
# What of a question may hold a forgotten fact's text: removed when the fact is forgotten (or deleted) since.
FACT_WORDS = ("content", "keep", "copy", "why", "conflicts", "call", "calls", "edit", "text", "result", "note", "outcome",
              "locator", "fact", "other", "other_content")  # what of a candidate may hold words: the fact, the call, tool answers


def drop_words(q: dict) -> None:
    """A candidate that reached its end keeps no words (only the open, approved save may hold its text)."""
    for k in WORDS:
        q.pop(k, None)


def gone(q: dict) -> None:
    q["status"] = GONE
    drop_words(q)


def failed(q: dict, reason: str) -> None:
    q["status"], q["failure"] = FAILED, reason


def undo(q: dict) -> None:
    """Cancel the recorded decision (memory is not touched: a call already made is undone with memory_revert)."""
    for k in ("answer", "text", "call", "calls", "edit", "decided_at", "result", "failure"):
        q.pop(k, None)
    q["answer"] = None
    q["status"] = SHOWN
