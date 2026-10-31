"""Candidate new facts: a clause where the person says something lasting about themselves or their people, which
memory does not hold yet. The pass finds them with a wide filter; the agent of the review judges each one and asks
the person about at most one (plan 2026-10-31-new-fact-candidates).

Nothing of the person's words is kept for it. A candidate is a reference: which session file, and keyed digests
(HMAC-SHA256 with a salt of this install, ``~/.dreaming/salt``) of the message's identity and of its text. When the
review shows it, the message is read again from the CLI's own session file and both digests must match — a line
rewritten since, a file gone, a session moved beyond reach: the candidate is gone, nothing is shown.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets

from .paths import Homes
from .sources import Message
from .sources import claude_code as src_claude
from .sources import codex as src_codex

SALT_FILE = "salt"


def salt(homes: Homes) -> bytes:
    """This install's key for the digests: created once, readable by the user only, never leaves ``~/.dreaming``."""
    path = homes.dreaming / SALT_FILE
    try:
        raw = path.read_bytes()
        if len(raw) >= 32:
            return raw
    except FileNotFoundError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(SALT_FILE + ".partial")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(secrets.token_bytes(32))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    return path.read_bytes()


def mac(key: bytes, text: str) -> str:
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()


def reference(key: bytes, m: Message) -> dict:
    """What is kept of a message: no word of it (a version number at most, for an old Claude Code line)."""
    ref = {"client": m.client, "source": m.source, "ts": m.ts, "id": mac(key, m.key), "text": mac(key, m.text)}
    if m.below:
        ref["below"] = m.below
    return ref


def resolve(homes: Homes, key: bytes, ref: dict) -> Message | None:
    """The message *ref* names, read again now, or None when it is no longer there as it was."""
    try:
        source, since = str(ref["source"]), float(ref.get("ts") or 0) - 1
        below = tuple(int(x) for x in str(ref["below"]).split(".")) if ref.get("below") else None
    except (TypeError, ValueError, KeyError):  # a reference this version did not write: nothing to find
        return None
    if not source:
        return None
    if ref.get("client") == src_claude.CLIENT:
        msgs, _ = src_claude.read(homes.claude_projects, since, only={source}, boundary=below)
    elif ref.get("client") == src_codex.CLIENT:
        msgs, _ = src_codex.read(homes.codex_session_dirs(), since, stems={source})
    else:
        return None
    for m in msgs:
        if hmac.compare_digest(mac(key, m.key), str(ref.get("id"))):
            return m if hmac.compare_digest(mac(key, m.text), str(ref.get("text"))) else None
    return None


# -- the wide filter (measured 2026-10-31: with the review agent judging after it, 86 % precision on a blind
#    synthetic persona and 7 of 7 on a real home; the rules below are that measured version, change them only with a
#    new measurement) ----------------------------------------------------------------------------------------------

SENT_SPLIT = re.compile(r"(?<=[.!?…])\s+|\n+")
# a sentence often joins a lasting part and today's: "we call on Fridays, but today it fell through"
CLAUSE_SPLIT = re.compile(r",\s+(?:а|но|хотя|правда|зато|but|though)\s+|\s+[—–]\s+|;\s+", re.I)

# attributed to the person: first person, "my people", rules and habits
SELF = re.compile(
    r"\b(я|мы|мне|меня|мной|у меня|у нас|мой|моя|моё|мое|мои|моего|моей|моим|моих|наш|наша|наше|наши|"
    r"всегда|никогда|обычно|каждый день|каждое утро|каждую|каждый|по утрам|по вечерам|по выходным)\b", re.I)
SELF_EN = re.compile(r"\b(i|i'm|i've|my|we|our|always|never|usually|every (day|morning|week))\b", re.I)
KIN = re.compile(
    r"\b(муж\w*|жен[аыуе]|жене|сын\w*|доч\w*|мам\w*|пап\w*|свекров\w*|св[её]кр\w*|тёщ\w*|тещ\w*|бабушк\w*|"
    r"дедушк\w*|брат\w*|сестр\w*|реб[её]н\w*|дет(и|ей|ям)|кот|кота|коту|котом|кошк\w*|собак\w*|"
    r"husband|wife|son|daughter|mom|mum|dad|kids?|cat|dog)\b", re.I)

TEMPORARY = re.compile(
    r"\b(сегодня|сейчас|вчера|позавчера|утром|вечером|днём|днем|только что|на этой неделе|на днях|завтра|"
    r"упал|упала|упало|сломал\w*|не работает|билд|деплой|созвон|встреч[аеу]|пробк\w*|"
    r"устал\w*|today|yesterday|tonight|this morning|right now|tomorrow)\b", re.I)
HYPOTHETICAL = re.compile(r"\b(если бы|бы|допустим|представь|if i|i would|i'd)\b", re.I)
COMMAND = re.compile(
    r"(^|\b)(сделай|напиши|исправь|почини|переведи|отправь|составь|запланируй|найди|проверь|посмотри|"
    r"подскажи|помоги|давай|можешь|могла? бы ты|можно ли|нужно,? чтобы ты|хорошо бы|было бы здорово|"
    r"напомни|удали|посоветуй|скопируй|отмени|добавь|убери|покажи|объясни|сократи|перепиши|поставь|"
    r"не отправляй|не пиши|не делай|сохрани|распечатай|прогони|"
    r"please|can you|could you|write|fix|send|translate|make|check|summarize|explain|list|remind|show|tell me)\b",
    re.I)
