"""What a person and a session read: ``report.md`` per profile, the questions as text, and the one-line summary the
SessionStart hook prints (numbers only — never a fact's text: the hook's output goes into every session's context)."""
from __future__ import annotations

from datetime import datetime

from .questions import CLOSED, DEFERRED, OPEN

SECTION_TITLE = {
    "duplicate": "look like one fact — merge?",
    "conflict": "conflicts with what memory says",
    "fact_decay": "not mentioned for long — still true?",
    "review": "corroborated, but nobody said it is so — confirm?",
    "new_fact": "an agent stored this lately — is it right?",
    "promotion": "people keep saying this — mark as confirmed?",
    "confirm": "said again on different days — mark as confirmed?",
    "trust_feedback": "rejected earlier, still recalled — lower its trust?",
    "note_decay": "a Claude Code memory note nobody mentioned lately — still relevant?",
}


def _q(text, n=400):
    text = (text or "").replace("\n", " ").strip()
    return f"«{text[:n]}{'…' if len(text) > n else ''}»"


def question_text(q: dict) -> str:
    s = q["section"]
    if s == "duplicate":
        keep, copy = q["keep"], q["copy"]
        line = f"{q['qid']} [{q['proposal']}] {_q(keep['content'], 2000)} and {_q(copy['content'], 2000)} — {SECTION_TITLE[s]}"
        if copy.get("covered") is False and copy.get("extra_words"):
            line += f" (only the second has: {', '.join(copy['extra_words'])})"
        return line
    if s == "note_decay":
        note = q["note"]
        return (f"{q['qid']} note '{note['name']}' of project {note['project']}: {_q(q.get('content'), 160)} — "
                f"{SECTION_TITLE[s]} ({q.get('why')})")
    line = f"{q['qid']} fact #{q['fact_id']}: {_q(q.get('content'))} — {SECTION_TITLE[s]}"
    if s == "conflict" and q.get("conflicts"):
        other = q["conflicts"][0]
        line += f" Memory: {_q(other.get('entry') or other.get('content'), 200)}"
    if q.get("why") and s in ("review", "promotion", "fact_decay"):
        line += f" ({q['why']})"
    return line


def local_time(stamp: str) -> str:
    try:
        return datetime.fromisoformat(stamp).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(stamp)[:16]


def coverage_line(coverage: dict) -> str:
    parts = []
    for c in coverage.values():
        if c["reasons"].get("not_used") and not c["files"]:
            continue
        extra = f" ({', '.join(sorted(k for k in c['reasons'] if k not in ('cut_last_line', 'duplicate_line')))})" \
            if c["status"] != "complete" else ""
        parts.append(f"{c['client']} {c['status']}{extra}: {c['messages']} messages in {c['read']} sessions")
    return " · ".join(parts) or "no sessions found"


def render(doc: dict) -> str:
    qs = doc["questions"]
    open_qs = [q for q in qs if q["status"] in OPEN and q["status"] != DEFERRED]
    lines = [f"# Dreaming — {local_time(doc['generated_at'])} · profile {doc['profile']}", "",
             f"Read: {coverage_line(doc['coverage'])}.",
             f"Facts in the store: {doc['stats'].get('facts', 0)}; messages that may corroborate: "
             f"{doc['stats'].get('messages_window', 0)}.", ""]
    if open_qs:
        lines.append(f"## Questions ({len(open_qs)})")
        lines += [question_text(q) for q in open_qs]
        deferred = sum(q["status"] == DEFERRED for q in qs)
        if deferred:
            lines.append(f"(+{deferred} more on another night)")
    else:
        lines.append("No questions tonight.")
    done = [q for q in qs if q["status"] in CLOSED]
    if done:
        lines += ["", f"Answered: {', '.join(q['qid'] + ' ' + str(q['answer']) for q in done)}."]
    notes = []
    for a in doc.get("alerts") or []:
        notes.append(f"- {a.get('kind')}: {a.get('message', '')[:300]}")
    for p in doc.get("index_pressure") or []:
        if max(p["lines_pct"], p["bytes_pct"]) >= 80:
            notes.append(f"- Claude Code memory index of {p['project']}: {p['lines']} lines / {p['bytes']} bytes "
                         f"({max(p['lines_pct'], p['bytes_pct'])}% of what loads at start)")
    screened = doc.get("screened") or {}
    if screened.get("secret") or screened.get("injection"):
        notes.append(f"- not read: {screened.get('secret', 0)} message(s) holding a secret, "
                     f"{screened.get('injection', 0)} holding instructions to an agent")
    if doc.get("skipped"):
        notes.append("- held back: " + ", ".join(f"{k} {v}" for k, v in sorted(doc["skipped"].items())))
    if notes:
        lines += ["", "## Notes", *notes]
    return "\n".join(lines) + "\n"


def summary_line(entries: dict) -> str | None:
    """'Dreaming 2026-10-30: 3 memory questions are waiting (1 duplicate, 2 facts). …' or None when nothing waits."""
    total, kinds, day = 0, {}, ""
    for e in entries.values():
        if not isinstance(e, dict):
            continue
        total += e.get("open", 0)
        day = max(day, (e.get("generated_at") or "")[:10])
        for k, v in (e.get("sections") or {}).items():
            kinds[k] = kinds.get(k, 0) + v
    if not total:
        return None
    parts = []
    for name, keys in (("duplicate", ("duplicate",)),
                       ("fact", ("conflict", "fact_decay", "review", "new_fact", "promotion", "confirm", "trust_feedback")),
                       ("note", ("note_decay",))):
        n = sum(kinds.get(k, 0) for k in keys)
        if n:
            parts.append(f"{n} {name}{'s' if n != 1 else ''}")
    return (f"Dreaming {day}: {total} memory question{'s' if total != 1 else ''} wait for the person "
            f"({', '.join(parts)}). If they want to go through them: /agent-dreaming:dream in Claude Code, "
            f"$dreaming in Codex. Do not bring it up again in this session unless asked.")
