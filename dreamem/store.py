"""holographic-memory's fact store, read-only (``mode=ro``): versions, origins, tombstones. Never written here."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

ORIGINS = ("said", "confirmed", "inferred")


def connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=5)


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"pragma table_info({table})")}


def facts_state(path: Path | None, ids) -> dict[int, dict]:
    """{fact_id: {version_id, content, origin, status}} of each fact's head version — the newest that is not a
    correction, as holographic-memory checks ``expect`` against: ``current``, or ``ended`` for a fact that no longer
    holds — id and text from one statement; origin as the engine reads it (a confirmation turns an inference or an
    unknown origin into ``confirmed``). Empty when the store is missing or older."""
    ids = sorted({int(i) for i in ids if str(i).isdigit()})
    if path is None or not ids or not Path(path).exists():
        return {}
    out: dict[int, dict] = {}
    conn = connect(path)
    try:
        cols = _columns(conn, "fact_versions")
        if not cols:
            return {}
        marks = ",".join("?" * len(ids))
        origin = "src_origin" if "src_origin" in cols else "null"
        for fid, vid, text, org, status in conn.execute(
                f"select fact_id, version_id, content, {origin}, status from fact_versions "
                f"where status != 'corrected' and fact_id in ({marks}) order by fact_id, seq desc, version_id desc",
                ids):
            if int(fid) not in out:
                out[int(fid)] = {"version_id": vid, "content": text, "origin": org if org in ORIGINS else None,
                                 "status": status}
        if _columns(conn, "fact_confirmations"):
            for (fid,) in conn.execute(f"select distinct fact_id from fact_confirmations where fact_id in ({marks})", ids):
                rec = out.setdefault(int(fid), {"version_id": None, "content": None, "origin": None, "status": None})
                if rec["origin"] in (None, "inferred"):
                    rec["origin"] = "confirmed"
    except sqlite3.Error:
        return out
    finally:
        conn.close()
    return out


def fact_text(path, fact_id) -> str | None:
    """The current text of *fact_id*, or None (no store, no such fact)."""
    if not path or not str(fact_id).isdigit() or not Path(path).exists():
        return None
    conn = connect(Path(path))
    try:
        row = conn.execute("select content from facts where fact_id = ?", (int(fact_id),)).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def forgotten(path: Path | None) -> set[int]:
    if path is None or not Path(path).exists():
        return set()
    conn = connect(path)
    try:
        if not _columns(conn, "forgotten"):
            return set()
        return {int(r[0]) for r in conn.execute("select fact_id from forgotten")}
    except sqlite3.Error:
        return set()
    finally:
        conn.close()


# The outcome journal (holographic-memory 0.4+, ``experience = true``): which facts a recall or search offered, at which
# version, and what the agent reported after. A report is the agent's, not the person's: it confirms nothing and changes
# nothing here, it only raises a question for the person.
WENT_WRONG = ("corrected", "dead_end")
# Reasons that say something about the fact's text. wrong_entity, irrelevant, missing_evidence and answer_error are
# about the search or the answer, not about whether the fact still holds.
ABOUT_THE_FACT = ("stale_version", "other")
OFFERED_TIMES = 2  # a fact no correction names is asked about once it was offered this many times when it went wrong


def _refs(value) -> list[tuple[int, int]]:
    out = []
    for r in value if isinstance(value, list) else []:
        if isinstance(r, dict) and type(r.get("fact_id")) is int and type(r.get("version_id")) is int:
            out.append((r["fact_id"], r["version_id"]))
    return out


def went_wrong(path: Path | None) -> list[dict]:
    """Facts the journal holds against: each ``{fact_id, version_id, events, named, attempts, last_at, correction}``
    — named by the correction of an outcome that went wrong for a reason about the fact, or offered in
    OFFERED_TIMES such attempts. ``version_id`` is the version those answers saw (the newest); ``correction`` the newest
    correction line. Only outcomes still active (not revised by a later one) count. Empty without a journal."""
    if path is None or not Path(path).exists():
        return []
    conn = connect(path)
    try:
        if not (_columns(conn, "memory_outcomes") and _columns(conn, "memory_attempts")):
            return []
        rows = conn.execute("select o.event_id, o.attempt_id, o.created_at, o.payload, a.emitted "
                            "from memory_outcomes o join memory_attempts a on a.attempt_id = o.attempt_id "
                            "order by o.created_at").fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    events = []
    for event_id, attempt_id, created_at, payload, emitted in rows:
        try:
            p, e = json.loads(payload), json.loads(emitted)
        except (TypeError, ValueError):
            continue
        if isinstance(p, dict):
            events.append((str(event_id), str(attempt_id), float(created_at or 0), p, e))
    revised = {str(p.get("supersedes")) for *_, p, _ in events if p.get("supersedes")}
    facts: dict[int, dict] = {}
    for event_id, attempt_id, at, p, emitted in events:
        if event_id in revised or p.get("outcome") not in WENT_WRONG or p.get("reason") not in ABOUT_THE_FACT:
            continue
        named = _refs(p.get("correction_refs"))
        for fid, vid in named + [r for r in _refs(emitted) if r[0] not in {n[0] for n in named}]:
            rec = facts.setdefault(fid, {"fact_id": fid, "version_id": vid, "events": [], "named": False,
                                         "attempts": set(), "last_at": at, "correction": None})
            if event_id not in rec["events"]:
                rec["events"].append(event_id)
            rec["attempts"].add(attempt_id)
            rec["named"] = rec["named"] or (fid, vid) in named
            if at >= rec["last_at"]:
                rec["version_id"], rec["last_at"] = vid, at
                if isinstance(p.get("correction"), str) and p["correction"].strip():
                    rec["correction"] = p["correction"].strip()
    out = []
    for rec in facts.values():
        if rec["named"] or len(rec["attempts"]) >= OFFERED_TIMES:
            out.append({**rec, "attempts": len(rec["attempts"])})
    return sorted(out, key=lambda r: -r["last_at"])