OPINION = re.compile(r"\b(считаю|кажется|думаю|по-моему|реально|i think|i feel|imo)\b", re.I)
QUESTION_START = re.compile(r"^\W*((и|а|но)\s+)?(вопрос|почему|зачем|что|как|где|когда|какой|какая|какие|сколько|"
                            r"why|what|how|where|when|which|is it|do i|should)\b", re.I)
EVENT_WORDS = re.compile(r"\b(опять|снова|пока я|третий день|с обеда)\b", re.I)
NOISE = re.compile(r"(https?://|`|\b\w+\.(py|js|ts|md|json|txt)\b|[\\/]\w+[\\/]|Traceback|Error:|^\s*>|\"|"
                   r"\[\w+\]|map\[)")
NAME = re.compile(r"\b[А-ЯЁA-Z][а-яёa-z]{2,}")


def clauses(text: str):
    for sent in SENT_SPLIT.split(text):
        for s in CLAUSE_SPLIT.split(sent):
            s = s.strip(" ,.;:—–")
            if 12 <= len(s) <= 300:
                yield s


# A pasted document — a brief, a spec, a file attached — is not the person telling about their life (on a real home
# such clauses were most of the repeated candidates; on two synthetic persons no real fact came from one).
ATTACHED = re.compile(r"""(^|\s)@(["']?[A-Za-z]:[\\/]|\.{0,2}[\\/]|["'][^"']+\.\w{2,4})""")
PASTED_LINES = 8  # non-empty lines from which a message reads as a pasted document
PASTED_REPEATS = 3  # the same clause, word for word, in this many messages: pasted again and again


def pasted(text: str) -> bool:
    return bool(ATTACHED.search(text)) or sum(1 for ln in text.splitlines() if ln.strip()) >= PASTED_LINES


def _words(s: str) -> set[str]:
    return {w[:5] for w in re.findall(r"\w+", s.lower()) if len(w) > 3}


def _passes(s: str, names: frozenset) -> bool:
    """The wide filter: away go code and quotes, clauses about nobody of the person's, and — when it is the first
    thing the clause shows — a question or a command. A temporary, hypothetical or opinionated clause stays: the
    review agent judges those (rules alone stopped near half precision there)."""
    if "?" in s or NOISE.search(s):
        return False
    mentioned = {w.lower()[:4] for w in NAME.findall(s)}
    if not (SELF.search(s) or SELF_EN.search(s) or KIN.search(s) or mentioned & names):
        return False
    for rx in (TEMPORARY, HYPOTHETICAL, COMMAND, OPINION, QUESTION_START, EVENT_WORDS):
        if rx.search(s):
            return rx not in (COMMAND, QUESTION_START)
    return True


def exclusions(settings: dict) -> list:
    """The person's ``exclude_patterns`` as the engine reads them (case-insensitive; a broken one is skipped)."""
    out = []
    for pat in settings.get("exclude_patterns") or []:
        try:
            out.append(re.compile(str(pat), re.IGNORECASE))
        except re.error:
            continue
    return out


def known_names(facts: list[str]) -> frozenset:
    """The person's people as memory knows them: capitalised words inside its facts, by their first four letters."""
    return frozenset(w.lower()[:4] for f in facts for w in NAME.findall(f[1:]))


def find(messages: list[Message], facts: list[str], key: bytes) -> list[dict]:
    """Every candidate of *messages*, newest first: ``{ref, clause, days}`` — the message's reference, a digest of the
    clause, the number of days the same clause came back. No words. Newest, not most repeated: on a real home the
    clauses said on many days were pasted briefs, and every fact the pilot found was said once — ranked by repetition,
    a night's ten would have held them back for weeks."""
    known = [w for w in (_words(f) for f in facts) if w]
    names = known_names(facts)
    messages = [m for m in messages if not pasted(m.text)]
    repeats: dict[str, int] = {}
    for m in messages:
        for s in set(clauses(m.text)):
            repeats[s] = repeats.get(s, 0) + 1
    found: dict[str, dict] = {}
    for m in messages:
        for s in clauses(m.text):
            if repeats[s] >= PASTED_REPEATS or not _passes(s, names):
                continue
            ws = _words(s)
            if any(len(ws & k) / max(1, min(len(ws), len(k))) >= 0.6 for k in known):
                continue  # memory holds it already, by its words (the agent checks again with memory_search)
            c = found.setdefault(" ".join(sorted(ws)), {"m": m, "clause": s, "days": set()})
            c["days"].add(int(m.ts // 86400))
            if m.ts > c["m"].ts:
                c.update(m=m, clause=s)
    ranked = sorted(found.values(), key=lambda c: -c["m"].ts)
    return [{"ref": reference(key, c["m"]), "clause": mac(key, c["clause"]), "days": len(c["days"])} for c in ranked]


def clause_of(homes: Homes, key: bytes, item: dict) -> tuple[str, str] | None:
    """(clause, its message) of a candidate, read again now — or None when the message is gone or changed, or the
    clause is no longer in it."""
    m = resolve(homes, key, item.get("ref") or {})
    if m is None:
        return None
    for s in clauses(m.text):
        if hmac.compare_digest(mac(key, s), str(item.get("clause"))):
            return s, m.text
    return None
