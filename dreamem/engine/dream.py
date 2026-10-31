"""dream.py — the dreaming engine of agent-dreaming: a deterministic, read-only review of an agent's memory.

The caller hands everything in (``night()``): the person's messages, the memory notes, the fact store's path, the
answers given to the previous night, the state files. The engine reads the fact store read-only and writes only its
own state files; it reads no environment variable, no config file and no agent home of its own.

Phases:
  light — facts of the store and the person's messages of the corroboration window
  rem   — recurring themes, facts nobody uses, notes nobody mentions, conflicts between facts and memory
  deep  — score facts by corroboration in real conversations; duplicates across the store

Corroboration: for every fact and memory note we count on how many DIFFERENT days the person's messages actually
mention it (by signature tokens and stems) — an honest query diversity, frequency and "recency by mention" without
a query log.
"""
from __future__ import annotations
import bisect
import contextvars
import copy
import functools
import hashlib
import heapq
import importlib.util
import json
import math
import os
import re
import sqlite3
import sys
import time
import types
import unicodedata
from array import array
from collections import Counter, defaultdict, namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    # IANA timezone for the diary date and for bucketing messages into "days".
    "timezone": "UTC",
    # Regexes; a message whose text matches any of them never corroborates.
    "exclude_patterns": [],
    # Names the agent is addressed by — pure noise for corroboration.
    "agent_names": [],
    # Extra stopwords on top of the built-in lists.
    "extra_stopwords": [],
    # Minimal token length: ASCII/Latin tokens vs everything else (Cyrillic inflection makes short tokens noisy).
    "token_min_len": {"latin": 4, "other": 5},
    # "holographic": facts come from a holographic-memory store (`store`); "none": the memory notes alone.
    "fact_source": "holographic",
    # The fact store's path — the very database the memory tools of a session open.
    "store": "",
    # Word stems that hint a fact is about the person / household. Categories are matched exactly.
    "profile_hint_terms": ["prefer", "likes", "dislikes", "goal", "plans", "works", "lives",
                           "family", "spouse", "partner", "child", "daughter", "son",
                           "предпочита", "любит", "не любит", "цель", "планирует",
                           "работает", "живёт", "живет", "семь", "родител", "дочь", "сын", "мама", "папа"],
    "profile_categories": ["user", "user_pref", "preference", "profile", "personal", "family"],
    # Declarative near-duplicate rules (see `_semantic_memory_alias`).
    "alias_rules": [],
    "diary": {"heading": "## Dream", "keep_sections": 90},
    # A short rest for a promotion just shown; a fact a human rejected keeps its trust in the store, and
    # `trust_feedback` offers the one call that teaches recall (`memory_feedback unhelpful`). The floor is the
    # store's own `min_trust_threshold` (0.3 by default): below it recall ignores the fact.
    "gates": {"min_score": 0.55, "min_mentions": 3, "new_facts_cap": 30,
              "promotion_cooldown_days": 3,
              "feedback_trust_floor": None, "feedback_cap": 5,
              "seen_cooldown_days": 14, "md_ask_cooldown_days": 14,
              # A note that survived a "still relevant?" question is not asked about again for this long.
              "md_confirmed_cooldown_days": 90,
              "publish_cap": 15},
    "weights": {"relevance": 0.30, "frequency": 0.24, "query_diversity": 0.15,
                "recency": 0.15, "consolidation": 0.10, "conceptual_richness": 0.06},
    # Markers of pinned notes: never flagged as stale.
    "pinned_markers": ["📌", "[pinned]"],
    # Phrases that must stay in memory; one that disappears raises an alert.
    "probes": [],
    # A rejected fact is asked about once more when the rejection is older than this and it was said again since.
    "reject_reask_days": 180,
    # Loss guard: more than this share of yesterday's notes gone raises an alert.
    "memory_loss_alert_fraction": 0.25,
    # Facts without keywords (`memory_update keywords`): off — a write the person did not ask for.
    "keywords": {"enabled": False, "cap": 10},
    # Duplicates across the whole store: one "merge?" question a night with both texts whole and a number (`D-17`);
    # the merge is made by the agent on a "yes" to that number, with the versions the person saw.
    "duplicates": {"enabled": True, "min_containment": 0.7, "per_night": 1, "question_cooldown_days": 180,
                   "max_pair_checks": 200000, "max_chars": 2000},
    # What one pass keeps of what it read from the window's messages, in MiB (see references/scoring.md).
    "corroboration": {"memory_mb": 256},
    # Recurring themes of the window's messages (`emerging_themes`): about half of a night's time; off for a caller
    # that asks nothing about them.
    "themes": True,
}

_UTC_NAMES = {"utc", "etc/utc", "gmt", "etc/gmt", "z", "zulu", "universal", "etc/universal"}


def _deep_merge(base, override):
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


FACT_ID_RE = re.compile(r"\d+")
FACT_SOURCES = ("holographic", "none")


def messages_for(fact, messages, by_store=None):
    """The messages that may corroborate *fact* (one pool: the person's own words)."""
    return by_store.get(None, []) if by_store is not None else messages


def messages_by_store(messages):
    return {None: list(messages)}


def fact_source():
    src = str(SETTINGS.CONFIG.get("fact_source") or "holographic").strip().lower()
    if src not in FACT_SOURCES:
        raise ValueError(f"fact_source {src!r} is not one of {', '.join(FACT_SOURCES)}")
    return src


def fact_store_path():
    """The fact store's path, or None (no store: the notes alone)."""
    raw = SETTINGS.CONFIG.get("store")
    return str(raw) if raw and fact_source() == "holographic" else None


def same_file(a, b):
    """Whether two paths name one file: the same file on disk when both exist, else the same resolved path."""
    if not a or not b:
        return False
    try:
        if os.path.exists(a) and os.path.exists(b):
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _tz(name):
    if not name or str(name).strip().lower() in _UTC_NAMES:
        return timezone.utc
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 — unknown zone → UTC with a warning
            print(f"[dream] warn: unknown timezone {name!r}, using UTC (is tzdata installed?)", file=sys.stderr)
    return timezone.utc


# The effective settings, one object: configure() builds a whole Settings and swaps it in; code reads
# `SETTINGS.MIN_SCORE`, so where a value comes from is visible.
_SETTING_NAMES = (
    "CONFIG", "LOCAL_TZ", "WEIGHTS", "MIN_SCORE", "MIN_MENTIONS", "NEW_FACTS_CAP", "PUBLISH_CAP",
    "MD_ASK_COOLDOWN_DAYS", "MD_CONFIRMED_COOLDOWN_DAYS", "SEEN_COOLDOWN_DAYS", "EXCLUDE_PATTERNS",
    "STOPWORDS", "TOKEN_MIN_LATIN", "TOKEN_MIN_OTHER", "ALIAS_RULES",
    "PROFILE_HINT_RE", "PROFILE_CATEGORIES", "DIARY_HEADING", "DIARY_SECTION_RE", "DIARY_KEEP_SECTIONS",
    "PINNED_MARKERS", "PROBES", "REJECT_REASK_DAYS", "MEMORY_LOSS_ALERT_FRACTION",
    "PROMOTION_COOLDOWN_DAYS", "FEEDBACK_TRUST_FLOOR", "FEEDBACK_CAP", "KEYWORDS_ENABLED", "KEYWORDS_CAP",
    "DUP_ENABLED", "DUP_MIN_CONTAINMENT", "DUP_PER_NIGHT", "DUP_COOLDOWN_DAYS", "DUP_MAX_PAIR_CHECKS",
    "DUP_MAX_CHARS", "PASS_MEMORY_MB", "THEMES",
)


class Settings:
    """The effective settings of one config (see configure()). Slots: a misspelt name raises instead of adding a
    setting nobody reads."""
    __slots__ = _SETTING_NAMES


SETTINGS_GENERATION = 0  # bumped by configure(): what is cached by the settings is cached by this


def _number(cfg, key, default, cast, low=None, high=None):
    """One setting, one parse: a bad value falls back to its default with a warning and touches nothing else."""
    raw = cfg.get(key, default)
    try:
        value = cast(raw)
        if isinstance(raw, bool) or value != value or (low is not None and value < low) \
                or (high is not None and value > high):
            raise ValueError(raw)
        return value
    except (TypeError, ValueError):
        print(f"[dream] warn: {key}={raw!r} ignored — {default!r} used", file=sys.stderr)
        return default


def configure(cfg):
    """Build the Settings of *cfg* (DEFAULT_CONFIG merged with the caller's) and make them the module's. The whole
    object is swapped in at the end: a config that fails half-way leaves the previous settings as they were."""
    global SETTINGS, SETTINGS_GENERATION
    cfg = _deep_merge(DEFAULT_CONFIG, cfg or {})
    s = Settings()
    s.CONFIG = cfg
    s.LOCAL_TZ = _tz(cfg.get("timezone"))
    s.WEIGHTS = dict(DEFAULT_CONFIG["weights"], **(cfg.get("weights") or {}))
    gates = cfg.get("gates") or {}
    s.MIN_SCORE = _number(gates, "min_score", 0.55, float, 0.0, 1.0)
    s.MIN_MENTIONS = _number(gates, "min_mentions", 3, int, 1)
    s.NEW_FACTS_CAP = _number(gates, "new_facts_cap", 30, int, 0)
    s.PUBLISH_CAP = _number(gates, "publish_cap", 15, int, 1)
    s.MD_ASK_COOLDOWN_DAYS = _number(gates, "md_ask_cooldown_days", 14, int, 0)
    s.MD_CONFIRMED_COOLDOWN_DAYS = _number(gates, "md_confirmed_cooldown_days", 90, int, 0)
    s.PROMOTION_COOLDOWN_DAYS = _number(gates, "promotion_cooldown_days", 3, int, 0)
    s.SEEN_COOLDOWN_DAYS = _number(gates, "seen_cooldown_days", 14, int, 0)
    floor = gates.get("feedback_trust_floor")
    s.FEEDBACK_TRUST_FLOOR = 0.3 if floor is None else _number(gates, "feedback_trust_floor", 0.3, float, 0.0, 1.0)
    s.FEEDBACK_CAP = _number(gates, "feedback_cap", 5, int, 0)
    kw = cfg.get("keywords") if isinstance(cfg.get("keywords"), dict) else {}
    s.KEYWORDS_ENABLED = kw.get("enabled") is True
    s.KEYWORDS_CAP = _number(kw, "cap", 10, int, 0)
    dup = cfg.get("duplicates") if isinstance(cfg.get("duplicates"), dict) else {}
    defaults = DEFAULT_CONFIG["duplicates"]
    s.DUP_ENABLED = dup.get("enabled", True) is not False
    s.DUP_MIN_CONTAINMENT = _number(dup, "min_containment", defaults["min_containment"], float, 0.0, 1.0)
    s.DUP_PER_NIGHT = _number(dup, "per_night", defaults["per_night"], int, 0)
    s.DUP_COOLDOWN_DAYS = _number(dup, "question_cooldown_days", defaults["question_cooldown_days"], int, 0)
    s.DUP_MAX_PAIR_CHECKS = _number(dup, "max_pair_checks", defaults["max_pair_checks"], int, 1)
    s.DUP_MAX_CHARS = _number(dup, "max_chars", defaults["max_chars"], int, 1)
    corr = cfg.get("corroboration") if isinstance(cfg.get("corroboration"), dict) else {}
    s.PASS_MEMORY_MB = _number(corr, "memory_mb", 256, float, 1.0, 1e9)
    s.THEMES = cfg.get("themes", True) is not False
    s.EXCLUDE_PATTERNS = []
    for pat in (cfg.get("exclude_patterns") or []):
        try:
            s.EXCLUDE_PATTERNS.append(re.compile(str(pat), re.IGNORECASE))
        except re.error as exc:
            print(f"[dream] warn: exclude_patterns {pat!r} ignored: {exc}", file=sys.stderr)
    s.STOPWORDS = set(RU_STOP) | set(EN_STOP) | OTHER_STOP | OTHER_TEMPORAL_STOP | OTHER_POLARITY_STOP
    s.STOPWORDS |= {_norm(w) for w in (cfg.get("agent_names") or []) if w}
    s.STOPWORDS |= {_norm(w) for w in (cfg.get("extra_stopwords") or []) if w}
    tml = cfg.get("token_min_len") or {}
    s.TOKEN_MIN_LATIN = _number(tml, "latin", 4, int, 1)
    s.TOKEN_MIN_OTHER = _number(tml, "other", 5, int, 1)
    s.ALIAS_RULES = [r for r in (cfg.get("alias_rules") or []) if isinstance(r, dict)]
    terms = [t for t in (cfg.get("profile_hint_terms") or []) if t]
    s.PROFILE_HINT_RE = re.compile(
        r"(?<!\w)(?:" + "|".join(re.escape(_norm(t)) for t in terms) + r")",
        re.IGNORECASE) if terms else None
    s.PROFILE_CATEGORIES = {_norm(c) for c in (cfg.get("profile_categories") or [])}
    diary = cfg.get("diary") or {}
    s.DIARY_HEADING = str(diary.get("heading") or "## Dream").strip()
    s.DIARY_SECTION_RE = re.compile(rf"^{re.escape(s.DIARY_HEADING)} \d{{4}}-\d{{2}}-\d{{2}}", re.MULTILINE)
    s.DIARY_KEEP_SECTIONS = _number(diary, "keep_sections", 90, int, 1)
    s.PINNED_MARKERS = [str(m) for m in (cfg.get("pinned_markers") or []) if m]
    s.PROBES = [str(m).strip() for m in (cfg.get("probes") or []) if str(m or "").strip()]
    s.REJECT_REASK_DAYS = _number(cfg, "reject_reask_days", 180, int, 0)
    s.MEMORY_LOSS_ALERT_FRACTION = _number(cfg, "memory_loss_alert_fraction", 0.25, float, 0.01, 1.0)
    SETTINGS = s
    SETTINGS_GENERATION += 1


# Threshold of stemmed containment above which a rewording counts as the same
# fact (shared by memory dedupe and the reject list).
REJECT_MATCH_THRESHOLD = 0.62


def _load_lang(path=None):
    """dream_lang.py next to this file: the word lists and patterns of every language the pass reads. Unlike
    holo_screen.py and dream_yaml.py it has no stand-in — without it the pass cannot tell a word from a stopword —
    so a missing or broken copy fails the import, and the gates report it as dream_error."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "dream_lang.py")
    spec = importlib.util.spec_from_file_location("_dream_lang", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_LANG = _load_lang()
# from dream_lang.py (3.0)
(RU_STOP, RU_TEMPORAL_STOP, RU_POLARITY_STOP, EN_STOP, OTHER_STOP, OTHER_TEMPORAL_STOP, OTHER_POLARITY_STOP,
 TEMPORAL_STOP, POLARITY_STOP) = (
    _LANG.RU_STOP, _LANG.RU_TEMPORAL_STOP, _LANG.RU_POLARITY_STOP, _LANG.EN_STOP, _LANG.OTHER_STOP,
    _LANG.OTHER_TEMPORAL_STOP, _LANG.OTHER_POLARITY_STOP, _LANG.TEMPORAL_STOP, _LANG.POLARITY_STOP)


def _conn(path):
    """The fact store, read-only (a URI, so a path with '#' or '?' in it is still a path)."""
    return sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=5)


def _now():
    return datetime.now(timezone.utc)


def _parse_ts(s):
    try:
        return datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _fold(s):
    """Unicode NFC plus ё→е.

    The same word arrives in different shapes: "ёлка"/"елка" are two strings,
    and a decomposed "й" (и + U+0306) is a third one after a copy-paste out of
    a browser. Both broke comparison silently — the tokens simply did not meet
    (external review, 2026-10-07)."""
    return unicodedata.normalize("NFC", s or "").replace("ё", "е").replace("Ё", "Е")


def _norm(s):
    return re.sub(r"\s+", " ", _fold(s).lower()).strip()


# ---------------------------------------------------------------------------
# Tokenization
# ---------------------------------------------------------------------------

# Reply quote: [Replying to: "…"] — the quoted text is unwrapped, not cut.
REPLY_QUOTE_RE = re.compile(r'\[replying\s+to:?\s*[«"“]?(.*?)[»"”]?\s*\]',
                            re.IGNORECASE | re.DOTALL)
# Voice message: [The user sent a voice message~ Here's what they said: "…"].
# The transcript is human speech and must be unwrapped like a reply quote:
# wrapped in brackets it yielded 0 tokens, i.e. voice messages (78 of 1103 in
# 60 days on live data) corroborated nothing. Image descriptions (`sent an
# image`) deliberately stay metadata: that is the model's retelling, not words
# of a human.
VOICE_QUOTE_RE = re.compile(
    r'\[the user sent a voice message[^:\]]*:\s*[«"“]?(.*?)[»"”]?\s*\]',
    re.IGNORECASE | re.DOTALL)

# Harness envelopes: context-compaction banner and skill-body injection. They
# are role=user in history but contain no human text — big English blocks that
# any English fact latches onto through generic words ("context"+"unless").
# Live case: a dead fact survived 34 nights on 30 "mentions in 12 days" — all
# 30 were compaction banners.
HARNESS_ENVELOPE_RE = re.compile(
    r"^\s*\[(?:context\s+compaction|important:\s*the\s+user\s+has\s+invoked)",
    re.IGNORECASE)
# Service text the harness persists as an ordinary message. Matched ANYWHERE in
# the text, not just at the start: the core glues the todo snapshot into a real
# reply, and a failure notice can be quoted (exact strings from Hermes 0.21.5 —
# tools/todo_tool.py:21, agent/turn_failure_copy.py:39-45,208).
HARNESS_INSERTS = (
    "[Your active task list was preserved across context compression]",
    "Your request was not processed. Send it again if you still want me to carry it out.",
    "This turn did not complete. Some actions may already have run; verify their effects",
    "Operation interrupted: waiting for model response (",
    "Operation interrupted.",
)


def is_harness_envelope(text):
    """True for harness service inserts that are not human speech."""
    raw = text or ""
    if HARNESS_ENVELOPE_RE.match(raw):
        return True
    return any(ins in raw for ins in HARNESS_INSERTS)


# A URL is a locator, not speech, and has no place in corroboration. It broke
# it from both sides: tracking params (`fbclid`, `tn__`, `cft__`) longer than 8
# chars became "distinctive" tokens where ONE match suffices; and host+path do
# not help either — two DIFFERENT links share `facebook`+`posts`, i.e. an
# overlap ≥2 and a false mention. A bare URL now yields 0 tokens (a link alone
# is not a durable fact), words around it are untouched.
URL_RE = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)


def strip_urls(text):
    return URL_RE.sub(" ", text or "")


TOKEN_RE = re.compile(r"[^\W\d_][\w\-]*", re.UNICODE)

# from dream_lang.py (3.0)
(_HAN_KANA, _HANGUL, _SEA, UNSEGMENTED_RE, _SEA_RE, _HIRAGANA_RE, _HAN_RE, CJK_FUNCTION, TH_FUNCTION, FILLER_RUNS) = (
    _LANG._HAN_KANA, _LANG._HANGUL, _LANG._SEA, _LANG.UNSEGMENTED_RE, _LANG._SEA_RE, _LANG._HIRAGANA_RE,
    _LANG._HAN_RE, _LANG.CJK_FUNCTION, _LANG.TH_FUNCTION, _LANG.FILLER_RUNS)


def is_gram(token):
    """True for an n-gram of unsegmented script (see UNSEGMENTED_RE), False for a word."""
    return bool(token) and UNSEGMENTED_RE.match(token[0]) is not None


def gram_weight(gram):
    """How much one shared n-gram is worth: 1, or ½ with a function character / all kana, 0 for no topic."""
    if _SEA_RE.match(gram):
        return 0.0 if any(gram in w for w in TH_FUNCTION) else 1.0
    hits = sum(c in CJK_FUNCTION for c in gram)
    if hits == len(gram):  # before the kana rule: には, ので are particles, not half a word (review 2026-10-13)
        return 0.0
    return 0.5 if hits or _HIRAGANA_RE.fullmatch(gram) else 1.0


def run_grams(run):
    """Content n-grams of one unsegmented run; a run of one character (a lone 猫 between Latin words) gives none."""
    if run in FILLER_RUNS:
        return set()
    n = 3 if _SEA_RE.match(run) else 2
    if len(run) < n:
        return set()
    return {g for g in (run[i:i + n] for i in range(len(run) - n + 1)) if gram_weight(g) > 0}


# The wide tokenizer (3.2.0) keeps words of three letters and more: a fact of short words («Гоша пьёт кофе по
# утрам», "Bob likes tea") had one token or none under the usual minimum, and nothing ever mentioned it (Laya
# experiment, 2026-10-22). Only such facts are read wide, and the messages are read wide only against them; the
# short function words the usual minimum used to drop are WIDE_STOP.
WIDE_TOKEN_MIN = 3
WIDE_STOP = _LANG.WIDE_STOP


def _keep_token(tok, wide=False):
    if tok in SETTINGS.STOPWORDS:
        return False
    if wide:
        return len(tok) >= WIDE_TOKEN_MIN and tok not in WIDE_STOP
    if tok.isascii():
        return len(tok) >= SETTINGS.TOKEN_MIN_LATIN
    return len(tok) >= SETTINGS.TOKEN_MIN_OTHER


def _raw_tokens(text, wide=False):
    """Significant tokens of an already unwrapped string (brackets are metadata): words, and the n-grams of the
    runs of unsegmented script (cut out first, so "ACME的老板" gives the word "acme" and the pair "老板")."""
    low = re.sub(r"\[[^\]]*\]", " ", strip_urls(_fold(text))).lower()
    grams = set()
    for run in UNSEGMENTED_RE.findall(low):
        grams |= run_grams(run)
    words = {w for w in TOKEN_RE.findall(UNSEGMENTED_RE.sub(" ", low)) if _keep_token(w, wide)}
    return words | (grams - SETTINGS.STOPWORDS)


def sig_tokens(text, wide=False):
    """Signature tokens: Unicode words without stopwords (Latin ≥4, other ≥5 chars; with *wide*, ≥3 — see
    WIDE_TOKEN_MIN).

    Reply quotes and voice transcripts are unwrapped BEFORE bracket blocks are
    cut: a family reply to a message about an entry is a mention of the entry
    (otherwise a short "yes, still Vespa" gives 0 tokens and the dream asks the
    same thing every night). Other [..] blocks (sender tags, [id=N], image
    descriptions) are still cut as metadata."""
    raw = text or ""
    # A reply quote counts only when the person added words of their own. A bare
    # "ок" under a quoted cron report used to corroborate everything the report
    # mentioned — including the entries it proposed to drop (external review,
    # 2026-10-07). The live case that put the unwrapping here ("Да, всё ещё
    # Vespa") keeps working: its tail has its own tokens.
    tail = VOICE_QUOTE_RE.sub(" ", REPLY_QUOTE_RE.sub(" ", raw))
    tail_tokens = _raw_tokens(tail, wide)
    quoted = " ".join(m.group(1) for m in REPLY_QUOTE_RE.finditer(raw))
    if not tail_tokens and quoted:
        return set()
    if quoted and MACHINE_QUOTE_RE.search(quoted):
        # Quoting a machine (a cron report, a script block) is not speech about
        # the subject, however much the person adds around it.
        return tail_tokens
    unwrapped = REPLY_QUOTE_RE.sub(lambda m: " " + m.group(1) + " ", raw)
    unwrapped = VOICE_QUOTE_RE.sub(lambda m: " " + m.group(1) + " ", unwrapped)
    return _raw_tokens(unwrapped, wide)


def fact_tokens(text):
    """The tokens a fact is corroborated by: sig_tokens, or the wide ones for a fact of short words (fewer than two
    word stems the usual way, and no n-grams)."""
    toks = sig_tokens(text)
    if any(is_gram(t) for t in toks) or len({_stem(t) for t in toks}) >= 2:
        return toks
    return sig_tokens(text, wide=True)


def _is_wide(item_tokens):
    """Whether a fact's tokens came from the wide tokenizer (a word under the usual minimum among them)."""
    return any(not is_gram(t) and len(t) < (SETTINGS.TOKEN_MIN_LATIN if t.isascii() else SETTINGS.TOKEN_MIN_OTHER)
               for t in item_tokens)


@functools.lru_cache(maxsize=16384)
def _wide_stems(text, generation):
    """Stems of a message read wide, for comparing it with a fact of short words. *generation*: see _sentence_stems."""
    return frozenset(_stems(sig_tokens(text, wide=True)))


MACHINE_QUOTE_RE = re.compile(
    r"cronjob\s+response|##\s*script\s+output|\[dream[\]-]|wakeagent", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _missing_db(db, what):
    """A database the core has not created yet is 'nothing to work with', not a
    crash. On a fresh install `memory_store.db` appears with the first stored
    fact and `state.db` with the first session; without this the nightly job
    reported a traceback-flavoured `dream_error` every night until then."""
    if os.path.exists(db):
        return False
    print(f"[dream] note: {db} does not exist yet — {what}", file=sys.stderr)
    return True


FACT_FIELDS = ["fact_id", "content", "category", "tags", "trust_score", "retrieval_count",
               "helpful_count", "created_at", "updated_at"]


def load_facts(db=None):
    """Facts of the holographic-memory store as dicts with FACT_FIELDS, read-only; [] without a store."""
    path = db or fact_store_path()
    if not path or _missing_db(path, "treating the fact store as empty"):
        return []
    return _load_holographic(path)


def _load_holographic(db):
    rows = []
    c = _conn(db)
    try:
        origins = _fact_origins(c)
        for r in c.execute("select " + ",".join(FACT_FIELDS) + " from facts"):
            row = dict(zip(FACT_FIELDS, r))
            row["tags"], row["keywords"] = split_keywords(row.get("tags"))
            if origins.get(row["fact_id"]):
                row["origin"] = origins[row["fact_id"]]
            rows.append(row)
    finally:
        c.close()  # the sqlite3 context manager commits but does NOT close
    return rows


ORIGINS = ("said", "confirmed", "inferred")


def _fact_origins(conn):
    """{fact_id: origin} (holographic >= 0.15.0): who stands behind a fact's current text — `said` (a person said
    it), `confirmed`, `inferred` (an agent's conclusion). An inference or an unknown origin a person confirmed since
    reads as `confirmed`, as the provider shows it. Unknown (stored before 0.15.0, or an older schema: the column
    and the table appear on a writer's open) is left out. Read-only; never part of the `facts` query, which the
    provider keeps stable for this reader."""
    if "src_origin" not in _table_columns(conn, "fact_versions"):
        return {}
    try:
        out = {fid: o for fid, o in conn.execute(
            "SELECT fact_id, src_origin FROM fact_versions WHERE status = 'current'") if o in ORIGINS}
        if _table_columns(conn, "fact_confirmations"):
            for (fid,) in conn.execute("SELECT DISTINCT fact_id FROM fact_confirmations"):
                if out.get(fid) in (None, "inferred"):
                    out[fid] = "confirmed"
        return out
    except sqlite3.Error:
        return {}


KEYWORDS_MARK = "keywords: "


def split_keywords(tags):
    """(the human's tags, the keywords) of a holographic `tags` value.

    holographic >= 0.6.0 appends the agent's keywords after a "keywords: "
    line. They are search aids, not tags: left in, a fact with eight synonyms
    scored as "conceptually rich" and climbed towards promotion."""
    text = tags or ""
    if text.startswith(KEYWORDS_MARK):
        head, tail = "", text[len(KEYWORDS_MARK):]
    else:
        head, mark, tail = text.rpartition("\n" + KEYWORDS_MARK)
        if not mark:
            return text, []
    return head, [w.strip() for w in tail.split(",") if w.strip()]


def _table_columns(conn, table):
    """Column names of `table` ([] when it does not exist). The query is built
    from this instead of catching OperationalError three times over."""
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _day_of(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(SETTINGS.LOCAL_TZ).strftime("%Y-%m-%d")


def prepare_messages(items, days, now=None):
    """The person's messages of the last *days*, ready for corroboration. *items*: dicts with `content`, `ts` (Unix
    time), `session_id`, and optionally `source` and `key` — the caller has already kept only the person's own words.
    Harness inserts and `exclude_patterns` are dropped here too, and a sentence repeated in one session at one moment
    (a copied line) counts once. In time order, so everything downstream is the same however the caller listed them."""
    cutoff = (now or _now()).timestamp() - days * 86400
    out, seen_rows = [], set()
    for i, m in enumerate(sorted(items, key=lambda m: (m.get("ts") or 0, str(m.get("key") or "")))):
        content, ts, sid = m.get("content"), m.get("ts"), m.get("session_id")
        if not content or not isinstance(ts, (int, float)) or ts < cutoff:
            continue
        if is_harness_envelope(content) or any(p.search(content) for p in SETTINGS.EXCLUDE_PATTERNS):
            continue
        row = (sid, content, ts)
        if row in seen_rows:
            continue
        seen_rows.add(row)
        out.append({"id": i, "content": content, "ts": ts, "day": _day_of(ts), "tokens": sig_tokens(content),
                    "source": m.get("source"), "session_id": sid,
                    "key": m.get("key") or _message_key(sid, ts, content)})
    return out


# ---------------------------------------------------------------------------
# Corroboration and scoring
# ---------------------------------------------------------------------------

EVIDENCE_MAX = 3
# One shared word is evidence only when the word is rare. Latin keeps the
# historical 8 characters; Cyrillic needs more — see corroborate().
DISTINCTIVE_LATIN = 8
DISTINCTIVE_OTHER = 10


# Shared n-grams count by the stretches of text they cover, not one by one: the three pairs of ソフィア are one
# name, as "Sofia" is one word. Pairs that chain (ソフ → フィ → ィア) are one shared stretch; a mention needs two
# stretches, or a stretch and a word — as it needs two shared words in spaced scripts. A stretch of a name alone
# would otherwise corroborate every fact about that person. A stretch with only half-weight n-grams (a particle
# glued to a name, ンは) counts half; a long stretch is rare enough to count alone, as a long word does.
DISTINCTIVE_GRAMS = {"cjk": 5, "hangul": 4, "sea": 6}


def _gram_script(gram):
    return "sea" if _SEA_RE.match(gram) else "hangul" if "가" <= gram[0] <= "힯" else "cjk"


def gram_stretches(grams):
    """Groups of n-grams that chain into one stretch of text (each overlaps the next by all but one character)."""
    # Linear, not pairwise: every n-gram ending in k chains with every n-gram starting with k, so all of them are one
    # stretch — joined through k once instead of edge by edge (a character shared by hundreds of pairs, 一中 … 中一,
    # made the pairwise version quadratic; review of 2.5.1). A key nobody continues joins nothing.
    grams = set(grams)
    heads = {g[:-1] for g in grams}
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for g in grams:  # ends in a key that some n-gram starts with: join the key
        find(g)
        if g[1:] in heads:
            parent[find(g)] = find(("key", g[1:]))
    for g in grams:  # starts with a key that some n-gram ends in: join the same key
        key = ("key", g[:-1])
        if key in parent:
            parent[find(g)] = find(key)
    groups = defaultdict(set)
    for g in grams:
        groups[find(g)].add(g)
    return sorted(groups.values(), key=min)


def _mentions(shared):
    """Whether a shared set of stems is a mention: two words or stretches of n-grams, or one long stretch (a rare
    shared word is checked by the caller, see DISTINCTIVE_PREFIX_LATIN)."""
    score = sum(1 for s in shared if not is_gram(s))
    if score >= 2:
        return True
    for group in gram_stretches(s for s in shared if is_gram(s)):
        if len(group) >= DISTINCTIVE_GRAMS[_gram_script(next(iter(group)))]:
            return True
        weight = max(gram_weight(g) for g in group)
        # Chinese writes several words without a break (搬到上海 = "move to Shanghai"): a stretch of Han
        # characters weighs one word per two characters; kana, Hangul and Thai stretches are mostly one word.
        if any(_HAN_RE.search(g) for g in group):
            weight *= max(1.0, (len(group) + 1) / 2)
        elif len(group) == 1 and _SEA_RE.match(next(iter(group))):
            # Three Thai letters are a piece of a word as often as a word (น้ำ "water" sits in ห้องน้ำ
            # "bathroom" and in ว่ายน้ำ "swim"): alone they weigh half (blind Thai set, 4.4 % false, 2026-10-13).
            weight *= 0.5
        score += weight
    return score >= 2


# One shared rare word is a mention only when the two words share more than the 5-letter stem: «commercial» is
# rare, but its stem «comme» is also the stem of «comment» and «commence», «Schauinsland» shares «schau» with
# «schaut», «gidiyoruz» shares «gidiy» with every "we go" (French 3,3 % false mentions, 2026-10-13). Six letters
# keep «Lissabon»/«Lissabone», «transcription»/«transcribe». Cyrillic keeps its stem (endings are cut, not a prefix).
DISTINCTIVE_PREFIX_LATIN = 6


# from dream_lang.py (3.0)
WEEKDAY_PREFIXES = _LANG.WEEKDAY_PREFIXES


def _distinctive_stem(token):
    """(script, stem) — tagged, so a Latin prefix never meets the stem of a mixed token: «telegram_react» and
    «telegram-юзернейм» share six letters, not a word (live data, 2026-10-13)."""
    latin = latin_fold(token)
    return ("latin", latin[:DISTINCTIVE_PREFIX_LATIN]) if latin.isascii() else ("other", _stem(token))


# A reply carries the message it answers (`[Replying to: "…"]`, mostly the agent's own text), and sig_tokens unwraps
# the quote so that "Да, всё ещё Vespa" under a question about the bike is a mention of the bike. On a live install
# 46 % of human messages were replies, the quotes ran 523 characters against 63 of the person's own, and they
# corroborated what the person never spoke of: 535 fact-message mentions from 81 replies, 64 with the quote left out
# (Laya experiment, 2026-10-22). The quote now counts for a fact only when the person's own words share a stem with it.
@functools.lru_cache(maxsize=16384)
def _own_stems(text, generation, wide=False):
    """Stems of the person's own words in a reply (quote left out, voice transcript kept), read *wide* against a fact
    of short words; None for a text without a reply quote. *generation* keys the cache to the settings' stopwords, as
    in _sentence_stems."""
    if not REPLY_QUOTE_RE.search(text):
        return None
    return frozenset(_stems(_raw_tokens(_own_words(text), wide)))


def _own_words(text):
    """The person's own words of a message: the reply quote left out, a voice transcript unwrapped (it is speech)."""
    return VOICE_QUOTE_RE.sub(lambda m: " " + m.group(1) + " ", REPLY_QUOTE_RE.sub(" ", text or ""))


# Words nearly every fact of a home shares ("чат", "ветка", "Telegram", "сообщения" in a store of thread rules) made a
# mention between any two of them: after the quotes, they carried most of the remaining false mentions on a live
# install (8 stems in a fifth of its 59 facts; Laya experiment, 2026-10-22). A stem in HOUSEHOLD_SHARE of the facts
# or more still counts towards a mention but cannot make one alone: a name is such a stem too (李雷 in a fifth of the
# facts), and "李雷" + "有车" stays a mention. A store under HOUSEHOLD_MIN_FACTS facts has none — too few to tell.
HOUSEHOLD_SHARE = 0.2
HOUSEHOLD_MIN_FACTS = 10
_HOUSEHOLD = frozenset()


def household_stems(contents):
    """The stems in HOUSEHOLD_SHARE of *contents* (fact texts) or more."""
    contents = [c for c in contents if c]
    if len(contents) < HOUSEHOLD_MIN_FACTS:
        return frozenset()
    df = Counter(s for c in contents for s in _stems(fact_tokens(c)))
    return frozenset(s for s, n in df.items() if n >= max(2, HOUSEHOLD_SHARE * len(contents)))


def set_household(contents):
    """Make the household stems of *contents* the ones corroboration does not let make a mention alone (the pass
    calls it with its facts)."""
    global _HOUSEHOLD
    _HOUSEHOLD = household_stems(contents)


class RulesContext(namedtuple("RulesContext", "household settings")):
    """What the rules of one night read besides the settings: the household stems of its facts, and the settings
    fingerprint they were taken under. Passed explicitly instead of the module's `_HOUSEHOLD` (3.3.0)."""


def rules_context(contents):
    """The RulesContext of a night whose shared facts are *contents*."""
    return RulesContext(household_stems(list(contents)), settings_fingerprint())


def settings_fingerprint():
    """Identity, generation and every value of the module's Settings. Callers and tests set fields in place, so
    the identity alone misses a change; a step that must leave the settings alone compares this before and after."""
    s = SETTINGS
    values = repr([(name, getattr(s, name, None)) for name in _SETTING_NAMES])
    return id(s), SETTINGS_GENERATION, hashlib.sha256(values.encode("utf-8")).hexdigest()


# A (fact, message) pair: what a cloud judge may veto. The message key does not depend on the row order or on which
# compaction copy was kept — session, time and text are what make a message this one.
PairId = namedtuple("PairId", "fact_id message_key")
FactProbe = namedtuple("FactProbe", "item_stems own_item distinctive wide")


def _message_key(session_id, ts, content):
    return hashlib.sha256(f"{session_id}\x1f{ts!r}\x1f{content}".encode("utf-8")).hexdigest()[:32]


def message_key(m):
    """The stable key of message *m* (load_messages() stores it; a hand-built message gets it computed)."""
    return m.get("key") or _message_key(m.get("session_id"), m.get("ts"), m.get("content"))


def fact_probe(item_tokens, rules=None):
    """What a fact brings to every pair, computed once: its stems, the ones that are its own (not household words),
    the distinctive ones and whether it is read wide."""
    household = _HOUSEHOLD if rules is None else rules.household
    item_stems = _stems(item_tokens)
    distinctive = {_distinctive_stem(t) for t in item_tokens if not is_gram(t)
                   and len(t) >= (DISTINCTIVE_LATIN if latin_fold(t).isascii() else DISTINCTIVE_OTHER)
                   and not t.startswith(WEEKDAY_PREFIXES) and _stem(t) not in household}
    return FactProbe(item_stems, item_stems - household, distinctive, _is_wide(item_tokens))


def _pair_counts(probe, m, content=None):
    """Whether message *m* mentions the fact of *probe* — the one rule the count and the judge's candidates share.
    With *content* (the fact's text), a message that negates it or gives it a new number is no mention. What is
    read from *m* is read once a pass (_message_data)."""
    d = _message_data(m)
    own = d.own_stems(probe.wide)
    if own is not None and probe.own_item.isdisjoint(own):
        return False  # a reply whose own words say nothing of this fact: the quote alone is no mention of it
    msg_stems = d.wide_stems() if probe.wide else d.stems()
    inter = probe.item_stems & msg_stems
    rare = bool(probe.distinctive) and d.has_distinctive(probe.distinctive)
    if not rare and probe.own_item.isdisjoint(inter):
        return False
    return bool(rare or _mentions(inter)) and not (content and _message_contradicts(content, d))


def corroborate(item_tokens, messages, content=None, rules=None, veto=None, fact_id=None):
    """How many different days / messages mention the token set: (mentions, ref_days, last_ts). With *content* (the
    fact's text), a message that says the same with the opposite polarity ("не любит" against "любит") is no mention
    (3.1.0, Antigravity review of 3.0.1). *rules*: the night's RulesContext (default: the module's household).
    *veto*: PairIds of *fact_id* that do not count (3.3.0).

    Match: overlap ≥2 STEMS, or ≥1 "distinctive" stem. Stems, because Russian
    inflects: «Мы окончательно переехали, в Лиссабоне…» did not corroborate
    «Илья переехал в Лиссабон» while «Мой брат переехал в Москву» did (external
    review, 2026-10-07). "Distinctive" means rare enough that one hit is
    evidence — and 8 characters is not rare in Russian (41-45% of words reach
    it: «например», «обязательно»), hence a higher bar for non-Latin."""
    return _corroborate(item_tokens, messages, False, content, rules, veto, fact_id)[:3]


def corroborate_with_evidence(item_tokens, messages, content=None, rules=None, veto=None, fact_id=None,
                              counted=None):
    """`corroborate` plus the samples: (mentions, ref_days, last_ts, samples), where samples are up to
    EVIDENCE_MAX {day, text} snippets from different days — provenance for the reviewer ("why does the dream
    think this is confirmed?"). Two functions, not a flag that changes the shape of the answer (3.0).
    *counted*: a set that receives the PairId of every pair that counted."""
    return _corroborate(item_tokens, messages, True, content, rules, veto, fact_id, counted)


def _corroborate(item_tokens, messages, evidence, content=None, rules=None, veto=None, fact_id=None, counted=None):
    if not item_tokens:
        return 0, 0, None, []
    # The fact's own words: household stems still count towards a mention, but cannot make one alone.
    probe = fact_probe(item_tokens, rules)
    days, mentions, last_ts, samples = set(), 0, None, []
    for m in messages:
        if _pair_counts(probe, m, content):
            if veto or counted is not None:
                pair = PairId(fact_id, message_key(m))
                if veto and pair in veto:
                    continue
                if counted is not None:
                    counted.add(pair)
            mentions += 1
            # A message still counts as a mention, but its text travels to the
            # agent only when it is clean: the fact was screened by
            # classify_unsafe, the corroborating message was not — a
            # `password: hunter2` said in chat leaked through `evidence`.
            if (evidence and m["day"] not in days and len(samples) < EVIDENCE_MAX
                    and not _message_data(m).unsafe()):
                samples.append({"day": m["day"], "text": _message_data(m).snippet()})
            days.add(m["day"])
            last_ts = max(last_ts or 0, m["ts"])
    return mentions, len(days), last_ts, samples


def _snippet(text, n=100):
    text = re.sub(r"\s+", " ", strip_urls(text or "")).strip()
    return text[:n] + ("…" if len(text) > n else "")


def signals_for_fact(f, messages, now=None, rules=None, veto=None, counted=None):
    now = now or _now()
    rc = f.get("retrieval_count") or 0
    hc = f.get("helpful_count") or 0
    trust = f.get("trust_score")
    trust = 0.5 if trust is None else float(trust)
    created = _parse_ts(f.get("created_at"))
    updated = _parse_ts(f.get("updated_at")) or created
    tags = [t for t in re.split(r"[,\s]+", f.get("tags") or "") if t]

    toks = fact_tokens(f.get("content"))
    mentions, ref_days, last_ts, evidence = corroborate_with_evidence(
        toks, messages, f.get("content"), rules=rules, veto=veto, fact_id=f.get("fact_id"), counted=counted)

    days_updated = (now - updated).total_seconds() / 86400 if updated else 999
    days_last_ref = (now.timestamp() - last_ts) / 86400 if last_ts else 999
    eff_recent = min(days_updated, days_last_ref)
    span_days = ((updated - created).total_seconds() / 86400) if (created and updated) else 0

    relevance = min(1.0, 0.6 * trust + 0.4 * min(hc / 3.0, 1.0))
    frequency = min(1.0, (rc + mentions) / 6.0)          # stored retrievals + chat mentions
    query_diversity = min(1.0, ref_days / 3.0)           # real number of distinct days
    recency = math.exp(-max(eff_recent, 0) / 30.0)
    consolidation = min(1.0, max(span_days / 14.0, (ref_days - 1) / 3.0 if ref_days > 1 else 0.0))
    conceptual_richness = min(1.0, len(tags) / 3.0)

    sig = {"relevance": relevance, "frequency": frequency, "query_diversity": query_diversity,
           "recency": recency, "consolidation": consolidation, "conceptual_richness": conceptual_richness}
    score = round(sum(SETTINGS.WEIGHTS[k] * v for k, v in sig.items()), 3)
    meta = {"days_old": round(days_updated, 1), "rc": rc, "mentions": mentions, "ref_days": ref_days,
            "evidence": evidence, "last_ts": last_ts or 0}
    return score, {k: round(v, 3) for k, v in sig.items()}, meta


# ---------------------------------------------------------------------------
# Durable memory: dedupe
# ---------------------------------------------------------------------------

# Memory comes in as texts, not files. `notes` are the entries the night may ask "still relevant?" about (Claude
# Code's memory notes); `dedupe` are texts that already are memory and are only compared against (instructions,
# Codex memory). Internally they are joined with one delimiter into `memory_text`, split back into comparison units.
MEMORY_ENTRY_DELIMITER = "\n§\n"


class Memory(namedtuple("Memory", "notes dedupe")):
    """What the agents' memory holds tonight: `notes` (tuple of str, asked about) and `dedupe` (tuple of str)."""
    __slots__ = ()

    def __new__(cls, notes=(), dedupe=()):
        def clean(items):
            return tuple(t for t in ((s or "").replace(MEMORY_ENTRY_DELIMITER, "\n").strip() for s in items) if t)
        return super().__new__(cls, clean(notes), clean(dedupe))


def _memory_entries(text):
    """Comparison units of a joined memory text."""
    return [e for e in (p.strip() for p in (text or "").split(MEMORY_ENTRY_DELIMITER)) if e]


def load_durable_memory_text(memory):
    return MEMORY_ENTRY_DELIMITER.join(memory.notes + memory.dedupe)


def _memory_chunks(memory_text):
    """Comparison units for fuzzy dedupe."""
    return _memory_entries(memory_text) or [(memory_text or "").strip()]


def load_durable_memory_sources(memory):
    """[(target, entry)] of the notes — what `nearest_entry` may point at."""
    return [("memory", entry) for entry in memory.notes]


NUM_TERM_RE = re.compile(r"^#(\d+)$")


def _term_hit(term, text):
    """`#437` → whole number 437 (not 2024 / 4370); anything else → substring."""
    m = NUM_TERM_RE.match(term)
    if m:
        return re.search(rf"(?<!\d){re.escape(m.group(1))}(?!\d)", text) is not None
    return term in text


def _groups_hit(groups, text):
    """AND of OR-groups: [["a","b"],["c"]] ⇒ (a or b) and c."""
    if not groups:
        return False
    for group in groups:
        if isinstance(group, str):
            group = [group]
        if not any(_term_hit(_norm(t), text) for t in group if t):
            return False
    return True


def _semantic_memory_alias(content, memory_text, rules=None):
    """Declarative aliases for recurring facts that get reworded.

    Token overlap alone misses inflection/synonyms. Each rule is
    {"fact": [[…],[…]], "memory": [[…]]}: when the fact matches its groups AND
    durable memory matches its groups, the fact counts as already known — no
    LLM needed in the nightly cron. Rules are installation-specific and live in
    the config (`alias_rules`)."""
    rules = SETTINGS.ALIAS_RULES if rules is None else rules
    if not rules:
        return False
    c = _norm(content)
    m = _norm(memory_text)
    return any(_groups_hit(r.get("fact"), c) and _groups_hit(r.get("memory"), m) for r in rules)


# from dream_lang.py (3.0)
RU_SUFFIXES = _LANG.RU_SUFFIXES
STEM_CHARS_LATIN = 5
STEM_CHARS_OTHER = 6
STEM_MIN_ROOT = 4


# Latin letters that do not decompose into a base letter and a diacritic.
_LATIN_FOLD = str.maketrans({"ł": "l", "ı": "i", "ß": "ss", "ø": "o", "æ": "ae", "œ": "oe", "đ": "d", "ħ": "h",
                             "þ": "th", "ð": "d"})


@functools.lru_cache(maxsize=65536)
def latin_fold(token):
    """The token without diacritics when it is written in Latin letters (szkoła → szkola, öğretmen → ogretmen,
    Größe → grosse); any other token unchanged. One word must not get two stems because one of its forms happens
    to be plain ASCII and another is not: «szkoła»/«szkole» were "szkoła"/"szkol" — never the same word."""
    if token.isascii():
        return token
    plain = "".join(c for c in unicodedata.normalize("NFKD", token.translate(_LATIN_FOLD))
                    if not unicodedata.combining(c))
    return plain if plain.isascii() else token


@functools.lru_cache(maxsize=65536)
def _stem(token):
    """One crude stem for fuzzy comparison. Latin (diacritics folded) keeps the old 5-char prefix; an n-gram of
    unsegmented script is its own stem."""
    if is_gram(token):
        return token
    latin = latin_fold(token)
    if latin.isascii():
        return latin[:STEM_CHARS_LATIN]
    t = token
    for suf in RU_SUFFIXES:
        if len(t) - len(suf) >= STEM_MIN_ROOT and t.endswith(suf):
            t = t[: -len(suf)]
            break
    return t[:STEM_CHARS_OTHER]


def _latin_root(word):
    """A Latin word without its third-person or plural "-s", for comparing what two texts negate only: "eats" /
    "eat", "likes" / "like" — the 5-character prefix kept them apart, so "does not eat" never met "eats" (Antigravity
    review of 3.0.1). Not in the general stem: Dutch "fiets" / "fietsen" would part there and German "Dachs" meet
    "Dach" (Codex review). Not "-ss", "-us", "-is" (class, bus, Paris), nor a word of three letters (gas)."""
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


# A negation is often said in another tense than the fact: "eats" / "hasn't eaten", "курит" / "никогда не курил",
# "любит" / "не любила" — the 5- or 6-character stem kept them apart, and the flip was lost (Laya experiment,
# 2026-10-22). Only for comparing what two texts negate, like _latin_root; a root keeps three letters at least.
_LATIN_TENSE_ENDINGS = ("ing", "ed", "en", "e")
_RU_TENSE_ENDINGS = ("ила", "ило", "или", "ела", "ело", "ели", "ала", "ало", "али", "ил", "ел", "ал",
                     "ит", "ет", "ат", "ят", "ют", "ут")


def _tense_root(word, endings):
    # Twice at most, so that an ending on an ending meets the shorter form: "listened" → "listen" → "list", as
    # "listens" → "listen" → "list" (Codex review of 3.2.0).
    for _ in range(2):
        for end in endings:
            if word.endswith(end) and len(word) - len(end) >= 3:
                word = word[: -len(end)]
                break
        else:
            break
    return word


@functools.lru_cache(maxsize=65536)
def _neg_stem(word):
    """The stem a negation is compared by: _stem, after _latin_root and the tense ending for a Latin word, after the
    tense ending for a Cyrillic one."""
    if is_gram(word):
        return _stem(word)
    latin = latin_fold(word)
    if latin.isascii():
        return _stem(_tense_root(_latin_root(latin), _LATIN_TENSE_ENDINGS))
    return _stem(_tense_root(word, _RU_TENSE_ENDINGS) if _CYRILLIC_RE.match(word) else word)


def _stems(tokens):
    """Stems for fuzzy comparison.

    Inflection breaks exact comparison («транспортные»≠«транспорт») — a
    reworded duplicate scored containment 0.3–0.5 against the 0.62 threshold
    and resurfaced in promotions every night."""
    return {_stem(t) for t in tokens}


# Reverse dedupe direction: a short memory entry as a digest of a long fact.
# Direct containment is |fact ∩ entry| / |fact|, so the tidier the rule, the
# worse it dedupes: an auto-extracted fact has 46 stems, a normal entry 19 —
# ceiling 0.41 against 0.62, unreachable. Live case: a rule sat in MEMORY.md
# while the agent answered "already there verbatim" six nights in a row and
# the dream kept proposing it (containment 0.35).
# The reverse check is stricter on purpose: the entry must lie almost entirely
# inside the fact, be substantial by itself, and really overlap. Otherwise a
# short entry about a person would silence any long fact mentioning them.
SUMMARY_CONTAINMENT = 0.75
SUMMARY_MIN_CHUNK_STEMS = 8
SUMMARY_MIN_SHARED_STEMS = 6


# Third dedupe direction: the entry the agent has just written from this very
# candidate. It is neither a containment (the candidate keeps its own reporting
# wrapper — "<person> confirmed that …" — so forward coverage lands just under
# 0.62) nor a digest (the entry is too short for the ≥8-stem reverse rule).
# Live case 2026-08-16: three facts were promoted, and one of them returned as
# a candidate in the very next pass at coverage 0.57.
# The guard against false positives is NUMBERS: identity is accepted only when
# the candidate introduces no number the entry lacks — so "thread 42 → 437",
# a new price or a moved date stays a conflict (an update to review), never a
# silent duplicate.
SAME_SUBJECT_RATIO = 0.8
SAME_SUBJECT_MIN_SHARED = 4
# A short entry that sits ENTIRELY inside the candidate ("X likes jasmine tea"
# vs "2026-06-19 Y said that X likes jasmine tea"): every stem of the entry is
# in the candidate, the numbers agree — the wrapper is the only difference.
SAME_SUBJECT_FULL_MIN_STEMS = 3


def _same_subject(item_stems, chunk_stems, content, chunk):
    shared = item_stems & chunk_stems
    if len(shared) < SAME_SUBJECT_MIN_SHARED:
        if not (len(chunk_stems) >= SAME_SUBJECT_FULL_MIN_STEMS and shared == chunk_stems):
            return False
    elif len(shared) / min(len(item_stems), len(chunk_stems)) < SAME_SUBJECT_RATIO:
        return False
    return _numbers(content) <= _numbers(chunk)


def _covers(item_stems, chunk_stems, threshold=REJECT_MATCH_THRESHOLD):
    if not chunk_stems:
        return False
    shared = item_stems & chunk_stems
    if len(shared) / len(item_stems) >= threshold:
        return True
    return (len(chunk_stems) >= SUMMARY_MIN_CHUNK_STEMS
            and len(chunk_stems) < len(item_stems)
            and len(shared) >= SUMMARY_MIN_SHARED_STEMS
            and len(shared) / len(chunk_stems) >= SUMMARY_CONTAINMENT)


# A negation flips a fact without changing its words: «<person> ест арахис» and
# «<person> не ест арахис» share every significant stem (negations are
# stopwords for corroboration), so fuzzy dedupe called the second "already in
# memory" — the changed fact was dropped silently, and an old rejection closed
# the opposite one (Codex review of 2.7.2, 2026-10-14). What is compared is
# what each side NEGATES — the word right after a negation ("не ест",
# "does not tolerate"; past a modal, the verb it carries: "не должна
# использоваться") — and only words both texts contain: a word negated on one
# side and plain on the other is a flip; a "не" about something the other text
# does not mention is not. A long entry is narrowed first to its sentences that
# say nearly the same as the candidate (stem Jaccard >= 0.5): a profile has a
# "не" in some sentence about something else. A flipped candidate is no
# duplicate of that entry (nor of a rejection): it stays a candidate and goes to
# `conflicts` as a possible update. Only negations count — comparatives
# ("больше", "more") are significant for the store-duplicate check but negate
# nothing ("drinks more coffee" is no flip of "drinks coffee"; review of 2.7.4).
# When a text says the same word both ways ("любит арахис, но не любит
# миндаль"), the word alone cannot tell the sides apart: such a word is
# compared together with the word after it, the thing it is said of (Codex
# review of 2.7.5).
# from dream_lang.py (3.0)
(NEGATION_WORDS, _MODALS) = (
    _LANG.NEGATION_WORDS, _LANG._MODALS)
_SENTENCE_SPLIT_RE = re.compile(r"[.!?;\n。！？；]+")
POLARITY_MIN_JACCARD = 0.5


@functools.lru_cache(maxsize=16384)
def _sentence_stems(text, generation):
    """The stems of *text* by the current settings' stopwords. *generation* (SETTINGS_GENERATION with the id of
    SETTINGS) keys the cache to them: an id alone may be reused by the next Settings (second Codex pass)."""
    return frozenset(_stems(sig_tokens(text)))


@functools.lru_cache(maxsize=16384)
def _words_of(text):
    return tuple(_plain_words(text))


def _plain_words(text):
    return _WORD_RE.findall(_norm(text).replace("ё", "е"))


def _real_word(words, start):
    """Index of the first real word from *start* (negations and 1–2 letter words skipped), or None."""
    for j in range(start, len(words)):
        if words[j] not in NEGATION_WORDS and len(words[j]) >= 3:
            return j
    return None


@functools.lru_cache(maxsize=65536)
def _negations(words):
    """((index, stem, "не ест"), …) — the first real word after each negation, and the next one as well when the
    first is a modal. *words* is a tuple (_words_of): the answer is cached."""
    out = []
    for i, w in enumerate(words):
        if w not in NEGATION_WORDS:
            continue
        j = _real_word(words, i + 1)
        while j is not None:
            out.append((j, _neg_stem(words[j]), f"{w} {words[j]}"))
            if words[j] not in _MODALS:
                break
            j = _real_word(words, j + 1)
    return tuple(out)


def _two_way(words, negs):
    """Stems this text says both negated and plain."""
    at = {j for j, _, _ in negs}
    return {s for _, s, _ in negs} & {_neg_stem(w) for k, w in enumerate(words) if k not in at and len(w) >= 3}


def _negated(words, both, two_way):
    """{key: "не ест"} for the negated words both texts contain; a word said both ways is keyed together with
    the word after it."""
    return _negated_by(words, _negations(words), both, two_way)


def _negated_by(words, negations, both, two_way):
    """_negated with the _negations of *words* already read (a message's are read once a pass)."""
    out = {}
    for j, s, phrase in negations:
        if s not in both:
            continue
        if s in two_way:
            k = _real_word(words, j + 1)
            obj = words[k] if k is not None else ""
            if obj and _neg_stem(obj) not in both:
                # "likes Thai food but does not like Italian food" against "likes Thai food": the negated thing is not
                # one the other text speaks of (Codex review of 3.1.0).
                continue
            out.setdefault(f"{s} {_neg_stem(obj) if obj else ''}", f"{phrase} {obj}".strip())
        else:
            out.setdefault(s, phrase)
    return out


# Chinese writes no spaces: what a negation negates is what follows it, up to five characters ("不喜欢吃苹果" →
# "喜欢吃苹果"). It is a flip when the other text says the longest piece of it it holds, the rest following too, without
# a negation of its own before it in the same clause ("不吃苹果" against "吃红苹果" meets at "吃"; "不喜欢吃苹果" does not
# meet "喜欢吃香蕉"; "不再喝酒" agrees with "不喝酒"; 无锡, 没收, 别墅 are the same word on both sides). Antigravity review of
# 3.0.1, Codex passes over 3.1.0 / 3.1.1.
_CJK_NEGATION_RE = re.compile(r"(?:没有|沒有|无法|無法|不|没|沒|别|別|未|无|無)([㐀-鿿豈-﫿]{1,5})")
_CJK_NEGATION_CHARS = "不没沒别別未无無"


# An aspect particle after the negated verb is no part of the thing negated: "不吃肉了", "没抽过烟", "再也不打网球了" negate
# 吃肉, 抽烟, 打网球, and with the particle kept the other text never held the piece (Laya experiment, 2026-10-22). Only
# past the first character: 过敏, 了解, 着急 begin with it.
_CJK_ASPECT = "了过過着著"


def _cjk_negated(text):
    """{"喜欢苹": "不喜欢苹"}: what *text* negates, by the characters after each negation."""
    out = {}
    for m in _CJK_NEGATION_RE.finditer(text):
        what = m.group(1)
        what = what[0] + "".join(c for c in what[1:] if c not in _CJK_ASPECT)
        out.setdefault(what, m.group(0))
    return out


# A clause ends at punctuation and at "but" (但, 却, 可是): "不抽烟但喝酒" does not negate 喝酒.
_CJK_CLAUSE_END = ("，。、；：！？,.;:!?" + " " + chr(10), ("但是", "可是", "但", "却", "卻"))


def _cjk_says_plainly(text, what):
    """Whether *text* holds *what* somewhere without a negation before it in its clause, up to four characters back
    ("不经常喝酒" negates 喝酒 too)."""
    start = text.find(what)
    while start >= 0:
        before = text[max(0, start - 4):start]
        for c in (*_CJK_CLAUSE_END[0], *_CJK_CLAUSE_END[1]):
            before = before.rsplit(c, 1)[-1]
        if not any(c in _CJK_NEGATION_CHARS for c in before):
            return True
        start = text.find(what, start + 1)
    return False


def _cjk_flips(negated, other):
    """{key: phrase} for what *negated* (from _cjk_negated) negates and *other* says plainly."""
    out = {}
    for what, phrase in negated.items():
        for n in range(len(what), 0, -1):
            # A shorter piece counts when the rest is there too: "不吃苹果" meets "吃红苹果", not "吃香蕉".
            if _cjk_says_plainly(other, what[:n]) and what[n:] in other:
                out[f"zh {what[:n]}"] = phrase
                break
    return out


_NEGATION_ANY_RE = None


@functools.lru_cache(maxsize=16384)
def _negates_anything(text):
    """Whether *text* holds a negation at all: no polarity can differ between two texts that have none."""
    global _NEGATION_ANY_RE
    if _NEGATION_ANY_RE is None:
        words = "|".join(sorted((re.escape(w) for w in NEGATION_WORDS), key=len, reverse=True))
        _NEGATION_ANY_RE = re.compile(r"(?<!\w)(?:" + words + r")(?!\w)|" + _CJK_NEGATION_RE.pattern, re.IGNORECASE)
    return bool(_NEGATION_ANY_RE.search(_norm(text or "").replace("ё", "е")))


def polarity_flip(content, chunk):
    """(what the fact negates, what the entry negates) when they disagree about a word both contain, or None."""
    # Every matching message of a fact is checked against it, and every message against every fact: the parts of a
    # text are cached (Codex review of 3.1.0: ten times the corroboration time on a busy install otherwise).
    # The nightly count does not come here since 3.7.1: it reads a message once a pass (_message_flip). This is the
    # rule for two texts (memory entries, rejections, the store's pairs) and the reference _message_flip is tested
    # against.
    if not (_negates_anything(content or "") or _negates_anything(chunk or "")):
        return None
    sentences = [s for s in _SENTENCE_SPLIT_RE.split(chunk or "") if s.strip()]
    if len(sentences) > 1:
        item = _sentence_stems(content or "", (SETTINGS_GENERATION, id(SETTINGS)))
        sentences = [s for s in sentences if (st := _sentence_stems(s, (SETTINGS_GENERATION, id(SETTINGS)))) and item
                     and len(item & st) / len(item | st) >= POLARITY_MIN_JACCARD]
        if not sentences:
            return None
    other = ". ".join(sentences) if sentences else (chunk or "")
    mine, theirs = _words_of(content or ""), _words_of(other)
    both = {_neg_stem(w) for w in mine} & {_neg_stem(w) for w in theirs}
    two_way = _two_way(mine, _negations(mine)) | _two_way(theirs, _negations(theirs))
    a, b = _negated(mine, both, two_way), _negated(theirs, both, two_way)
    mine_text, their_text = _norm(content), _norm(other)
    na, nb = _cjk_negated(mine_text), _cjk_negated(their_text)
    a.update(_cjk_flips(na, their_text))
    b.update(_cjk_flips(nb, mine_text))
    if set(a) == set(b):
        return None
    return sorted(a.values()), sorted(b.values())


def polarity_differs(content, chunk):
    """True when `content` negates (or un-negates) what `chunk` says about the same word."""
    return polarity_flip(content, chunk) is not None


HEAD_CHARS = 40
HEAD_MATCH_MIN_STEM_RATIO = 0.6


def exact_or_alias_in_memory(content, memory_text):
    """The strong part of dedupe: identical head (40 chars) or a declared alias.
    Fuzzy containment is deliberately NOT here — a fuzzy match with different
    numbers is a conflict candidate, not a duplicate.

    The head alone is not enough: two facts that open with the same template
    ("Notifications for the team go to thread 42…" / "…thread 437, from Monday")
    share 40 characters and differ in everything that matters. So a head hit
    must land in an entry that carries every number of the candidate and most
    of its stems — otherwise it is left to the conflict detector."""
    norm = _norm(content)
    head = norm[:HEAD_CHARS]
    if head:
        item_stems = _stems(sig_tokens(content))
        for chunk in _memory_chunks(memory_text):
            chunk_norm = _norm(chunk)
            if head not in chunk_norm:
                continue
            if polarity_differs(content, chunk):
                continue  # "use X" inside "do not use X" is a flip, not a copy (Codex review of 2.7.5)
            if norm in chunk_norm:
                return True
            if not (_numbers(content) <= _numbers(chunk)):
                continue
            chunk_stems = _stems(sig_tokens(chunk))
            if (not item_stems
                    or len(item_stems & chunk_stems) / len(item_stems) >= HEAD_MATCH_MIN_STEM_RATIO):
                return True
    return _semantic_memory_alias(content, memory_text)


def already_in_memory(content, memory_text, token_threshold=0.62):
    """True when `content` is already represented in durable memory.

    Exact first-40-char match catches identical facts; alias rules cover
    reworded recurring facts; stemmed containment (both directions, per memory
    chunk rather than the union) catches the remaining near-duplicates."""
    if exact_or_alias_in_memory(content, memory_text):
        return True
    item_tokens = sig_tokens(content)
    if len(item_tokens) < 4:
        return False
    item_stems = _stems(item_tokens)
    for chunk in _memory_chunks(memory_text):
        chunk_stems = _stems(sig_tokens(chunk))
        if ((_covers(item_stems, chunk_stems, token_threshold)
             or _same_subject(item_stems, chunk_stems, content, chunk))
                and not polarity_differs(content, chunk)):
            return True
    return False


# ---------------------------------------------------------------------------
# Possible contradictions / supersession
# ---------------------------------------------------------------------------
# A candidate that overlaps a memory entry strongly but disagrees on numbers or
# dates is most likely an UPDATE of that entry (thread 42 → 437, a new price, a
# moved deadline), not a new fact. mem0/Zep resolve this with an LLM
# (ADD/UPDATE/DELETE, edge invalidation); here it is a deterministic hint: the
# agent decides, the entry is never rewritten by the script.
CONFLICT_MIN_OVERLAP = 0.45
CONFLICT_MIN_SHARED = 4
# A number's edge is a letter of a spaced alphabet: Chinese, Japanese, Korean and Thai write it right against
# the words (每月500美元, ราคา800บาท), and with a bare \w edge those texts had no numbers at all — so no conflicts.
_NUM_EDGE = f"(?![{_HAN_KANA}{_HANGUL}{_SEA}])"
NUMBER_RE = re.compile(rf"(?<!{_NUM_EDGE}[\w.])\d[\d.,:/-]*\d(?!{_NUM_EDGE}\w)|(?<!{_NUM_EDGE}\w)\d(?!{_NUM_EDGE}\w)")


# A fact that opens with its own date stamp ("2026-06-19 X said that …") is
# dated provenance, not content: the stamp must not count as a number when the
# fact is compared with the entry written from it, or every stamped fact would
# come back as "different numbers" (a candidate the night after it was saved,
# a conflict with the entry it produced).
LEADING_DATE_STAMP_RE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}(?:[ ,:—-]+|$)")


# Thousands separators: "25 000", "25\u00a0000", "25,000" and "25000" are one
# number, and seeing them as four made every retelling of a price a conflict.
THOUSANDS_RE = re.compile(r"(?<=\d)[\s\u00a0\u202f,](?=\d{3}(?!\d))")
# A month said in words is as much a "number" as 15.04 — otherwise moving
# «15 марта» to «15 апреля» changed nothing comparable, and the fuzzy dedupe
# swallowed it as "already in memory" (external review, 2026-10-07).
# from dream_lang.py (3.0)
(OTHER_MONTHS, OTHER_MONTHS_RE, DAY_MONTH_RE, CJK_DATE_RE, KO_DATE_RE, RU_MONTH_WORD_RE, RU_MONTH_NUM,
 AMBIGUOUS_MONTHS) = (
    _LANG.OTHER_MONTHS, _LANG.OTHER_MONTHS_RE, _LANG.DAY_MONTH_RE, _LANG.CJK_DATE_RE, _LANG.KO_DATE_RE,
    _LANG.RU_MONTH_WORD_RE, _LANG.RU_MONTH_NUM, _LANG.AMBIGUOUS_MONTHS)
# Month names are looked up without case and diacritics: IGNORECASE lets "KASIM" match «kasım», but "KASIM".lower()
# is "kasim", which was not a key — the pass died on a KeyError (review 2026-10-13).
_OTHER_MONTHS_FOLDED = {latin_fold(name): num for name, num in OTHER_MONTHS.items()}


def other_month(word):
    """Month number of a month name in one of the OTHER_MONTHS languages, any case; None when it is not one."""
    return _OTHER_MONTHS_FOLDED.get(latin_fold((word or "").lower()))


OTHER_MONTH_WORD_RE = re.compile(rf"(?<!\w)({OTHER_MONTHS_RE})(?!\w)", re.IGNORECASE)


def _month_markers(text):
    """`month:MM` for every month named in words (and for 11月 / 11월 in a date)."""
    text = text or ""
    nums = set()
    for m in RU_MONTH_WORD_RE.finditer(text):
        nums.add(RU_MONTH_NUM[next(g for g in m.groups() if g).lower()])
    for m in OTHER_MONTH_WORD_RE.finditer(text):
        month = other_month(m.group(1))
        if month and latin_fold(m.group(1).lower()) not in AMBIGUOUS_MONTHS:
            nums.add(month)
    for m in DAY_MONTH_RE.finditer(text):
        month = other_month(m.group(2))
        if month:
            nums.add(month)
    for m in EN_DATE_RE.finditer(text):
        nums.add(EN_MONTHS[m.group(1).lower()[:3]])
    for regex in (CJK_DATE_RE, KO_DATE_RE):
        for m in regex.finditer(text):
            if 1 <= int(m.group(2)) <= 12:
                nums.add(int(m.group(2)))
    return {f"month:{n:02d}" for n in nums}


# from dream_lang.py (3.0)
(_WEEKDAY_NAMES, _WEEKDAY_PREFIXES_NUM, _WEEKDAY_WORD_RE, _CJK_WEEKDAY_RE, _CJK_WEEKDAY_NUM) = (
    _LANG._WEEKDAY_NAMES, _LANG._WEEKDAY_PREFIXES_NUM, _LANG._WEEKDAY_WORD_RE, _LANG._CJK_WEEKDAY_RE,
    _LANG._CJK_WEEKDAY_NUM)


# The cells of _WEEKDAY_PREFIXES_NUM looked up instead of tried one by one (3.7.0): every word of every fact went
# through all ~100 of them, and the whole-store duplicate search spent most of a large store's time there. A word
# takes the day of the first cell that names it, in the list's own order, exactly as the loop did.
_WEEKDAY_WHOLE, _WEEKDAY_PREFIX = {}, {}
for _pos, (_cell, _day) in enumerate(_WEEKDAY_PREFIXES_NUM):
    _WEEKDAY_WHOLE.setdefault(_cell.rstrip("="), (_pos, _day))
    if len(_cell) >= 5 and not _cell.endswith("="):
        _WEEKDAY_PREFIX.setdefault(_cell, (_pos, _day))
_WEEKDAY_PREFIX_LENS = sorted({len(p) for p in _WEEKDAY_PREFIX})


@functools.lru_cache(maxsize=65536)
def _weekday_of(word):
    """The day (1 = Monday) a lowercased, Latin-folded word names, or None."""
    hits = [_WEEKDAY_WHOLE.get(word)] + [_WEEKDAY_PREFIX.get(word[:k]) for k in _WEEKDAY_PREFIX_LENS if len(word) >= k]
    hits = [h for h in hits if h]
    return min(hits)[1] if hits else None


def _weekday_markers(text):
    """`weekday:N` (1 = Monday) for every day of the week named in the text."""
    days = set()
    for m in _WEEKDAY_WORD_RE.finditer(text or ""):
        n = _weekday_of(latin_fold(m.group(0).lower()))
        if n is not None:
            days.add(n)
    for m in _CJK_WEEKDAY_RE.finditer(text or ""):
        ch = next(g for g in m.groups() if g)
        days.add(7 if m.group(2) == "日" else _CJK_WEEKDAY_NUM[ch])
    return {f"weekday:{n}" for n in days}


def _numbers(text):
    body = LEADING_DATE_STAMP_RE.sub("", text or "", count=1)
    normalized = THOUSANDS_RE.sub("", _norm(body))
    nums = {n.strip(".,:") for n in NUMBER_RE.findall(normalized)}
    return {n for n in nums if n} | _month_markers(normalized) | _weekday_markers(normalized)


def _number_kinds(text):
    """The numbers of *text* (as _numbers) grouped by kind: times, dates, whole numbers by size, months, weekdays —
    a number is compared with numbers of its own kind only ("18 октября" is no other weekday, "2 сетки" no other
    time)."""
    body = LEADING_DATE_STAMP_RE.sub("", text or "", count=1)
    normalized = THOUSANDS_RE.sub("", _norm(body))
    out = {}
    for n in {n.strip(".,:") for n in NUMBER_RE.findall(normalized)}:
        if not n:
            continue
        kind = "time" if ":" in n else "date" if any(c in n for c in "./-") else (
            "int1" if len(n) <= 2 else "int3" if len(n) <= 4 else "int5")
        if kind == "time" or (kind == "date" and len(re.findall(r"[./-]", n)) >= 2):
            # 09:00 is 9:00, 07.03.2026 is 7.3.2026 (Codex review); a lone dot may be a decimal ("1.01" is no "1.1",
            # second Codex pass) and is compared as written.
            n = re.sub(r"\d+", lambda m: str(int(m.group())), n)
        out.setdefault(kind, set()).add(n)
    for kind, found in (("month", _month_markers(normalized)), ("weekday", _weekday_markers(normalized))):
        if found:
            out[kind] = set(found)
    return out


# A message that gives the same thing a new number says the fact no longer holds: «абонемент теперь 1500» under
# «1200 рублей в месяц», «тихий режим с 00:30» under «между 23:30 и 06:30». It corroborated the old number (0 of 14 such
# pairs; Laya experiment, 2026-10-22). Changed: for some kind, the person's own words hold a number the fact lacks
# and lack one it holds — a number restated beside an extra one ("в 19:00, взяли 2 сетки") is no change.
def _message_number_kinds(text):
    """The numbers of message *text* by kind (_number_kinds): its reply quote and [bracketed] metadata left out, as
    for the tokens."""
    source = re.sub(r"\[[^\]]*\]", " ", _own_words(text))
    return {kind: frozenset(nums) for kind, nums in _number_kinds(source).items()}


def _fact_number_kinds(text):
    """The numbers of a fact's *text* by kind (_number_kinds)."""
    return {kind: frozenset(nums) for kind, nums in _number_kinds(text).items()}


def _numbers_differ(own, fact):
    """For some kind, the message's numbers *own* hold one the fact's *fact* lack and lack one they hold."""
    return any(own.get(kind) and (own[kind] - nums) and (nums - own[kind]) for kind, nums in fact.items())


def numbers_changed(content, text):
    """True when *text* (a message; its reply quote and [bracketed] metadata left out, as for the tokens) changes a
    number of the fact *content*. The rule for two texts; the nightly count reads a message once a pass instead
    (_message_contradicts)."""
    return _numbers_differ(_message_number_kinds(text), _fact_number_kinds(content))


def contradicts(content, text):
    """A message that says the fact otherwise: the opposite polarity, or a changed number. It corroborates nothing."""
    return polarity_differs(content, text) or numbers_changed(content, text)


# A pass reads every message once (3.7.1). The count checks each fact against each message of the window, and what
# a message is made of — its stems and rare words, a reply's own words, its numbers, what it negates, its sentences
# and their stems — is the same for every fact. 3.6.1 kept a message's numbers in a cache of 4 096 entries and 4 MiB
# of text that stopped taking new ones when full: a busy desktop's window (1 562 messages, 17.6 MiB) filled it, every
# message past it was read again for every fact, and the nightly pass outran the gate's 600 s (killed at 34 min).
# With the numbers read once, the negation check was next: it split a message into sentences and took the stems of
# each, again for every fact (5 min 44 s). Issue #3, from the measurements of @sprd-tmu. Now a pass keeps what it read
# from each message, keyed by the message (the dict the pass loaded), never by its text: sets of stems, numbers by
# kind, where each sentence starts and ends and which sentences hold which stem (a sorted array of integers) — not
# the text again. What it keeps is held under `corroboration.memory_mb` (an estimate of its bytes): past it, the parts
# that cost least time per byte to read again are let go first, and read again when a fact needs them — slower,
# never another answer; nothing stops being kept because the limit was reached once. It is dropped with the pass.
# Every decision is the one the rules for two texts give (polarity_flip, numbers_changed);
# tests/test_message_pass_371.py holds the two side by side.
_PASS_DATA = contextvars.ContextVar("dream_pass_data", default=None)
_PENDING = object()  # a part not read yet (None is an answer: "no reply quote", "nothing unsafe")
# Bytes a record and a kept part cost besides their values: the object, its entries in the pass's dicts.
_RECORD_BYTES, _PART_BYTES = 400, 200


def _footprint(value):
    """Bytes *value* holds, on the high side: the object and the strings, numbers and containers in it (a string
    several parts share — a word common to many messages — is counted in each)."""
    size = sys.getsizeof(value)
    if isinstance(value, dict):
        return size + sum(_footprint(k) + _footprint(v) for k, v in value.items())
    if isinstance(value, (tuple, list, set, frozenset)):
        if set(map(type, value)) == {str}:  # a set of stems, the words of a sentence: sized in one sweep
            return size + sum(map(sys.getsizeof, value))
        return size + sum(_footprint(v) for v in value)
    return size


class _PassData:
    """What one pass has read from messages and from the texts of facts, held under *limit* bytes (estimated)."""
    __slots__ = ("messages", "facts", "limit", "held", "parts", "order", "kept_parts", "evictions")

    def __init__(self, limit):
        self.messages, self.facts, self.parts = {}, {}, {}
        self.order = []  # a heap of (seconds per byte, number, key): the part cheapest to read again on top
        self.limit, self.held, self.kept_parts, self.evictions = limit, 0, 0, 0

    def add(self, table, key, record):
        old = table.get(key)
        if old is not None:
            self.forget(old)
        table[key] = record
        record.keeper, record.home, record.parts = self, (table, key), 0
        self.held += _RECORD_BYTES

    def kept(self, record, name, size, seconds):
        """*record* now holds part *name* of *size* bytes, read in *seconds*; past the limit, let parts go."""
        size += _PART_BYTES
        key, number = (id(record), name), self.kept_parts
        self.kept_parts += 1
        self.parts[key] = (size, record, name, number)
        heapq.heappush(self.order, (seconds / size, number, key))
        record.parts += 1
        self.held += size
        # The cheapest to read again per byte go first — the sets of a long paste before its numbers and sentences,
        # and a part read again just now before any kept since long (it is the one that costs least). A window that
        # does not fit keeps the same parts fact after fact and reads the rest again; nothing stops being kept. The
        # record being read may lose a part too: its caller keeps the value it was given.
        while self.held > self.limit and self.order:
            _density, number, key = heapq.heappop(self.order)
            entry = self.parts.get(key)
            if entry is not None and entry[3] == number:  # else let go already
                self._let_go(entry[1], entry[2], entry[0])

    def _let_go(self, record, name, size):
        del self.parts[(id(record), name)]
        setattr(record, name, _PENDING)
        self.held -= size
        self.evictions += 1
        record.parts -= 1
        if record.parts == 0:
            self._drop(record)

    def forget(self, record):
        """Let go of *record* and all it holds (a message given another text, or read under other settings)."""
        if record.keeper is not self:
            return
        for key in [k for k in self.parts if k[0] == id(record)]:
            size, _record, name, _number = self.parts.pop(key)
            setattr(record, name, _PENDING)
            self.held -= size
        self._drop(record)

    def close(self):
        """Let go of everything at the end of the pass, at once: records point back at the pass, and without this the
        memory would wait for the garbage collector."""
        for record in [*self.messages.values(), *self.facts.values()]:
            record.keeper = record.home = None
        self.messages.clear()
        self.facts.clear()
        self.parts.clear()
        self.order.clear()
        self.held = 0

    def _drop(self, record):
        table, key = record.home
        if table.get(key) is record:
            del table[key]
        record.keeper = record.home = None
        record.parts = 0
        self.held -= _RECORD_BYTES


def _pass_limit():
    return int(SETTINGS.PASS_MEMORY_MB * 1024 * 1024)


def _message_pass(fn):
    """Make *fn* one pass: what is read from messages (and from the texts of facts) is kept until the outermost such
    call returns, and dropped then — also when it raises."""
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        if _PASS_DATA.get() is not None:
            return fn(*args, **kwargs)
        data = _PassData(_pass_limit())
        token = _PASS_DATA.set(data)
        try:
            return fn(*args, **kwargs)
        finally:
            _PASS_DATA.reset(token)
            data.close()
    return wrapped


class _Kept:
    """A record whose parts are read when first asked for and kept by the pass (see _PassData)."""
    __slots__ = ("keeper", "home", "parts")

    def _part(self, name, read):
        value = getattr(self, name)
        if value is _PENDING:
            started = time.perf_counter()
            value = read()
            setattr(self, name, value)
            if self.keeper is not None:
                self.keeper.kept(self, name, _footprint(value), time.perf_counter() - started)
        return value


class _PolaritySide(namedtuple("_PolaritySide", "words negations neg_stems two_way norm cjk")):
    """What the negation check reads from one side of a pair (see polarity_flip): its words, what they negate, the
    stems a negation is compared by, the stems said both ways, the normalized text and what it negates in Chinese."""
    __slots__ = ()


def _polarity_side(text):
    words = tuple(_plain_words(text))  # _words_of without its cache: a message's side is kept by the pass
    negations = _negations.__wrapped__(words)
    norm = _norm(text)
    return _PolaritySide(words, negations, {_neg_stem(w) for w in words}, _two_way(words, negations), norm,
                         _cjk_negated(norm))


def _flip_sides(mine, theirs):
    """polarity_flip for two sides already read: the fact's (*mine*) and the message's or entry's (*theirs*)."""
    both = mine.neg_stems & theirs.neg_stems
    two_way = mine.two_way | theirs.two_way
    a = _negated_by(mine.words, mine.negations, both, two_way)
    b = _negated_by(theirs.words, theirs.negations, both, two_way)
    a.update(_cjk_flips(mine.cjk, theirs.norm))
    b.update(_cjk_flips(theirs.cjk, mine.norm))
    if set(a) == set(b):
        return None
    return sorted(a.values()), sorted(b.values())


class _Sentences(namedtuple("_Sentences", "count whole spans sizes index")):
    """The sentences polarity_flip finds in a message: how many (blank ones left out); the side of the one sentence
    (or of the text, with none); else where each starts and ends, how many stems each has, and which sentence holds
    which stem — (hash of the stem, 32 bits) << 32 | number of the sentence, sorted."""
    __slots__ = ()


def _read_sentences(chunk, generation):
    pieces, start = [], 0  # the pieces _SENTENCE_SPLIT_RE.split gives, as (start, end)
    for found in _SENTENCE_SPLIT_RE.finditer(chunk):
        if chunk[start:found.start()].strip():
            pieces.append((start, found.start()))
        start = found.end()
    if chunk[start:].strip():
        pieces.append((start, len(chunk)))
    if len(pieces) <= 1:
        return _Sentences(len(pieces), _polarity_side(chunk[pieces[0][0]:pieces[0][1]] if pieces else chunk),
                          None, None, None)
    sizes, index = array("I"), []
    for i, (a, b) in enumerate(pieces):
        stems = _sentence_stems.__wrapped__(chunk[a:b], generation)
        sizes.append(len(stems))
        index.extend((hash(s) & 0xFFFFFFFF) << 32 | i for s in stems)
    index.sort()
    return _Sentences(len(pieces), None, array("I", (p for span in pieces for p in span)), sizes, array("Q", index))


def _like_sentences(sentences, item, chunk, generation):
    """The sentences (in their order) whose stems are as near the stems *item* of a fact as polarity_flip demands
    (Jaccard ≥ POLARITY_MIN_JACCARD). Counted by the hashes of the stems first, which may only overcount (two stems
    may share a hash, none is missed), with the union counted, not built: |item ∪ s| = |item| + |s| − |item ∩ s|.
    A sentence the count lets through is checked by its own stems, as polarity_flip checks it."""
    shared, index = {}, sentences.index
    for h in {hash(s) & 0xFFFFFFFF for s in item}:
        lo = bisect.bisect_left(index, h << 32)
        for k in index[lo:bisect.bisect_left(index, (h + 1) << 32, lo)]:
            i = k & 0xFFFFFFFF
            shared[i] = shared.get(i, 0) + 1
    n, spans, keep = len(item), sentences.spans, []
    for i in sorted(shared):
        k = shared[i]
        if k / (n + sentences.sizes[i] - k) < POLARITY_MIN_JACCARD:
            continue  # its Jaccard is no higher than this
        st = _sentence_stems.__wrapped__(chunk[spans[2 * i]:spans[2 * i + 1]], generation)
        if st and item and len(item & st) / len(item | st) >= POLARITY_MIN_JACCARD:
            keep.append(i)
    return keep


class _MessageData(_Kept):
    """What one pass has read from one message, each part when first asked for (_message_data)."""
    __slots__ = ("m", "content", "tokens", "generation", "_stems", "_wide", "_distinctive", "_own", "_own_wide",
                 "_numbers", "_negates", "_sentences", "_unsafe", "_snippet", "__weakref__")

    def __init__(self, m, generation):
        # The message itself is held, so its id is nobody else's while the pass keeps this.
        self.m, self.content, self.tokens, self.generation = m, m.get("content"), m.get("tokens"), generation
        self.keeper = self.home = None
        self.parts = 0
        self._stems = self._wide = self._distinctive = self._own = self._own_wide = self._numbers = _PENDING
        self._negates = self._sentences = self._unsafe = self._snippet = _PENDING

    def stems(self):
        return self._part("_stems", lambda: _stems(self.tokens))

    def wide_stems(self):
        """Its stems with those of its words read wide, against a fact of short words."""
        return self._part("_wide", lambda: self.stems() | _wide_stems.__wrapped__(self.content or "", self.generation))

    def has_distinctive(self, wanted):
        """Whether one of its words has a distinctive stem of *wanted* (a fact's, from _distinctive_stem)."""
        # Such a word has that stem among the message's stems: "other" is _stem itself, and a Latin word's stem is the
        # first STEM_CHARS_LATIN of its DISTINCTIVE_PREFIX_LATIN. A message with none of them is answered without
        # reading (nor keeping) its distinctive stems — most messages, for most facts.
        stems = self.stems()
        if not any((s[:STEM_CHARS_LATIN] in stems if STEM_CHARS_LATIN <= DISTINCTIVE_PREFIX_LATIN else True)
                   if script == "latin" else s in stems for script, s in wanted):
            return False
        by_script = self._part("_distinctive", self._read_distinctive)
        return any(s in by_script[script] for script, s in wanted)

    def _read_distinctive(self):
        by_script = {"latin": set(), "other": set()}  # the only two tags of _distinctive_stem
        for t in self.tokens:
            if not is_gram(t):
                script, stem = _distinctive_stem(t)
                by_script[script].add(stem)
        return by_script

    def own_stems(self, wide):
        """_own_stems: the stems of a reply's own words, None for a message without a reply quote."""
        return self._part("_own_wide" if wide else "_own",
                          lambda: _own_stems.__wrapped__(self.content or "", self.generation, wide))

    def numbers(self):
        return self._part("_numbers", lambda: _message_number_kinds(self.content))

    def negates(self):
        return self._part("_negates", lambda: _negates_anything.__wrapped__(self.content or ""))

    def sentences(self):
        return self._part("_sentences", lambda: _read_sentences(self.content or "", self.generation))

    def unsafe(self):
        return self._part("_unsafe", lambda: classify_unsafe(self.content))

    def snippet(self):
        return self._part("_snippet", lambda: _snippet(self.content))


def _message_data(m):
    """What this pass has read from message *m*; outside a pass, a reading of its own."""
    generation = (SETTINGS_GENERATION, id(SETTINGS))
    data = _PASS_DATA.get()
    if data is None:
        return _MessageData(m, generation)
    d = data.messages.get(id(m))
    if (d is None or d.generation != generation or d.content is not m.get("content")
            or d.tokens is not m.get("tokens")):
        d = _MessageData(m, generation)
        data.add(data.messages, id(m), d)
    return d


class _FactData(_Kept):
    """What one pass has read from the text of a fact (or a memory entry) for the negation and number checks."""
    __slots__ = ("content", "generation", "_negates", "_side", "_item", "_numbers")

    def __init__(self, content, generation):
        self.content, self.generation = content, generation
        self.keeper = self.home = None
        self.parts = 0
        self._negates = self._side = self._item = self._numbers = _PENDING

    def negates(self):
        return self._part("_negates", lambda: _negates_anything(self.content or ""))

    def side(self):
        return self._part("_side", lambda: _polarity_side(self.content or ""))

    def item(self):
        return self._part("_item", lambda: _sentence_stems(self.content or "", self.generation))

    def numbers(self):
        return self._part("_numbers", lambda: _fact_number_kinds(self.content))


def _fact_data(content):
    generation = (SETTINGS_GENERATION, id(SETTINGS))
    data = _PASS_DATA.get()
    if data is None:
        return _FactData(content, generation)
    f = data.facts.get((content, generation))
    if f is None:
        f = _FactData(content, generation)
        data.add(data.facts, (content, generation), f)
    return f


def _message_flip(content, d):
    """polarity_flip(content, the message's text), from what the pass read from the message: its sentences near the
    fact, found by the stems they share with it, or the one sentence it is."""
    f = _fact_data(content)
    if not (f.negates() or d.negates()):
        return None
    sentences = d.sentences()
    if sentences.count > 1:
        keep = _like_sentences(sentences, f.item(), d.content, d.generation)
        if not keep:
            return None
        spans = sentences.spans
        theirs = _polarity_side(". ".join(d.content[spans[2 * i]:spans[2 * i + 1]] for i in keep))
    else:
        theirs = sentences.whole
    return _flip_sides(f.side(), theirs)


def _message_contradicts(content, d):
    """contradicts(content, the message's text) for the message read as *d* (_message_data)."""
    return _message_flip(content, d) is not None or _numbers_differ(d.numbers(), _fact_data(content).numbers())


def find_conflicts(content, memory_text):
    """Memory chunks that look like the same subject with different numbers/dates,
    or with the opposite negation (see polarity_differs)."""
    item_stems = _stems(sig_tokens(content))
    if len(item_stems) < CONFLICT_MIN_SHARED:
        return []
    item_nums = _numbers(content)
    out = []
    for chunk in _memory_chunks(memory_text):
        chunk_stems = _stems(sig_tokens(chunk))
        if not chunk_stems:
            continue
        shared = item_stems & chunk_stems
        overlap = len(shared) / min(len(item_stems), len(chunk_stems))
        if len(shared) < CONFLICT_MIN_SHARED or overlap < CONFLICT_MIN_OVERLAP:
            continue
        # An entry holding a secret or an instruction shows nothing of itself: its numbers and negated words
        # are its text too (a PIN went out as memory_numbers; Codex review of 2.7.5).
        hidden = {"entry": ENTRY_REDACTED, "replace_unsafe": True} if classify_unsafe(chunk) else None
        flip = polarity_flip(content, chunk)
        if flip:
            out.append(dict(hidden, fact_negations=flip[0]) if hidden else
                       {"entry": _safe_preview(chunk, 160, ENTRY_REDACTED),
                        "fact_negations": flip[0], "memory_negations": flip[1]})
            continue
        if not item_nums:
            continue
        chunk_nums = _numbers(chunk)
        if chunk_nums and chunk_nums != item_nums and not (item_nums <= chunk_nums):
            out.append(dict(hidden, fact_numbers=sorted(item_nums - chunk_nums)) if hidden else
                       {"entry": _safe_preview(chunk, 160, ENTRY_REDACTED),
                        "fact_numbers": sorted(item_nums - chunk_nums),
                        "memory_numbers": sorted(chunk_nums - item_nums)})
    return out


# ---------------------------------------------------------------------------
# Reject list: facts a human already called outdated / wrong
# ---------------------------------------------------------------------------
# Without it the dream has no way to close a question: the core `memory` tool
# edits MEMORY.md/USER.md only and cannot delete a row from memory_store.db,
# and the only repeat filter is "already in durable memory". A rejected fact is
# in neither, so it came back every night (live case: 12 identical questions
# about a dead thread in 5 weeks). Matching is by TEXT, not fact_id: the core
# may re-extract the same fact under a new id, and a generic `sqlite` source
# without AUTOINCREMENT reuses the id of a deleted last row (holographic's
# `facts.fact_id` is AUTOINCREMENT and never reuses one). fact_id is kept for
# tracing only.

# ---------------------------------------------------------------------------
# Near-duplicates inside the fact store
# ---------------------------------------------------------------------------
# The store has no dedupe of its own beyond an exact UNIQUE(content): the same
# statement extracted twice half an hour apart ("X said that …" / "X said: …")
# is two rows, and both arrived as two candidates for one memory entry. The
# dream never deletes from the store; it shows one representative per group
# and lists the others in `duplicates`.
#
# Deliberately stricter than memory dedupe: compared are ALL words, not the
# signature tokens — the tokenizer drops short words, and "profile de" vs
# "profile kz" would otherwise be one fact. Every word one side has and the
# other lacks must be a stopword ("said that X" / "said: X") or an inflection
# of a word the other side has (same 5-char stem); numbers and short codes never
# are, so two prices or two dates stay apart. A leading date stamp is provenance
# and is ignored.
STORE_DUP_MIN_SHARED = 4
_WORD_RE = re.compile(r"\w[\w\-]*")


def _store_words(text):
    body = _norm(LEADING_DATE_STAMP_RE.sub("", text or "", count=1)).replace("ё", "е")
    return set(_WORD_RE.findall(body))


def _store_duplicates(a, b):
    wa, wb = _store_words(a), _store_words(b)
    if len(wa & wb) < STORE_DUP_MIN_SHARED:
        return False
    stop = {s.replace("ё", "е")
            for s in SETTINGS.STOPWORDS - TEMPORAL_STOP - POLARITY_STOP}
    for mine, other in ((wa - wb, wb), (wb - wa, wa)):
        other_stems = {_stem(w) for w in other if len(w) >= 5}
        for w in mine:
            # A word with a digit is never an inflection: "2026-10-20" and "2026-09-15" share the stem "2026-", and two
            # dates of one recurring event were folded into one fact (audit 2026-10-13).
            if w in stop or (len(w) >= 5 and not any(c.isdigit() for c in w) and _stem(w) in other_stems):
                continue
            return False
    return True


def collapse_store_duplicates(items):
    """Keep the first fact of every near-duplicate group (callers sort
    best-first). Returns (kept, hidden); kept items are copies carrying
    `duplicates` (fact ids) — the input dicts are shared between sections."""
    kept, hidden = [], []
    for s in items:
        for k in kept:
            if _store_duplicates(k["content"], s["content"]):
                k.setdefault("duplicates", []).append(s["fact_id"])
                hidden.append(s)
                break
        else:
            kept.append(dict(s))
    return kept, hidden


def _fact_fingerprint(content):
    return hashlib.md5(_norm(content).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Duplicates across the whole store (3.4.0): the similarity rule
# ---------------------------------------------------------------------------
# `_store_duplicates` above is strict on purpose (every differing word must be a stopword or an inflection) and
# sees only the candidates of one night; a retelling of a stored fact ("Thread 30 is for V's work tasks" /
# "Thread 30: messages there are work tasks of V") never met it. This rule is looser and is a generator of
# QUESTIONS for a human, not a test of identity: it finds pairs that look like one fact, and the human decides on
# the two whole texts. What it cannot tell apart (two names in swapped roles, the tense of one verb) is written
# in references/scoring.md as a limit, not hidden. A pair is a candidate when all of these hold:
# - significant words: every word of the body (the leading date stamp and words with a digit left out, stopwords
#   KEPT, a word of 5+ letters by its stem) — containment |A∩B| / min(|A|,|B|) >= `duplicates.min_containment`;
# - a short fact (fewer than DUP_SHORT_WORDS significant words on the shorter side) pairs only at containment 1.0
#   with the same non-empty numbers / @-handles; a fact without significant words pairs only with its own text;
#   two texts equal but for case and spaces are a pair whatever their length;
# - the numbers (`_numbers`: digits, months and weekdays in words) and @-handles of one side are a subset of the
#   other's, and the shared ones come in the same order («в 71 доходы, в 74 расходы» against the swap);
# - names (capitalized words, the first of a sentence included) of one side are a subset of the other's, and
#   point the same way as the numbers when both differ («Виктор и Ольга ведут ветку 30» / «Виктор ведёт ветки 30
#   и 31»: names added on one side, numbers on the other);
# - a word substitution is no retelling: when each side has words the other lacks and there are at most
#   DUP_SUBSTITUTION_MAX of them on each side, one word was swapped («утром» / «вечером», «разрешает» /
#   «запрещает», «мама» / «папа», «до» / «после»);
# - no opposite polarity (`polarity_differs`), checked last: it is the dearest test.
_HANDLE_RE = re.compile(r"@\w+")
DUP_SHORT_WORDS = 3
DUP_SUBSTITUTION_MAX = 2


def _dup_body(text):
    return LEADING_DATE_STAMP_RE.sub("", text or "", count=1)


def _dup_words(text):
    """{stem: a word it stands for} of the significant words of *text* (see the section comment)."""
    out = {}
    for w in sorted(_store_words(text)):  # the stamp is already off, the text normalized
        if any(c.isdigit() for c in w):
            continue
        out.setdefault(_stem(w) if len(w) >= 5 else w, w)
    return out


def _dup_anchors(text):
    """The numbers (as `_numbers`, the leading date stamp left out) and @-handles of the body, as a set."""
    return _numbers(text) | {m.group() for m in _HANDLE_RE.finditer(_norm(_dup_body(text)))}


def _dup_anchor_seq(text):
    """The digit numbers and @-handles of the body in order of first occurrence (months and weekdays in words
    have no position here: they take part in the subset test only)."""
    normalized = THOUSANDS_RE.sub("", _norm(_dup_body(text)))
    found = [(m.start(), m.group().strip(".,:")) for m in NUMBER_RE.finditer(normalized)]
    found += [(m.start(), m.group()) for m in _HANDLE_RE.finditer(normalized)]
    seq = []
    for _, anchor in sorted(found):
        if anchor and anchor not in seq:
            seq.append(anchor)
    return seq


def _dup_name_seq(text):
    """Capitalized words of the body (the first word of a sentence too — a cost in recall, see scoring.md), folded
    and stemmed, in order of first occurrence."""
    seq = []
    for tok in _WORD_RE.findall(_dup_body(text)):
        if tok[0].isupper() and not any(c.isdigit() for c in tok):
            low = _fold(tok).lower()
            name = _stem(low) if len(low) >= 5 else low
            if name not in seq:
                seq.append(name)
    return seq


def _same_order(seq_a, seq_b):
    shared = set(seq_a) & set(seq_b)
    return [x for x in seq_a if x in shared] == [x for x in seq_b if x in shared]


def _dup_directions(a, b):
    return {d for d, ok in (("a", a <= b), ("b", b <= a)) if ok}


DupFeatures = namedtuple("DupFeatures", "text norm word_of words anchors anchor_seq name_seq names")


def _dup_features(text, shared=None):
    """What the rule compares of one text, computed once per fact (a night compares a fact with many). *shared*: a
    dict that keeps one copy of each word, stem, number and name across the facts of a search — a large store
    repeats them in almost every fact, and their copies were a third of the search's memory."""
    one = (lambda x: shared.setdefault(x, x)) if shared is not None else (lambda x: x)
    word_of = {one(stem): one(word) for stem, word in _dup_words(text).items()}
    names = tuple(one(n) for n in _dup_name_seq(text))
    return DupFeatures(text, _norm(text), word_of, frozenset(word_of), frozenset(one(a) for a in _dup_anchors(text)),
                       tuple(one(a) for a in _dup_anchor_seq(text)), names, frozenset(names))


def _dup_judge(fa, fb):
    """(pair?, why, containment) of two `_dup_features` (see the section comment)."""
    wa, wb = fa.words, fb.words
    if fa.norm == fb.norm:
        return True, "the same text", 1.0
    smaller = min(len(wa), len(wb))
    cont = len(wa & wb) / smaller if smaller else 0.0
    na, nb = fa.anchors, fb.anchors
    if smaller < DUP_SHORT_WORDS:
        if not (cont >= 1.0 and na and na == nb):
            return False, "short: not the same words and numbers", cont
    else:
        if cont < SETTINGS.DUP_MIN_CONTAINMENT:
            return False, f"containment {cont:.2f}", cont
        only_a, only_b = wa - wb, wb - wa
        if only_a and only_b and len(only_a) <= DUP_SUBSTITUTION_MAX and len(only_b) <= DUP_SUBSTITUTION_MAX:
            return False, (f"a word substituted: {', '.join(sorted(fa.word_of[w] for w in only_a))} / "
                           f"{', '.join(sorted(fb.word_of[w] for w in only_b))}"), cont
    nd = _dup_directions(na, nb)
    if not nd:
        return False, f"numbers differ: {', '.join(sorted(na - nb))} / {', '.join(sorted(nb - na))}", cont
    if not _same_order(fa.anchor_seq, fb.anchor_seq):
        return False, "the shared numbers come in another order", cont
    ma, mb = fa.names, fb.names
    md = _dup_directions(ma, mb)
    if not md:
        return False, f"names differ: {', '.join(sorted(ma - mb))} / {', '.join(sorted(mb - ma))}", cont
    if na != nb and ma != mb and not (nd & md):
        return False, "names and numbers are added on opposite sides", cont
    if polarity_differs(fa.text, fb.text) or polarity_differs(fb.text, fa.text):
        return False, "polarity differs", cont
    why = [f"containment {cont:.2f}"]
    if na & nb:
        why.append(f"numbers {', '.join(sorted(na & nb))} shared")
    if ma == mb and len(ma) >= 2 and not _same_order(fa.name_seq, fb.name_seq):
        why.append("the names come in another order")
    return True, "; ".join(why), cont


def duplicate_pair(a, b):
    """Whether two fact texts look like one fact: (True, why) or (False, why). `why` names the containment and the
    shared numbers for the human's question; a difference in the order of names, which the rule cannot judge, is
    named there too."""
    ok, why, _cont = _dup_judge(_dup_features(a), _dup_features(b))
    return ok, why


class DupBudget(Exception):
    """The night's budget of pair checks (`duplicates.max_pair_checks`) ran out."""


def _rarest(items, df):
    return min(items, key=lambda x: (df[x], x))


def find_duplicate_pairs(facts, max_checks=None):
    """[{"a", "b", "why", "containment"}] (fact ids, a < b by id) of every pair of *facts* ({fact_id, content}) the
    rule calls one fact, and how many pairs were checked. Raises DupBudget past *max_checks*.

    The rule decides every pair; the indexes below only choose which pairs it is shown, and each of them leaves out
    only pairs the rule is sure to reject, so the pairs found are the same as when every pair is checked:
    - stems (3.4.0): a pair the rule can accept shares at least need = max(1, ceil(min_containment · |S|)) stems
      with the shorter side S — exactly the rule's bound, for every allowed `min_containment` above 0; S's rarest
      |S| − need + 1 stems are then sure to hold one of them (a prefix filter);
    - numbers and names (3.7.0): the rule wants the numbers / @-handles of one side to be a subset of the other's,
      and the names too. When both sides have numbers, the side with fewer holds all of its own in the other, its
      rarest one included: the other is listed under that number, or the side itself is listed under its rarest
      number, which the other holds. Names alike. Reminders («оплата 2026-11-03 в 10:00», a hundred days of them)
      or one sentence said of many people have nothing rare in their stems: each was a candidate of every other,
      the checks grew with the square of the store and ran out of the night's budget; now each meets the facts
      with its own date or its own person.
    For each kind of other side (with numbers or not, with names or not) the smallest of the lists that may hold it
    is read, and a pair is shown to the rule only when it shares enough stems and its numbers and names are
    subsets one way. At `min_containment` 0 the rule needs no shared stem, so every pair of long facts is checked,
    in the same budget. Short facts and facts without significant words have their own path: the same numbers /
    @-handles, or the same text."""
    shared = {}
    feats = {f["fact_id"]: _dup_features(f["content"], shared) for f in facts}
    del shared
    ids = sorted(feats, key=lambda i: (str(type(i)), i))
    rank = {i: n for n, i in enumerate(ids)}
    checked = set()
    found = []

    def check(i, j):
        key = (i, j) if rank[i] < rank[j] else (j, i)
        if key in checked:
            return
        if max_checks is not None and len(checked) >= max_checks:
            raise DupBudget(len(checked))
        checked.add(key)
        ok, why, cont = _dup_judge(feats[key[0]], feats[key[1]])
        if ok:
            found.append({"a": key[0], "b": key[1], "why": why, "containment": round(cont, 4)})

    long_ids = [i for i in ids if len(feats[i].words) >= DUP_SHORT_WORDS]
    floor = SETTINGS.DUP_MIN_CONTAINMENT
    if floor <= 0:
        # Containment 0 passes the rule's first test with no stem shared at all: there is nothing to index,
        # every pair of long facts is checked (within the budget).
        for n, i in enumerate(long_ids):
            for j in long_ids[n + 1:]:
                check(i, j)
    else:
        df = Counter(x for i in long_ids for f in (feats[i],) for x in f.words | f.anchors | f.names)
        kind = {i: (bool(feats[i].anchors), bool(feats[i].names)) for i in long_ids}
        stems = defaultdict(lambda: defaultdict(list))  # kind of fact -> stem -> facts
        # with names / without -> a number -> the facts holding it, and the facts whose rarest number it is
        by_number, by_rarest_number = defaultdict(lambda: defaultdict(list)), defaultdict(lambda: defaultdict(list))
        by_name, by_rarest_name = defaultdict(lambda: defaultdict(list)), defaultdict(lambda: defaultdict(list))
        for i in long_ids:
            f, (has_numbers, has_names) = feats[i], kind[i]
            for s in f.words:
                stems[kind[i]][s].append(i)
            for x in f.anchors:
                by_number[has_names][x].append(i)
            for x in f.names:
                by_name[has_numbers][x].append(i)
            if has_numbers:
                by_rarest_number[has_names][_rarest(f.anchors, df)].append(i)
            if has_names:
                by_rarest_name[has_numbers][_rarest(f.names, df)].append(i)
        for i in long_ids:
            f = feats[i]
            words, size = f.words, len(f.words)
            # As few shared stems as the rule accepts: one of four at 0.25 is a pair for it.
            need = max(1, math.ceil(floor * size - 1e-9))
            prefix = sorted(words, key=lambda s: (df[s], s))[:size - need + 1]
            for other_kind in ((False, False), (False, True), (True, False), (True, True)):
                sources = [[stems[other_kind].get(s, ()) for s in prefix]]
                if f.anchors and other_kind[0]:
                    sources.append([by_number[other_kind[1]].get(_rarest(f.anchors, df), ())]
                                   + [by_rarest_number[other_kind[1]].get(x, ()) for x in f.anchors])
                if f.names and other_kind[1]:
                    sources.append([by_name[other_kind[0]].get(_rarest(f.names, df), ())]
                                   + [by_rarest_name[other_kind[0]].get(x, ()) for x in f.names])
                near = set()
                for lists in min(sources, key=lambda ls: sum(map(len, ls))):
                    near.update(lists)
                near.discard(i)
                for j in near:  # any order: past the budget the whole search is dropped, and `found` is sorted
                    g = feats[j]
                    if (len(g.words) >= size and len(words & g.words) >= need
                            and (f.anchors <= g.anchors or g.anchors <= f.anchors)
                            and (f.names <= g.names or g.names <= f.names)):
                        check(i, j)
    # Short facts and facts without significant words: by their numbers / @-handles, and by the same text.
    by_anchors, by_text = defaultdict(list), defaultdict(list)
    for i in ids:
        if feats[i].anchors:
            by_anchors[feats[i].anchors].append(i)
        by_text[feats[i].norm].append(i)
    for group in by_anchors.values():
        short = [i for i in group if 0 < len(feats[i].words) < DUP_SHORT_WORDS]
        if not short:
            continue
        # A short pair needs every word of its smaller side in the other: the other holds the smaller side's rarest
        # word. A fact without significant words pairs with no other but its own text (the rule's containment is 0).
        holding = defaultdict(list)
        for j in group:
            for s in feats[j].words:
                holding[s].append(j)
        for i in short:
            words = feats[i].words
            for j in holding[min(words, key=lambda s: (len(holding[s]), s))]:
                if j != i and len(feats[j].words) >= len(words):
                    check(i, j)
    for group in by_text.values():
        for n, i in enumerate(group):
            for j in group[n + 1:]:
                check(i, j)
    found.sort(key=lambda p: (rank[p["a"]], rank[p["b"]]))
    return found, len(checked)


def feedback_floor(fact_source_name=None):
    """The trust below which the store stops recalling a fact: `gates.feedback_trust_floor` — the caller sets it to
    the store's own `min_trust_threshold` (0.3 by default)."""
    return SETTINGS.FEEDBACK_TRUST_FLOOR


def trust_feedback_items(suppressed, rejected, fact_source_name, floor=None):
    """Rejected facts that the store still treats as trustworthy.

    `dream-reject.py` closes a question for the DREAM; the store knows nothing
    about it, so with the holographic provider the same fact keeps arriving in
    ordinary turns through prefetch (it only drops out below the store's own
    floor — `feedback_floor()`, the provider's `min_trust_threshold`). The one
    call that teaches retrieval is `fact_feedback(action="unhelpful")` — the
    agent has it in the nightly session; the pass itself never writes the store.
    Only for the holographic source: nobody else has that tool."""
    if fact_source_name != "holographic":
        return []
    floor = feedback_floor(fact_source_name) if floor is None else floor
    out = []
    for s in suppressed:
        trust = s.get("trust_score")
        trust = 0.5 if trust is None else float(trust)
        if trust < floor:
            continue                      # уже ниже порога — prefetch его не поднимет
        reason, when = "", ""
        fingerprint = _fact_fingerprint(s["content"])
        rec = rejected.get(fingerprint)
        if isinstance(rec, dict):
            reason = str(rec.get("reason") or "")
            when = str(rec.get("at") or "")[:10]
        # Rejected facts leave before quarantine, so a secret the human closed with
        # dream-reject.py came back here in clear text (Codex review of 2.7.2): the call
        # needs only the id.
        unsafe = s.get("unsafe") or classify_unsafe(s["content"])
        if unsafe or classify_unsafe(reason):
            reason = ""
        out.append({
            "fact_id": s["fact_id"],
            "content": f"[hidden: {unsafe}]" if unsafe else s["content"][:200],
            "trust": round(trust, 2),
            # The store's own floor, in the item itself: the precheck hands the agent the items,
            # not `gates`, so a floor kept only there never reached the nightly prompt.
            "floor": round(floor, 2),
            "fingerprint": fingerprint,
            "rejected_at": when,
            "rejected_reason": reason[:160],
            "why": (f"rejected by a human{' on ' + when if when else ''}, but the store still "
                    f"trusts it at {round(trust, 2)} — prefetch keeps offering it"),
            "call": {"tool": "fact_feedback", "action": "unhelpful", "fact_id": s["fact_id"]},
        })
    out.sort(key=lambda i: -i["trust"])
    return out


# Confirmations (holographic >= 0.15.0): facts people keep saying, on CONFIRM_MIN_DAYS different days at least, by
# messages that do not negate them — offered to `fact_feedback(action="confirmed")`, at most CONFIRM_CAP a night. One
# mention is a topic, not an agreement; "Илья не переехал" mentions the fact it denies, so a message whose polarity
# differs from the fact's never counts. A fact the store already counts a confirmation of within CONFIRM_REST_DAYS
# rests.
CONFIRM_MIN_DAYS = 2
CONFIRM_CAP = 10
CONFIRM_REST_DAYS = 30
# Said again, not merely talked about: the message holds at least this share of the fact's own word stems. The
# corroboration rule (two shared stems, or one rare one) is right for "is this fact alive?" and too loose here — a
# family chat names a child and her school every day.
CONFIRM_MIN_COVERAGE = 0.6


def recent_confirmations(db, rest_days=CONFIRM_REST_DAYS, now=None):
    """Fact ids the store counts a confirmation of within *rest_days* before *now* (the night's time; without it,
    the database clock); None when the store has no `fact_confirmations` table (holographic older than 0.15.0: no
    `confirmed` call to offer). Read-only."""
    when = "now" if now is None else now.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    if not db or not os.path.exists(db):
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'fact_confirmations'"
                                ).fetchone():
                return None
            return {r[0] for r in conn.execute(
                "SELECT DISTINCT fact_id FROM fact_confirmations WHERE julianday(?) - julianday(confirmed_at) < ?",
                (when, rest_days))}
        finally:
            conn.close()
    except sqlite3.Error:
        return None


_NOT_READ = object()  # "the caller did not read the confirmations" (not the YAML scanner's _UNREAD)


@_message_pass
def confirm_items(safe, messages, by_store, fact_source_name, db=None, rules=None, now=None, veto=None,
                  rested=_NOT_READ, counted=None):
    """Facts of the main store that people said again on different days (see CONFIRM_MIN_DAYS). Only for the
    holographic source with the confirmations table; chat-store facts get no call (a cron session cannot reach a
    chat's database). The pass itself never writes the store. *rested*: the store's recent confirmations when
    the caller read them already (None: no confirmations table); *veto* / *counted* as in corroborate."""
    if fact_source_name != "holographic":
        return []
    if rested is _NOT_READ:
        rested = recent_confirmations(db or fact_store_path(), now=now)
    if rested is None:
        return []
    out = []
    for s in safe:
        if s.get("store") or s.get("unsafe") or s["fact_id"] in rested \
                or (s.get("meta") or {}).get("ref_days", 0) < CONFIRM_MIN_DAYS:
            continue
        tokens = fact_tokens(s["content"])
        stems = _stems(tokens)
        wide = _is_wide(tokens)
        if not stems:
            continue
        days = set()
        for m in messages_for(s, messages, by_store):
            if (corroborate(tokens, [m], rules=rules, veto=veto, fact_id=s["fact_id"])[0]
                    and len(stems & (_message_data(m).wide_stems() if wide else _message_data(m).stems()))
                    / len(stems) >= CONFIRM_MIN_COVERAGE
                    and not _message_contradicts(s["content"], _message_data(m))):
                days.add(m["day"])
                if counted is not None:
                    counted.add(PairId(s["fact_id"], message_key(m)))
        if len(days) >= CONFIRM_MIN_DAYS:
            out.append({"fact_id": s["fact_id"], "content": s["content"][:200], "days": len(days),
                        **({"origin": s["origin"]} if s.get("origin") else {}),
                        "fingerprint": _fact_fingerprint(s["content"]),
                        "call": {"tool": "fact_feedback", "action": "confirmed", "fact_id": s["fact_id"]}})
    out.sort(key=lambda i: (-i["days"], i["fact_id"]))
    return out


def keywords_needed_items(safe, fact_source_name):
    """Facts the store keeps without keywords, the most recalled first.

    Only for the holographic source with `keywords.enabled`: the call writes
    the provider's own column, and a provider older than 0.6.0 accepts the
    parameter and drops it. The pass itself never writes the store."""
    if not SETTINGS.KEYWORDS_ENABLED or fact_source_name != "holographic":
        return []
    out = []
    for s in safe:
        if s.get("keywords"):
            continue
        out.append({
            "fact_id": s["fact_id"],
            "content": s["content"][:300],
            # The cooldown and the forget sweep key on the full text; a key of the cut text never matched a
            # fact longer than 300 characters (review of 2.7.4).
            "fingerprint": _fact_fingerprint(s["content"]),
            "retrieval_count": s["meta"]["rc"],
            "call": {"tool": "fact_store", "action": "update", "fact_id": s["fact_id"],
                     "keywords": ["3-8 words a person might ask with that the fact lacks"]},
        })
    out.sort(key=lambda i: (-i["retrieval_count"], str(i["fact_id"])))
    return out


def load_rejected(path):
    """Read the reject list. Missing — run without it; broken — moved aside with a warning (`_load_state_file`)."""
    return _load_state_file(path)


def is_rejected(content, rejected):
    """Match by text fingerprint or by rewording of the same fact (both
    directions — a short human rejection must be able to close a long
    re-extracted fact, with the same strict reverse conditions as dedupe)."""
    return rejection_of(content, rejected) is not None


def rejection_of(content, rejected):
    """The latest reject record that matches `content` (see is_rejected), or None."""
    if not rejected:
        return None
    fingerprint = _fact_fingerprint(content)
    item_stems = _stems(sig_tokens(content))
    found = []
    for rec in rejected.values():
        if not isinstance(rec, dict):
            continue
        if rec.get("fingerprint") == fingerprint:
            found.append(rec)
            continue
        text = rec.get("content") or ""
        if not text or len(item_stems) < 4:
            continue
        if _covers(item_stems, _stems(sig_tokens(text))) and not polarity_differs(content, text):
            found.append(rec)
    epoch = datetime.min.replace(tzinfo=timezone.utc)  # an unreadable date loses to any readable one
    return max(found, key=lambda r: _parse_iso(str(r.get("at") or "")) or epoch) if found else None


def said_again(fact, meta, rec, now=None):
    """True when a rejection is old enough to ask about once more and the fact was said again after it: its store
    row was created after the rejection AND a human mentioned it after it. A rejection is not forever — "no longer
    does ballet" was right in May and wrong once she went back in September (mem0#7612 is the same trap with
    expiry). But "said again" alone cannot tell that from the core re-extracting the same stale fact while people
    still talk about it — the very case the list was made for (twelve identical questions in five weeks) — so only a
    rejection older than `reject_reask_days` (180; 0 = never) is asked about again, and a second "no" starts the
    count over. The same old row stays silenced."""
    if not SETTINGS.REJECT_REASK_DAYS:
        return False
    at = _parse_iso(str(rec.get("at") or ""))
    created = _parse_ts(fact.get("created_at"))
    if at is None or created is None or (now or _now()) - at < timedelta(days=SETTINGS.REJECT_REASK_DAYS):
        return False
    return created > at and (meta.get("last_ts") or 0) > at.timestamp()


# ---------------------------------------------------------------------------
# "Already shown": cooldown for new_facts / fact_decays / conflicts
# ---------------------------------------------------------------------------
# The reject list closes a question by a HUMAN decision, but for these
# sections the usual outcome is the agent's own "looked, nothing to do". That
# had nowhere to be stored, so the section re-opened the wake gate every night
# — new_facts up to 14 nights per fact, fact_decays indefinitely. Same trick as
# md_decays: not "silence forever" but "not more often than once per
# SEEN_COOLDOWN_DAYS". Key — text fingerprint (a reworded / re-extracted fact
# is new work). Only candidates that were actually published (after the caps)
# are marked — an overflow candidate never reached the agent.

def _parse_iso(s):
    try:
        dt = datetime.fromisoformat(str(s))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def unseen(items, bucket, now, cooldown_days=None, key=None):
    """Candidates not shown for longer than the cooldown. Does not mutate state."""
    cooldown = timedelta(days=SETTINGS.SEEN_COOLDOWN_DAYS if cooldown_days is None else cooldown_days)
    key = key or (lambda item: _fact_fingerprint(item["content"]))
    fresh = []
    for item in items:
        last = _parse_iso((bucket.get(key(item)) or {}).get("at"))
        if last and (now - last) < cooldown:
            continue
        fresh.append(item)
    return fresh


def _feedback_key(item):
    return item.get("fingerprint") or _fact_fingerprint(item["content"])


def mark_seen(items, bucket, now, key=None):
    """Mark as shown — call only for actually published items."""
    stamp = now.isoformat(timespec="seconds")
    key = key or (lambda item: _fact_fingerprint(item["content"]))
    for item in items:
        bucket[key(item)] = {"at": stamp, "fact_id": item.get("fact_id")}


def prune_seen(bucket, now, cooldown_days=None):
    """Drop entries older than two cooldowns so the state does not grow forever."""
    horizon = timedelta(days=2 * (SETTINGS.SEEN_COOLDOWN_DAYS if cooldown_days is None else cooldown_days))
    for key in [k for k, v in bucket.items()
                if not isinstance(v, dict) or not _parse_iso(v.get("at"))
                or (now - _parse_iso(v.get("at"))) >= horizon]:
        bucket.pop(key, None)


def load_seen(path):
    return {k: v for k, v in _load_state_file(path).items() if isinstance(v, dict)}


# ---------------------------------------------------------------------------
# Ephemeral (dated) events and expiry
# ---------------------------------------------------------------------------

# from dream_lang.py (3.0)
(MONTHS_RE, THAI_MONTHS_RE, EPHEMERAL_HINTS_RE) = (
    _LANG.MONTHS_RE, _LANG.THAI_MONTHS_RE, _LANG.EPHEMERAL_HINTS_RE)
# 14.11, 14/11, 14-11-2026, Korean 12. 31.; the digits may touch Chinese, Japanese or Thai (奶奶11/23上午).
NUMERIC_DATE_RE = re.compile(
    rf"(?<!{_NUM_EDGE}[\w.])(?<!-)(\d{{1,2}})(?:[./-]|\.\s)(\d{{1,2}})(?:[./-](\d{{2,4}}))?(?!{_NUM_EDGE}\w)(?!-\d)")
DATED_RE = re.compile(
    rf"\b\d{{1,2}}\s+(?:{MONTHS_RE})(?:\s+\d{{4}})?\b|"
    r"\b\d{4}-\d{2}-\d{2}\b|"
    rf"{NUMERIC_DATE_RE.pattern}|"
    # The English dates the expiry check reads (`EN_DATE_RE`): a list of its own here missed "Sept 5" and "Dec. 5",
    # so such an event was never held back from promotion (review of 3.3.1).
    rf"{_LANG.EN_DATE_RE.pattern}|"
    rf"\b\d{{1,2}}\s+(?:{THAI_MONTHS_RE})|"
    rf"{DAY_MONTH_RE.pattern}|{CJK_DATE_RE.pattern}|{KO_DATE_RE.pattern}",
    re.IGNORECASE,
)
RELATIVE_DATE_RE = re.compile(
    r"\b(?:сегодня|завтра|послезавтра|на этой неделе|на следующей неделе|"
    r"today|tomorrow|this week|next week|"
    r"heute|morgen|übermorgen|nächste\s+woche|hoy|mañana|pasado\s+mañana|la\s+semana\s+que\s+viene|"
    r"aujourd'hui|demain|après-demain|la\s+semaine\s+prochaine|hoje|amanhã|depois\s+de\s+amanhã|"
    r"oggi|domani|dopodomani|la\s+settimana\s+prossima|vandaag|overmorgen|volgende\s+week|"
    r"bugün|yarın|öbür\s+gün|gelecek\s+hafta|dzisiaj|dziś|jutro|pojutrze|w\s+przyszłym\s+tygodniu|"
    r"hari\s+ini|besok|lusa|minggu\s+depan)\b"
    r"|(?:วันนี้|พรุ่งนี้|มะรืน|今天|明天|后天|後天|今日|明日|明後日|来週|下周|下週|오늘|내일|모레|다음\s*주)",
    re.IGNORECASE,
)


# A whole date: a day and a month name («1 декабря», "30. Nov", «4 grudnia»), or a day and a month in digits
# (30/11, 30.11, 14-11-2026 — a real month, so a time «15.00» is not a date; review of 2.5.1).
_DATE_AHEAD = (rf"(?<!\d)\d{{1,2}}(?:\.|st|nd|rd|th|er)?\s+(?:de\s+|of\s+)?(?:{OTHER_MONTHS_RE}|{MONTHS_RE})\b"
               r"|(?<![\d-])(?:[12]\d|3[01]|0?[1-9])[./-](?:1[0-2]|0?[1-9])(?!\d)")
# A deadline: "until / by / before" followed by a whole date («до 1 декабря», «bis 30. Nov», «do 4 grudnia»), or a
# Turkish date with «kadar» after it («30/11'e kadar»). A bare number is not a date: "can do 20 pushups" is not a
# Polish «do 20.» (review 2026-10-13), and the old open-ended `\S*` before «kadar» was quadratic on long numbers.
DEADLINE_RE = re.compile(
    r"\b(?:до|until|by|before|till|bis|bis\s+zum|antes\s+del?|hasta\s+el|avant\s+le|até|entro\s+il|entro|tot|"
    rf"uiterlijk|do|sebelum)\s+(?:the\s+|dem\s+|el\s+|le\s+|il\s+)?(?:{_DATE_AHEAD})"
    rf"|(?:{_DATE_AHEAD})(?:\s+\d{{4}})?'?\w{{0,4}}\s+kadar\b",
    re.IGNORECASE)
# "Since <date>" opens a state that lasts — a job, a move, a rule («Илья врач с 14 ноября 2020 года», "Tom can
# do 20 pushups since 14 November 2020"). Only that phrase is taken out before the event check, not the whole
# fact: "Call on 14 November; Tom has worked here since 2020" is still a call (review of 2.5.1).
SINCE_DATE_RE = re.compile(
    r"\b(?:since|seit|desde|depuis|dal|dalla|sinds|vanaf|od|sejak|с|со)\s+(?:the\s+|dem\s+|el\s+|le\s+|il\s+)?"
    rf"(?:{_DATE_AHEAD})(?:\s+\d{{4}})?|(?:{_DATE_AHEAD})(?:\s+\d{{4}})?'?\w{{0,4}}\s+(?:beri|itibaren|bu\s+yana)\b",
    re.IGNORECASE)
# …unless the date opens a range: «с 15.11 по 17.11», «od 14 listopada do 16 listopada», "from … to …".
RANGE_END_RE = re.compile(
    r"\s*(?:г\.|года?)?\s*(?:,\s*)?(?:по|до|to|until|till|through|bis|al|au|a|do|tot|t/m|hingga|sampai|[-–—])\s*\d",
    re.IGNORECASE)


def _without_lasting(text):
    """The text with every "since <date>" phrase cut out (a range «с … по …» stays: it is an event's span)."""
    out, last = [], 0
    for m in SINCE_DATE_RE.finditer(text):
        if RANGE_END_RE.match(text, m.end()):
            continue
        out.append(text[last:m.start()])
        last = m.end()
    out.append(text[last:])
    return " ".join(out)


def is_ephemeral_fact(content):
    """Temporary dated school/calendar facts should not become durable memory."""
    text = _without_lasting(content or "")
    has_time_anchor = bool(DATED_RE.search(text) or RELATIVE_DATE_RE.search(text))
    return bool(has_time_anchor and (EPHEMERAL_HINTS_RE.search(text) or DEADLINE_RE.search(text)))


# A dated event whose date has passed is garbage, not context: it must not be
# promoted, and strong corroboration keeps it from decaying by itself. Live
# case: "test on July 3" stayed in the output until the end of July and
# opened the wake gate every night although no work was possible.
# from dream_lang.py (3.0)
(RU_MONTHS, EN_MONTHS, THAI_MONTHS, ISO_DATE_RE, RU_DATE_RE, EN_DATE_RE, THAI_DATE_RE) = (
    _LANG.RU_MONTHS, _LANG.EN_MONTHS, _LANG.THAI_MONTHS, _LANG.ISO_DATE_RE, _LANG.RU_DATE_RE, _LANG.EN_DATE_RE,
    _LANG.THAI_DATE_RE)


def _resolve_year(year, month, day, today):
    """No year given → the one that puts the date closest to today (±180 d).
    A year ≥ 2400 is the Thai Buddhist calendar."""
    if year:
        year = int(year)
        if year >= 2400:
            year -= 543
        return year
    for candidate in (today.year, today.year - 1, today.year + 1):
        try:
            probe = datetime(candidate, month, day).date()
        except ValueError:
            continue
        if -180 <= (probe - today).days <= 180:
            return candidate
    return today.year


def _safe_date(year, month, day, today):
    try:
        return datetime(_resolve_year(year, month, day, today), month, day).date()
    except (ValueError, TypeError):
        return None


def extract_event_dates(text, today):
    """(certain dates, ambiguous pairs). Both empty — nothing parsed."""
    text = text or ""
    certain, ambiguous = [], []
    for y, m, d in ISO_DATE_RE.findall(text):
        got = _safe_date(y, int(m), int(d), today)
        if got:
            certain.append(got)
    for day, month_word, year in RU_DATE_RE.findall(text):
        month = next((n for stem, n in RU_MONTHS.items()
                      if month_word.lower().startswith(stem)), None)
        got = _safe_date(year, month, int(day), today) if month else None
        if got:
            certain.append(got)
    for month_word, day, year in EN_DATE_RE.findall(text):
        month = EN_MONTHS.get(month_word.lower()[:3])
        got = _safe_date(year, month, int(day), today) if month else None
        if got:
            certain.append(got)
    for day, month_word, year in DAY_MONTH_RE.findall(text):
        month = other_month(month_word)
        got = _safe_date(year, month, int(day), today) if month else None
        if got:
            certain.append(got)
    for regex in (CJK_DATE_RE, KO_DATE_RE):
        for year, month, day in regex.findall(text):
            got = _safe_date(year, int(month), int(day), today)
            if got:
                certain.append(got)
    for day, month_word, year in THAI_DATE_RE.findall(text):
        month = THAI_MONTHS.get(month_word)
        got = _safe_date(year, month, int(day), today) if month else None
        if got:
            certain.append(got)
    # 5/10 is both Oct 5 and May 10: keep both readings, decide by the worse one.
    for a, b, year in NUMERIC_DATE_RE.findall(text):
        pair = [d for d in (_safe_date(year, int(b), int(a), today),
                            _safe_date(year, int(a), int(b), today)) if d]
        if pair:
            ambiguous.append(pair)
    return certain, ambiguous


def is_expired_event(content, today=None):
    """True — a temporary event whose date has passed. Conservative: a relative
    anchor ("tomorrow") is not resolved; an ambiguous numeric date counts as
    past only if it is past under EVERY reading."""
    text = content or ""
    if not is_ephemeral_fact(text):
        return False
    text = _without_lasting(text)  # the date a state began is not the event's date
    if RELATIVE_DATE_RE.search(text):
        return False
    today = today or _now().astimezone(SETTINGS.LOCAL_TZ).date()
    certain, ambiguous = extract_event_dates(text, today)
    if not certain and not ambiguous:
        return False
    return (all(d < today for d in certain)
            and all(max(pair) < today for pair in ambiguous))


# ---------------------------------------------------------------------------
# Quarantine of unsafe content
# ---------------------------------------------------------------------------
# Facts are extracted by the core from live conversations (including observed
# chats), so they may contain secrets or embedded instructions (indirect
# prompt injection). Such content must reach neither promotions, nor
# new_facts, nor the agent's prompt.
SECRET_RE = re.compile(
    r"(?:"
    r"sk-[A-Za-z0-9_-]{16,}"                        # OpenAI/Anthropic-style keys
    r"|apikey_[A-Za-z0-9]{8,}"                      # Anthropic console key ids
    r"|AKIA[0-9A-Z]{16}"                            # AWS access key
    r"|AIza[0-9A-Za-z_\-]{30,}"                     # Google API key
    r"|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[abprs]-[A-Za-z0-9\-]{10,}"               # Slack
    r"|gsk_[A-Za-z0-9]{20,}"                        # Groq
    r"|ntn_[A-Za-z0-9]{20,}|secret_[A-Za-z0-9]{32,}"  # Notion (new and legacy)
    r"|sk_[a-f0-9]{32,}"                            # ElevenLabs and friends
    r"|gh[osu]_[A-Za-z0-9]{20,}"                    # GitHub OAuth/server/user tokens
    r"|\b\d{8,10}:AA[A-Za-z0-9_\-]{30,}"            # Telegram bot token
    r"|eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}"  # JWT
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\b[0-9a-f]{40,}\b"                           # long hex (keys/sessions)
    r")")
# A credential marker is rarely glued to the colon: "Пароль от wifi в квартире:
# X" and "Password for the router is X" both carry one. Up to four words are
# allowed between the marker and the separator — but they must not contain a
# verb of change, so "сменила пароль от Wi-Fi" (an event, not a secret) stays
# out (external review, 2026-10-07).
PASSWORD_MARKER_RE = re.compile(
    r"(?:парол[ьяию]|password|passwd|passphrase|seed[- ]?фраза|seed[- ]?phrase|"
    r"секретн\w{0,4}\s+(?:ключ|код)|api[- ]?ключ|private\s+key|2fa[- ]?код|"
    r"пин[- ]?код|\bпин\b|\bpin\b|код\s+от|密码|密碼|口令|密钥|密鑰|私钥|私鑰)"
    r"(?:[^\n:=—–：]{0,40}?)(?P<sep>[:=—–：]|\s-\s)\s*(?P<value>\S{4,})",
    re.IGNORECASE)
# …and these say "we changed it", not "here it is". A colon or "=" hands the
# value over whatever comes first ("Changed password: hunter2", "поменяла
# пароль: розовыйслон"). After a dash a verb of change in the marker's own
# sentence makes it an event — "сменила пароль от Wi-Fi — новый знает только
# она", "сбросил пароль от Zoom — 40-минутный лимит остался" — unless the value
# looks like a credential: six or more characters and a digit or a symbol
# ("— Secret123!"); with Cyrillic in it, a digit glued to a letter
# ("— Кот2024!"), because "40-минутный" and "кв.123" are words, not
# passwords. Codex review of 2.7.2, review of 2.7.4, Codex review of 2.7.5.
PASSWORD_EVENT_RE = re.compile(
    r"(?:смен\w+|помен\w+|обнов\w+|сброс\w+|забыл\w*|changed|rotated|reset|forgot)",
    re.IGNORECASE)
_SENTENCE_END_RE = re.compile(r"[.!?\n]")
_CREDENTIAL_VALUE_RE = re.compile(r"[\d!@#$%^&*_+=~|\\/<>{}\[\]]")
_CYRILLIC_RE = re.compile(r"[а-яё]", re.IGNORECASE)
_GLUED_DIGIT_RE = re.compile(r"[^\W\d_]\d|\d[^\W\d_]")


def _looks_like_credential(value):
    value = value.rstrip(".,;")
    if len(value) < 6 or not _CREDENTIAL_VALUE_RE.search(value):
        return False
    return not _CYRILLIC_RE.search(value) or bool(_GLUED_DIGIT_RE.search(value))


_ZH_PASSWORD_IS_RE = re.compile(r"(?:密码|密碼|口令|密钥|密鑰|私钥|私鑰)\s*(?:是|为|為)\s*(?P<value>[^\s，。,;；]{4,})")
_ZH_VALUE_RE = re.compile(r"[0-9A-Za-z!@#$%^&*_+=~|\\/<>{}\[\]]")


def _password_marker_unsafe(text):
    if any(_ZH_VALUE_RE.search(m.group("value")) for m in _ZH_PASSWORD_IS_RE.finditer(text)):
        return True
    for marker in _found(PASSWORD_MARKER_RE, _PASSWORD_MARKER_START, text):
        if marker.group("sep").strip() in (":", "=", "："):
            return True
        ends = [m.end() for m in _SENTENCE_END_RE.finditer(text, 0, marker.start())]
        if not PASSWORD_EVENT_RE.search(text[(ends[-1] if ends else 0):marker.end()]):
            return True
        if _looks_like_credential(marker.group("value")):
            return True
    return False
INJECTION_RE = re.compile(
    r"(?:"
    # Stems, not exact forms: «Игнорируйте», «игнорировать», «забудьте» are the
    # same instruction (external review, 2026-10-07). Up to two words may stand
    # between the verb and its object.
    r"игнорир\w*\s+(?:\w+\s+){0,2}?(?:инструкци\w*|правил\w*|указани\w*|предыдущ\w*|прошл\w*)"
    r"|ignore\s+(?:\w+\s+){0,3}?(?:previous|prior|above|earlier|all)\s+(?:\w+\s+){0,2}?instructions?"
    r"|disregard\s+(?:all\s+|any\s+|your\s+)?(?:previous|prior|above|instructions|rules|guidelines)"
    r"|do\s+not\s+tell\s+the\s+user|system\s+prompt\s+override"
    r"|forget\s+(?:all\s+|your\s+|the\s+)?(?:previous\s+|prior\s+)?(?:instructions?|rules?)"
    r"|забуд\w*\s+(?:\w+\s+){0,2}?(?:инструкци\w*|правил\w*|ограничени\w*)"
    r"|запомни\s*[:,]?\s*(?:всегда|никогда|что\s+тебе\s+разреш)"
    r"|системн\w+\s+промпт|system\s+prompt"
    r"|(?:developer|god|dan)\s+mode|режим\s+разработчика"
    r"|new\s+instructions?\s*:|новые\s+инструкции\s*:"
    r"|(?:покажи|раскрой|выведи|отправь|перешли|скинь)[^.\n]{0,40}"
    r"(?:промпт|инструкци|ключ[иа]?\b|парол|токен|секрет)"
    r"|(?:запиши|сохрани|добавь)\s+в\s+память[^.\n]{0,60}"
    r"(?:игнориру|разреша|всегда\s+выполня|парол|ключ)"
    r"|(?:save|store|add|write)\s+(?:to|in|into)\s+memory[^.\n]{0,60}"
    r"(?:ignore|allow|always|password|key|secret)"
    r"|jailbreak"
    r")",
    re.IGNORECASE)


# Long texts (3.7.2). PASSWORD_MARKER_RE and INJECTION_RE are case-blind alternations the regex engine cannot skip
# through, so it tried each alternative at every position: on a megabyte of Russian prose half a second, next to the
# 16 s of the provider's screen, which holographic 0.22.1 cut the same way. Each is now tried only where one of its
# words can begin: those places are found by one case-sensitive pattern of the words' starts in a lower-case copy of
# the text (_folded, as the provider's screen folds it), and the pattern itself decides there on the text as it is —
# the matches a scan of the whole text finds, in the same order. A space in a start stands for any run of whitespace.
# tests/test_screen_speed_372.py derives from each pattern what its matches can begin with and checks these lists.
_PASSWORD_MARKER_STARTS = ("парол", "pass", "seed", "секретн", "api", "private ", "2fa", "пин", "pin", "код от",
                           "密码", "密碼", "口令", "密钥", "密鑰", "私钥", "私鑰")
_INJECTION_STARTS = ("игнорир", "ignore ", "disregard ", "do not tell", "system prompt", "forget ", "забуд",
                     "запомни", "системн", "developer mode", "god mode", "dan ", "режим разработчика",
                     "new instruction", "новые инструкции", "покажи", "раскрой", "выведи", "отправь", "перешли",
                     "скинь", "запиши в", "сохрани в", "добавь в", "save to", "save in", "store to", "store in",
                     "add to", "add in", "write to", "write in", "jailbreak")
# A letter the engine takes for another one regardless of case, written as that one ("İ" is replaced before lowering).
_FOLD_SAME = ("ıi", "ſs", "ςσ", "ᲀв", "ᲁд", "ᲂо", "ᲃс", "ᲄт", "ᲅт", "ᲆъ", "ᲇѣ")


def _folded(text):
    """*text* in lower case, one character for one, the letters of _FOLD_SAME written as one; None when lower case is
    not one character for one (the patterns then read the whole text)."""
    folded = (text.replace("İ", "i") if "İ" in text else text).lower()
    if len(folded) != len(text):
        return None
    if not folded.isascii():
        for same, letter in _FOLD_SAME:
            if same in folded:
                folded = folded.replace(same, letter)
    return folded


def _starts_pattern(starts):
    return re.compile("|".join("".join(r"\s+" if c == " " else re.escape(c) for c in s)
                               for s in sorted(starts, key=len, reverse=True)))


_PASSWORD_MARKER_START = _starts_pattern(_PASSWORD_MARKER_STARTS)
_INJECTION_START = _starts_pattern(_INJECTION_STARTS)


def _found(rx, start_rx, text):
    """What rx.finditer(text) yields, *rx* tried only where *start_rx* finds a start in the folded text, overlapping
    starts too, and not inside the match before (neither pattern matches an empty string)."""
    folded = _folded(text)
    if folded is None:
        yield from rx.finditer(text)
        return
    end, m = 0, start_rx.search(folded)
    while m:
        if m.start() >= end:
            found = rx.match(text, m.start())
            if found:
                yield found
                end = found.end()
        m = start_rx.search(folded, m.start() + 1)


# The holographic provider's own screen (scripts/holo_screen.py, a verbatim copy made by tools/vendor_screen.py):
# credentials in its 18 languages, token shapes (a bot token inside a Bot API address, A[KS]IA…, an address with a
# password), environment assignments, encoded blobs, instructions and requests to send secrets in every language,
# chat-role markup, hidden characters, images. The provider refuses these when storing and recalling, but never
# cleans what it already holds, nor people's messages — and the dream carries both into durable memory and its
# report (2.9.0). Two things it counts the dream does not:
# - a change of a password told after a dash, where what follows is a word of the sentence, not a value ("сбросил
#   пароль от Zoom — 40-минутный лимит остался", "забыл пароль от домофона — кв.123 не открывает"): the dream's
#   rule since 2.7.5, narrowed in 2.9.0 to a word in a non-Latin script with the sentence going on after it — a
#   short or Latin value ("— abc1", "— 1234", "— step1 …") is taken for a password (Codex review of 2.9.0);
# - a zero-width joiner inside an emoji (👩‍💻), the tags of a subdivision flag (🏴 + tags) and the zero-width space
#   that separates words in Thai, Lao, Khmer and Burmese: how these are written, not hidden text.
# A secret wins over an instruction: an injection carries a preview into the full JSON (Codex review of 2.9.0).
# A door key is not a credential: "перешли Даше ключ от дачи", "покажи ключ от квартиры соседке" read as a request
# to send a secret to both screens ("перешли … ключ"). The word before «от» + something with a lock is replaced
# before the instruction checks, not the secret ones (2.9.1). Only things with a lock: «ключ от почты», «от личного
# кабинета», «от API», a bare «ключ» stay suspicious.
def _load_holo_screen(path=None):
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), "holo_screen.py")
    try:
        spec = importlib.util.spec_from_file_location("_dream_holo_screen", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        missing = [n for n in ("_recall_problem", "_secret_in", "_RE_CREDENTIALS", "_credential_matches")
                   if not hasattr(mod, n)]
        if missing:
            raise ImportError(f"no {', '.join(missing)}")
        return mod
    except Exception as exc:  # noqa: BLE001 — a broken copy must not take the pass down (review of 2.9.1)
        print(f"[dream] warn: {path} not loaded ({exc!r}) — secrets and instructions screened by the dream's own "
              f"rules only", file=sys.stderr)
        return None


HOLO_SCREEN = _load_holo_screen()
_EMOJI_JOINER_RE = re.compile(r"(?<=[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F])\u200D(?=[\U0001F000-\U0001FAFF\u2600-\u27BF])")
_FLAG_TAGS_RE = re.compile(r"(?<=\U0001F3F4)[\U000E0030-\U000E0039\U000E0061-\U000E007A]{2,6}\U000E007F")
_NO_SPACE_SCRIPTS = "\u0E00-\u0EFF\u1000-\u109F\u1780-\u17FF"  # Thai, Lao, Burmese, Khmer
_NO_SPACE_LETTER_RE = re.compile(f"[{_NO_SPACE_SCRIPTS}]")


def _strip_word_separators(text):
    """The zero-width space these scripts separate words with, also next to a digit, a space or a mark ("ลูกสาว
    7\\u200b ขวบ", "ไปตลาด\\u200b, ซื้อปลา"; review of 2.9.1): dropped when a letter of them stands within four
    characters. Elsewhere it stays hidden text."""
    if "\u200b" not in text:
        return text
    return "".join(c for i, c in enumerate(text)
                   if c != "\u200b" or not _NO_SPACE_LETTER_RE.search(text, max(0, i - 4), i + 5))


_LATIN_RE = re.compile(r"[A-Za-z]")
# Between «от» and the thing only words that describe it — "от нашей виллы", "от почтового ящика", "от Дашиной
# машины" — never another noun: "ключ от почты в офисе", "ключ от API на даче" name a digital key and a place
# (review of 2.9.1).
_DESCRIBING = r"(?:[а-яё-]*(?:ого|его|ой|ей|ых|их|ую|юю|ая|яя|ое|ее|ые|ие|ий|ый|ом|ем|им|ым|ина|иной|иного|ова|овой|ового)|его|её|ее|их)"
_DOOR_KEY_RE = re.compile(
    r"\bключ(?:и|а|у|ом|ей|ами|ах)?(?=\s+от\s+(?:" + _DESCRIBING + r"\s+){0,2}?"
    r"(?:дач|квартир|дом(?:а|у|е|ом)?\b|домофон|машин|авто(?:мобил)?|гараж|двер|подъезд|калитк|ворот|сара[яеюй]"
    r"|подвал|кладов|офис|комнат|шкаф|ящик|сейф|зам(?:ок|к)|велосипед|байк|мотоцикл|скутер|номер|вилл|кондо"
    r"|бассейн))", re.IGNORECASE)
_LETTER_RE = re.compile(r"[^\W\d_]")


def _told_change(text, match):
    """The dream's rule: a credential word, a dash, then a word of the sentence, not a value — in a non-Latin
    script, nothing of a credential's shape, the sentence going on after it — in a sentence about changing it."""
    if "sep" not in match.re.groupindex or match.group("sep").strip(" \t\"'«“„‚`「『") not in ("—", "–", "-"):
        return False
    value = match.group("value").rstrip(".,;:!?")
    if not _LETTER_RE.search(value) or _LATIN_RE.search(value) or _looks_like_credential(value):
        return False
    rest = _SENTENCE_END_RE.split(text[match.end("value"):], 1)[0]
    if not _LETTER_RE.search(rest):
        return False
    ends = [m.end() for m in _SENTENCE_END_RE.finditer(text, 0, match.start())]
    return bool(PASSWORD_EVENT_RE.search(text[(ends[-1] if ends else 0):match.end()]))


def _screened_text(text):
    """The text the provider's screen judges: emoji joiners, flag tags and word separators out, and the value of a
    told change (_told_change) blanked."""
    text = _strip_word_separators(_FLAG_TAGS_RE.sub("", _EMOJI_JOINER_RE.sub("", text)))
    told = sorted({m.span("value") for m in HOLO_SCREEN._credential_matches(text) if _told_change(text, m)},
                  reverse=True)
    for start, end in told:
        text = text[:start] + "…" + text[end:]
    return text


@functools.lru_cache(maxsize=8192)
def classify_unsafe(content):
    """'secret' | 'injection' | None. Conservative quarantine: exclusion from
    auto-promotion with a note in the report, never deletion. The dream's own
    rules and the holographic provider's screen (see _load_holo_screen); a
    secret by either wins over an instruction."""
    text = content or ""
    if SECRET_RE.search(text) or _password_marker_unsafe(text):
        return "secret"
    screened = _screened_text(text) if HOLO_SCREEN is not None else None
    if screened is not None and HOLO_SCREEN._secret_in(screened):
        return "secret"
    if next(_found(INJECTION_RE, _INJECTION_START, _DOOR_KEY_RE.sub("вещь", text)), None) is not None:
        return "injection"
    if screened is not None and HOLO_SCREEN._recall_problem(_DOOR_KEY_RE.sub("вещь", screened)):
        return "injection"  # not a credential: _secret_in covers those
    return None


# ---------------------------------------------------------------------------
# Candidate gates and helpers
# ---------------------------------------------------------------------------

def promotion_ready(item):
    """Require corroboration, not only a high internal provider score — and a person behind the fact. An agent's
    conclusion nobody confirmed (origin `inferred`) is not promoted however often it is recalled, marked helpful or
    its words come up in chat: people saying it again offers it to `confirm`, and a confirmed one reads as
    `confirmed` (holographic F1 research, 2026-10-29). Without a known origin (a provider before 0.15.0, a fact
    stored before it, another fact source) the corroboration rule alone decides, as before."""
    if item.get("origin") == "inferred":
        return False
    meta = item["meta"]
    helpful = item.get("helpful_count") or 0
    return (
        meta["ref_days"] >= 2
        or meta["mentions"] >= SETTINGS.MIN_MENTIONS
        or (meta["rc"] >= 3 and helpful >= 1)
    )


def user_profile_hint(fact):
    """Hint only: the agent still judges stability, usefulness and sensitivity."""
    category = _norm(fact.get("category"))
    if category in SETTINGS.PROFILE_CATEGORIES:
        return True
    return bool(SETTINGS.PROFILE_HINT_RE and SETTINGS.PROFILE_HINT_RE.search(_norm(fact.get("content"))))


def _theme_gram(gram):
    """An n-gram that can stand for a theme: a pair of Han characters with no function character (学校, 牙医) —
    usually a word. Kana, Hangul and Thai n-grams are pieces of words (アン, 나는, รีย) and are left out."""
    return all(_HAN_RE.match(c) for c in gram) and gram_weight(gram) == 1.0


def extract_themes(messages, window_days):
    cutoff = _now().timestamp() - window_days * 86400
    doc_freq = defaultdict(int)
    samples = {}
    for m in messages:
        if m["ts"] < cutoff:
            continue
        for w in m["tokens"]:
            if is_gram(w) and not _theme_gram(w):
                continue
            doc_freq[w] += 1
            samples.setdefault(w, m["content"][:120])
    themes = [{"theme": w, "mentions": n, "sample": samples[w]}
              for w, n in doc_freq.items() if n >= SETTINGS.MIN_MENTIONS]
    # Ties by the word: a message's tokens are a set, and their order followed PYTHONHASHSEED — two runs of one
    # night published different themes under the cap (found by the 3.3.0 golden record).
    themes.sort(key=lambda x: (-x["mentions"], x["theme"]))
    return themes


def parse_md_entries(memory):
    """The notes the night may ask about (entries of 10 characters or less are too short to judge)."""
    return [e for e in memory.notes if len(e) > 10]


def _entry_key(entry):
    """Stable key of a §-entry for the cooldown (normalized head of the text)."""
    return hashlib.md5(_norm(entry)[:200].encode("utf-8")).hexdigest()[:16]


def is_pinned(entry):
    """A human-pinned entry (marker from `pinned_markers`) is never asked about."""
    return any(m in (entry or "") for m in SETTINGS.PINNED_MARKERS)


# `old_text` of the core `memory replace` is matched as a SUBSTRING of an
# existing entry, so a verbatim head of the entry is always a valid anchor —
# while a paraphrase or a fact's own text never is. Live failure 2026-08-16:
# the agent was told to "prefer replace" and passed fact texts as `old_text`;
# four zero-match errors tripped the core's per-turn consolidation guard and
# nothing was written that night. Hence: hand the anchor over, ready to copy.
REPLACE_ANCHOR_CHARS = 60
ANCHOR_MIN_CHARS = 12
# Above this the whole entry is not handed over verbatim (prompt budget); the
# agent is told the entry is bigger than the preview instead.
FULL_ENTRY_MAX_CHARS = 1200


def _replace_anchor(entry, sources):
    """A VERBATIM substring of `entry` that occurs in exactly one entry.

    Raw, with the original whitespace: the core looks for `old_text` as a
    literal substring, so collapsing spaces (as this did until 2026-10-08)
    made 5 of 56 anchors unfindable on a live memory file.

    Preference order is "what a human would copy": a whole single line, from
    the top down, trimmed to REPLACE_ANCHOR_CHARS and then at full length. A
    heading-only line like "## Marina" is too short and too common to locate
    anything, hence ANCHOR_MIN_CHARS. The last resort is a raw prefix of the
    entry, newline and all — it still matches, it is just less pleasant to
    copy. The anchor only LOCATES the entry; `replace` then rewrites the whole
    of it, which is why `nearest_entry` ships the full text next to it."""
    raw = entry or ""
    if not raw.strip():
        return None
    lines = [ln.strip() for ln in raw.split("\n") if ln.strip()]
    candidates = []
    for line in lines:
        candidates.append(line[:REPLACE_ANCHOR_CHARS].rstrip())
        if len(line) > REPLACE_ANCHOR_CHARS:
            candidates.append(line)
    candidates.append(raw[:REPLACE_ANCHOR_CHARS].rstrip())
    seen = set()
    for cand in candidates:
        if len(cand) < ANCHOR_MIN_CHARS or cand in seen:
            continue
        seen.add(cand)
        if sum(1 for _, other in sources if cand in (other or "")) == 1:
            return cand
    return None


def nearest_entry(content, memory_text=None, sources=None):
    """The durable memory chunk most similar to `content`, or None.

    Shown next to a candidate so the agent UPDATES the existing entry instead
    of appending a near-duplicate (mem0's ADD/UPDATE/NOOP idea, without an LLM
    in the cron). Carries `source` (which file to edit) and, when unambiguous,
    `old_text` — a verbatim anchor for `memory replace`."""
    if sources is None:
        sources = [("", chunk) for chunk in _memory_chunks(memory_text)]
    item_stems = _stems(sig_tokens(content))
    if len(item_stems) < 3:
        return None
    best, best_src, best_score = None, "", 0.0
    for src, chunk in sources:
        chunk_stems = _stems(sig_tokens(chunk))
        if not chunk_stems:
            continue
        shared = len(item_stems & chunk_stems)
        if shared < 3:
            continue
        score = shared / min(len(item_stems), len(chunk_stems))
        if score > best_score:
            best, best_src, best_score = chunk, src, score
    if best is None or best_score < 0.3:
        return None
    item = {"entry": best[:160], "overlap": round(best_score, 2)}
    if best_src:
        item["target"] = best_src
    # An entry a human wrote with a credential (or an instruction) in it: the
    # candidate still learns that it has a neighbour, but not its text — the
    # full entry and the replace anchor carried the secret into the prompt, the
    # cache and the cron log (Codex review of 2.7.2, 2026-10-14).
    if classify_unsafe(best):
        item.update(entry=ENTRY_REDACTED, replace_unsafe=True,
                    note="this entry holds a secret or an instruction and is not shown: "
                         "do not replace it — add a separate entry or ask")
        return item
    # `replace` rewrites the WHOLE entry, so the agent must know what it is
    # about to overwrite: how big the entry is, whether it has more than one
    # line/section, and — while it fits the prompt budget — its full text, so
    # the edit can keep what is already there (and pin `matched_entry`).
    if len(best) > 160:
        item["entry_chars"] = len(best)
    sections = [ln for ln in best.split("\n") if ln.strip()]
    if len(sections) > 1:
        item["multi_section"] = True
        item["note"] = ("replace rewrites this ENTIRE entry — carry over everything "
                        "worth keeping into new_content")
    anchor = _replace_anchor(best, sources)
    if len(best) <= FULL_ENTRY_MAX_CHARS:
        item["full_entry"] = best
        if anchor:
            item["old_text"] = anchor
    elif anchor:
        # Too big to show, so an update cannot be composed safely here.
        item["replace_unsafe"] = True
        item["note"] = ("this entry is longer than the preview and is not shown in full: "
                        "do not replace it blind — add a separate entry or ask")
    return item


# ---------------------------------------------------------------------------
# Loss guard: snapshot of durable entries between passes
# ---------------------------------------------------------------------------
# The script never writes memory, but the agent does after every dream. A bad
# turn ("memory replace" that swallowed half of MEMORY.md) would otherwise stay
# silent until somebody notices. The pass keeps the §-entry keys of the durable
# files; if on the next pass more than `memory_loss_alert_fraction` of them are
# gone, an alert is raised for the human (nothing is restored automatically —
# the human may have pruned on purpose).

def _entry_keys_of(path):
    return {_entry_key(e) for e in parse_md_entries(path)}


ENTRY_HEAD_CHARS = 60
# Below this an entry is too short for "it shrank" to mean anything.
SHRINK_MIN_CHARS = 160
# A file with this much text is worth guarding even when it holds few entries
# (a 4-entry MEMORY.md is normal; losing 90% of its characters is not).
LOSS_MIN_CHARS = 400


def _entry_head_key(entry):
    """Key of an entry's BEGINNING — survives a truncation that `_entry_key`
    (first 200 normalized chars) would see as a different entry."""
    return hashlib.md5(_norm(entry)[:ENTRY_HEAD_CHARS].encode("utf-8")).hexdigest()[:16]


def _entry_sizes_of(entries):
    """Snapshot of the notes: sizes by entry key and by entry head.

    Sizes make the guard notice that one BIG entry vanished among small ones;
    head keys make it notice an entry that kept its beginning and lost its
    body — the exact shape of a replace that swallowed a multi-section entry."""
    return {"entries": {_entry_key(e): len(e) for e in entries},
            "heads": {_entry_head_key(e): len(e) for e in entries}}


def _ints(value):
    if not isinstance(value, dict):
        return {}
    out = {}
    for k, v in value.items():
        try:
            out[str(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def _snapshot_sizes(value):
    """Previous snapshot for one file → ({entry_key: chars}, {head_key: chars}).
    Tolerates both older formats: a bare list of keys and a flat {key: chars}."""
    if isinstance(value, list):
        return {str(k): 0 for k in value}, {}
    if isinstance(value, dict):
        if "entries" in value or "heads" in value:
            return _ints(value.get("entries")), _ints(value.get("heads"))
        return _ints(value), {}
    return {}, {}


def load_snapshot(path):
    return _load_state_file(path)


_PROBE_SENTENCE_RE = re.compile(r"[.;!?。；！？\n]+")


def probe_found(probe, memory_text):
    """A probe is in memory when its text is (case, ё and spacing aside), or every word of it is in one entry
    (any form of it: "allergic to peanuts" / "peanut allergy" is not that, "аллергия на арахис" / "аллергии на
    арахис" is)."""
    if _norm(probe) in _norm(memory_text):
        return True
    stems = _stems(sig_tokens(probe))
    # in one sentence: "allergic to pollen; Bob likes peanuts" holds both words and not the fact (review 2026-10-13)
    return bool(stems) and any(stems <= _stems(sig_tokens(sentence)) for chunk in _memory_chunks(memory_text)
                               for sentence in _PROBE_SENTENCE_RE.split(chunk))


def check_probes(memory_text, previous_missing):
    """(alerts, missing): one alert per probe that went missing since the previous pass (or was never found);
    a probe still missing stays quiet — the alert said it, the agent cannot restore it on its own."""
    missing = [p for p in SETTINGS.PROBES if not probe_found(p, memory_text)]
    before = set(previous_missing or [])
    alerts = [{"kind": "probe_missing", "probe": p[:120],
               "message": f"memory no longer holds “{p[:120]}” (a probe in the settings) — check whether an "
                          f"entry was merged or removed by mistake."} for p in missing if p not in before]
    return alerts, missing


def check_memory_loss(memory, snapshot_path, memory_text=None):
    """Compare the notes with the previous snapshot; write a new one. Returns a list of alerts (possibly empty)."""
    if not snapshot_path:
        return []
    previous = load_snapshot(snapshot_path)
    alerts = []
    probes_missing = []
    if SETTINGS.PROBES and memory_text is not None:
        probe_alerts, probes_missing = check_probes(memory_text, previous.get("_probes_missing"))
        alerts.extend(probe_alerts)
    name = "notes"
    prev_sizes, prev_heads = _snapshot_sizes(previous.get(name))
    prev = set(prev_sizes)
    snap = _entry_sizes_of(parse_md_entries(memory))
    sizes, heads = snap["entries"], snap["heads"]
    keys = set(sizes)
    prev_total = sum(prev_sizes.values())
    if len(prev) >= 4 or prev_total >= LOSS_MIN_CHARS:
        lost = prev - keys
        frac = len(lost) / len(prev) if prev else 0.0
        lost_chars = sum(prev_sizes.get(k, 0) for k in lost)
        char_frac = (lost_chars / prev_total) if prev_total else 0.0
        if frac > SETTINGS.MEMORY_LOSS_ALERT_FRACTION or char_frac > SETTINGS.MEMORY_LOSS_ALERT_FRACTION:
            # Entry count alone misses "one big note vanished"; the share of characters alone misses "many small
            # notes gone". Both, then.
            alerts.append({"kind": "memory_loss", "file": name, "lost": len(lost), "had": len(prev),
                           "lost_chars": lost_chars, "had_chars": prev_total,
                           "message": f"{len(lost)} of {len(prev)} memory notes present at the previous pass are "
                                      f"gone ({round(100 * frac)}% of notes, {round(100 * char_frac)}% of "
                                      f"characters). Check whether that was intended."})
        # A note that kept its head but lost most of its body.
        for key, was in prev_heads.items():
            now_len = heads.get(key)
            if not was or now_len is None or was < SHRINK_MIN_CHARS:
                continue
            if now_len < was * (1 - SETTINGS.MEMORY_LOSS_ALERT_FRACTION * 2):
                alerts.append({"kind": "entry_truncated", "file": name, "had_chars": was, "chars": now_len,
                               "message": f"a memory note shrank from {was} to {now_len} characters while keeping "
                                          f"its beginning — check that nothing was dropped from it."})
    try:
        _write_private(snapshot_path, json.dumps(
            {name: snap, "_at": _now().isoformat(timespec="seconds"),
             **({"_probes_missing": probes_missing} if probes_missing else {})}, ensure_ascii=False))
    except OSError as e:
        print(f"[dream] warn: snapshot not written: {e}", file=sys.stderr)
    return alerts


def _agent_answers(acks, generated_at):
    """(acked, the answers) of the night stamped `generated_at`. *acks*: {generated_at: [answer texts]} — the nights
    a person answered, given by the caller (a night is acknowledged only by an answer, never by being shown). A merge
    question counts as answered only when an answer names its number (`D-17`)."""
    if not generated_at:
        return True, None
    answers = [a for a in (acks or {}).get(generated_at) or [] if str(a or "").strip()]
    return bool(answers), answers


def _revert_unacked(seen, acks):
    """Pop the previous pass's `_pending` record from the seen state; if that
    showing was never acknowledged, drop its marks. Returns the md_decays keys
    to re-ask (the asked-state lives in its own file)."""
    pending = seen.pop("_pending", None)
    if not isinstance(pending, dict) or not pending.get("generated_at"):
        return set()
    acked, answers = _agent_answers(acks, pending.get("generated_at"))
    if acked:
        _reopen_unnamed_proposals(pending, seen, answers)
        return set()
    dropped = 0
    for bucket_name, keys in (pending.get("seen") or {}).items():
        bucket = seen.get(bucket_name)
        if not isinstance(bucket, dict):
            continue
        for k in keys or []:
            # Ride-along sections are reopened silently: on a night the gate kept shut nobody was meant to answer.
            if bucket.pop(k, None) is not None and bucket_name not in ("trust_feedback", "keywords_needed", "confirm"):
                dropped += 1
    reask = set(pending.get("asked") or [])
    if dropped or reask:
        print(f"[dream] note: the pass of {pending['generated_at']} was not acknowledged by an "
              f"agent turn — {dropped} cooldown(s) and {len(reask)} md question(s) reopened",
              file=sys.stderr)
    return reask


def _load_asked_state(path):
    """{entry_key: {"at", "confirmed"?, "stems"?}}. A legacy value (a bare ISO
    date — the format before confirmations existed) reads as {"at": value}."""
    out = {}
    for k, v in _load_state_file(path).items():
        if isinstance(v, str):
            out[k] = {"at": v}
        elif isinstance(v, dict) and v.get("at"):
            out[k] = dict(v)
    return out


# A §-entry rewritten after a "still relevant?" question is the SAME entry with
# a new key: the key hashes the text, and the usual answer to the question is a
# fresh date stamp ("As of 2026-09-23, …"). Live case 18–19.09 and 24–30.09:
# three entries rewritten that way were asked again the very next night, and
# one six days later — the question came back precisely BECAUSE it had been
# answered. So an entry without a record of its own inherits the record of a
# vanished one on the same subject (≥4 shared stems, ≥0.7 of the shorter side),
# and the inheritance itself is the confirmation. Numbers are deliberately
# ignored: a new date is the very thing a confirming rewrite changes. The bar is
# lower than dedupe's because the pool is tiny — only entries that vanished
# after a question — and a false match costs little: a freshly written entry
# skips one "still relevant?" round. Rewrites inflect words («полке» → «полка»),
# and crude 5-char stems lose those, so 0.8 missed a plain rephrasing.
MD_SUCCESSOR_MIN_SHARED = 4
MD_SUCCESSOR_RATIO = 0.7


def _md_successor(stems, orphans):
    """Key of the vanished asked entry `stems` most likely rewrites, or None."""
    best, best_ratio = None, 0.0
    for key, rec in orphans.items():
        other = set(rec.get("stems") or ())
        shared = stems & other
        if len(shared) < MD_SUCCESSOR_MIN_SHARED:
            continue
        ratio = len(shared) / min(len(stems), len(other))
        if ratio >= MD_SUCCESSOR_RATIO and ratio > best_ratio:
            best, best_ratio = key, ratio
    return best


def _md_cooldown_end(rec):
    """When the running cooldown of an asked-record ends (None — no cooldown)."""
    confirmed = _parse_iso(rec.get("confirmed"))
    if confirmed:
        return confirmed + timedelta(days=SETTINGS.MD_CONFIRMED_COOLDOWN_DAYS)
    at = _parse_iso(rec.get("at"))
    return at + timedelta(days=SETTINGS.MD_ASK_COOLDOWN_DAYS) if at else None


@_message_pass
def md_decays(memory, messages, decay_days, asked_state_path=None, cap=None, reask=(),
              staged_removals=(), rules=None):
    """Memory notes that have not surfaced in conversations for long
    → soft flag "still relevant?".

    Cooldowns. An entry asked within MD_ASK_COOLDOWN_DAYS is not raised again
    (without it the dream asked the same thing every night). An entry that
    SURVIVED a question counts as confirmed and rests for
    MD_CONFIRMED_COOLDOWN_DAYS: either it is still there unchanged when the
    short cooldown runs out (the agreed way to say "not relevant" is to remove
    or rewrite the entry, so keeping it is the answer), or it was rewritten in
    place — see `_md_successor`. Records outlive their entry until their own
    cooldown ends: a rewrite must still find the record of the text it replaced.

    Only entries that make it into the published slice (`cap`) are marked as
    asked — an overflow entry never reached the agent. `reask` — keys whose
    previous showing was never acknowledged by an agent turn (see
    `_agent_acked`): their records are dropped, the question is asked anew."""
    out = []
    now = _now()
    now_ts = now.timestamp()
    stamp = now.isoformat(timespec="seconds")
    asked = _load_asked_state(asked_state_path) if asked_state_path else {}
    changed = False
    for key in reask:
        if key in asked:
            asked.pop(key, None)
            changed = True
    entries = [(i, entry, _entry_key(entry)) for i, entry in enumerate(parse_md_entries(memory))]
    current_keys = {key for _, _, key in entries}
    orphans = {k: r for k, r in asked.items() if k not in current_keys and r.get("stems")}
    for i, entry, key in entries:
        stems = _stems(sig_tokens(entry))
        rec = asked.get(key)
        if rec is None and orphans:
            prev = _md_successor(stems, orphans)
            if prev:
                rec = orphans.pop(prev)
                asked.pop(prev, None)
                rec["confirmed"] = stamp
                asked[key] = rec
                changed = True
                print(f"[dream] note: MEMORY entry #{i} rewrites an entry asked on "
                      f"{rec.get('at', '?')[:10]} — counted as confirmed", file=sys.stderr)
        if rec is not None and rec.get("stems") != sorted(stems):
            rec["stems"] = sorted(stems)  # also upgrades a legacy record
            changed = True
        if is_pinned(entry):
            continue
        if any(t and t in entry for t in staged_removals):
            # Its removal (or rewrite) is already waiting for approval: asking
            # again is noise, and "still here, so it is confirmed" would be a
            # lie — the answer was given, it is the queue that is stuck.
            continue
        toks = fact_tokens(entry)
        mentions, ref_days, last_ts = corroborate(toks, messages, entry, rules=rules)  # never a judge's veto
        days_since = (now_ts - last_ts) / 86400 if last_ts else None
        if not (mentions == 0 or (days_since is not None and days_since > decay_days)):
            continue
        if rec is not None:
            end = _md_cooldown_end(rec)
            if end and now < end:
                continue
            if not rec.get("confirmed"):
                # Asked, short cooldown over, entry still here unchanged: kept.
                rec["confirmed"] = stamp
                changed = True
                continue
        if cap is not None and len(out) >= cap:
            break
        asked[key] = {"at": stamp, "stems": sorted(stems)}
        changed = True
        out.append({"index": i, "entry": _safe_preview(entry, 160), "key": key,
                    "last_mention_days": round(days_since, 1) if days_since is not None else None,
                    "reason": "never surfaced within the window" if mentions == 0
                              else f"not surfaced for ~{round(days_since)} d"})
    if asked_state_path:
        # A record of a vanished entry is kept only while its cooldown runs.
        kept = {k: v for k, v in asked.items()
                if k in current_keys or ((_md_cooldown_end(v) or now) > now)}
        if changed or kept.keys() != asked.keys():
            try:
                _write_private(asked_state_path, json.dumps(kept, ensure_ascii=False, indent=1))
            except OSError as e:
                print(f"[dream] warn: asked-state not written: {e}", file=sys.stderr)
    return out


# ---------------------------------------------------------------------------
# Main pass
# ---------------------------------------------------------------------------

@_message_pass
def _score_facts(facts, all_messages, by_store, rejected, memory_text, staged_text, now=None, rules=None, veto=None,
                 counted=None):
    """Every fact with its score and what the sections decide on: quarantine class, already in memory or staged
    for approval, rejected by a human — or rejected and said again since, which makes it a candidate to ask about."""
    scored = []
    for f in facts:
        score, sig, meta = signals_for_fact(f, messages_for(f, all_messages, by_store), now=now, rules=rules,
                                            veto=veto, counted=counted)
        rejection = rejection_of(f["content"], rejected)
        again = rejection is not None and said_again(f, meta, rejection, now=now)
        scored.append({**f, "score": score, "signals": sig, "meta": meta,
                       "unsafe": classify_unsafe(f["content"]),
                       "in_memory": already_in_memory(f["content"], memory_text),
                       "staged": bool(staged_text)
                       and already_in_memory(f["content"], staged_text),
                       "rejected": rejection is not None and not again,
                       # said again after a human rejected it: a candidate once more, but one to ask about
                       **({"previously_rejected": {"at": str(rejection.get("at") or "")[:10],
                                                   "reason": str(rejection.get("reason") or "")[:160]}}
                          if again else {})})
    return scored


def _select_promotions(safe, seen_promotions, now):
    """(promotions, their hidden store twins, one-off events, how many rest after a recent showing): facts over
    the score gate, corroborated, not in memory or staged; dated one-off events go to their own section."""
    promotion_candidates = [
        s for s in safe
        if s["score"] >= SETTINGS.MIN_SCORE and promotion_ready(s)
        and not s["in_memory"] and not s["staged"]
    ]
    # Short rest after a promotion was actually shown (and the night acked): a
    # candidate the agent could not write — approval gate, budget, a refusal —
    # used to come back every single night.
    promotions_before_cooldown = len(promotion_candidates)
    promotion_candidates = unseen(promotion_candidates, seen_promotions, now,
                                  cooldown_days=SETTINGS.PROMOTION_COOLDOWN_DAYS)
    promotions_suppressed = promotions_before_cooldown - len(promotion_candidates)
    ephemeral_events = [s for s in promotion_candidates if is_ephemeral_fact(s["content"])]
    promotions = [s for s in promotion_candidates if not is_ephemeral_fact(s["content"])]
    promotions.sort(key=lambda s: -s["score"])
    ephemeral_events.sort(key=lambda s: -s["score"])
    promotions, promotions_dups = collapse_store_duplicates(promotions)
    return promotions, promotions_dups, ephemeral_events, promotions_suppressed


def _conflict_candidates(safe, promoted_ids, memory_text):
    """Facts that may update a memory entry (same subject, other numbers), each with its `conflicts`."""
    conflicts_pending = []
    for s in safe:
        if s["fact_id"] in promoted_ids:
            continue
        if is_ephemeral_fact(s["content"]) or exact_or_alias_in_memory(s["content"], memory_text):
            continue
        hits = find_conflicts(s["content"], memory_text)
        if hits:
            conflicts_pending.append({**s, "conflicts": hits})
    return conflicts_pending


def _decay_candidates(safe, window_days):
    """Facts nobody retrieved or mentioned for twice the window, not trusted above 0.5, not in memory."""
    return [s for s in safe
            if (s["meta"]["rc"] == 0 and s["meta"]["mentions"] == 0
                and s["meta"]["days_old"] > window_days * 2
                and (s["trust_score"] if s["trust_score"] is not None else 0.5) <= 0.5
                and not s["in_memory"])]


def _new_themes(messages, window_days, facts, memory_text):
    """Recurring themes of the window that no fact and no memory entry holds yet; a suspicious sample hidden."""
    facts_text = " ".join(_norm(f["content"]) for f in facts)
    themes = [t for t in extract_themes(messages, window_days)
              if t["theme"] not in facts_text and t["theme"] not in _norm(memory_text)]
    for t in themes:
        if classify_unsafe(t.get("sample")):
            t["sample"] = "[hidden: suspicious content]"
    return themes


def _mark_shown(seen, now, *, new_facts, promotions, trust_feedback, keywords_needed, confirm, fact_decays,
                conflicts):
    """Stamp what this pass shows into the cooldown buckets of `seen` (only the published slices), then drop
    stamps older than their cooldown."""
    mark_seen(new_facts, seen["new_facts"], now)
    mark_seen(promotions, seen["promotions"], now)
    mark_seen(trust_feedback, seen["trust_feedback"], now, key=_feedback_key)
    mark_seen(keywords_needed, seen["keywords_needed"], now, key=_feedback_key)
    mark_seen(confirm, seen["confirm"], now, key=_feedback_key)
    mark_seen(fact_decays, seen["fact_decays"], now)
    mark_seen(conflicts, seen["conflicts"], now)
    for name in ("new_facts", "fact_decays", "conflicts"):
        prune_seen(seen[name], now)
    prune_seen(seen["promotions"], now, cooldown_days=SETTINGS.PROMOTION_COOLDOWN_DAYS)
    prune_seen(seen["trust_feedback"], now)
    prune_seen(seen["keywords_needed"], now)
    # Pruned like the others (it was not before 3.0: one key more for every fact ever offered for a confirmation). Past
    # two cooldowns a stamp holds nothing back (`unseen` lets the fact through after one), so pruning changes no answer.
    prune_seen(seen["confirm"], now)


BUCKETS = ("new_facts", "fact_decays", "conflicts", "promotions", "trust_feedback", "keywords_needed", "confirm")


class DecisionInputs(namedtuple("DecisionInputs", "now today window_days rules facts messages by_store memory_text "
                                "staged_text pending_writes rejected recent_confirmations feedback_floor "
                                "fact_source seen messages_unavailable")):
    """Everything the night's decisions read from the world, read once. `_decide` reads nothing else — no clock,
    database, environment or file; it reads the settings, which must not change while it runs
    (`settings_fingerprint`). `seen` is a copy: deciding never marks anything shown. `recent_confirmations`: a
    frozenset of fact ids, or None when the store keeps no confirmations. `staged_text`, `pending_writes` and
    `messages_unavailable` stay empty here (the caller decides what a night may ask when a source was unreadable).

    A named tuple, not a dataclass: under `from __future__ import annotations` a dataclass looks its module up in
    sys.modules, so a script that loaded dream.py by path without registering it failed to load it at all (3.3.1)."""
    __slots__ = ()


def load_inputs(window_days, memory, messages, rejected_state_path=None, seen=None, now=None):
    """The DecisionInputs of a night: the store's facts (read now, read-only), the prepared *messages*, *memory*,
    the reject list and the cooldown state *seen*."""
    facts = load_facts()
    now = now or _now()
    source = fact_source()
    rested = recent_confirmations(fact_store_path(), now=now) if source == "holographic" else None
    return DecisionInputs(
        now=now, today=now.astimezone(SETTINGS.LOCAL_TZ).date(), window_days=window_days,
        rules=rules_context(f.get("content") for f in facts),
        facts=tuple(facts), messages=tuple(messages),
        by_store=types.MappingProxyType({k: tuple(v) for k, v in messages_by_store(messages).items()}),
        memory_text=load_durable_memory_text(memory), staged_text="", pending_writes=(),
        rejected=types.MappingProxyType(load_rejected(rejected_state_path)),
        recent_confirmations=None if rested is None else frozenset(rested),
        feedback_floor=feedback_floor(source), fact_source=source,
        seen=copy.deepcopy(seen or {}), messages_unavailable=False)


class Decisions(namedtuple("Decisions", "scored suppressed quarantined expired expired_ids new_facts_window "
                                  "new_facts new_facts_dups new_facts_suppressed all_promotions promotions_dups "
                                  "all_ephemeral promotions_suppressed staged_suppressed all_conflicts "
                                  "conflicts_suppressed all_decays decays_suppressed trust_feedback "
                                  "feedback_suppressed keywords_needed keywords_pending confirm counted "
                                  "publish_cap")):
    """What one night decides from the mention count, before anything is marked shown. The sections named as in
    the payload are the published slices; `all_*` are the whole lists (stats, diary). `counted`: the PairIds that
    counted, per path ("promotion": the facts' signals; "confirm": confirm_items). A named tuple, as DecisionInputs."""
    __slots__ = ()

    @property
    def promotions(self):
        return self.all_promotions[:self.publish_cap]

    @property
    def ephemeral_events(self):
        return self.all_ephemeral[:self.publish_cap]

    @property
    def conflicts(self):
        return self.all_conflicts[:self.publish_cap]

    @property
    def fact_decays(self):
        return self.all_decays[:self.publish_cap]


@_message_pass
def _decide(inputs, vetoes=frozenset()):
    """The night's decisions from *inputs*, with the PairIds in *vetoes* not counted (a cloud judge's verdicts;
    empty: the rules alone). Pure: called twice it gives the same answer and it touches nothing outside *inputs*."""
    now, window_days, source = inputs.now, inputs.window_days, inputs.fact_source
    facts, all_messages, by_store = inputs.facts, inputs.messages, inputs.by_store
    rejected, memory_text, staged_text = inputs.rejected, inputs.memory_text, inputs.staged_text
    (seen_new_facts, seen_fact_decays, seen_conflicts, seen_promotions, seen_feedback, seen_keywords,
     seen_confirm) = (inputs.seen.get(name) or {} for name in BUCKETS)
    counted = {"promotion": set(), "confirm": set()}

    scored = _score_facts(facts, all_messages, by_store, rejected, memory_text, staged_text, now=now,
                          rules=inputs.rules, veto=vetoes, counted=counted["promotion"])

    # Rejected by a human leaves through no section — not as a candidate, not
    # as a "new fact", not as a relevance question. Removed BEFORE quarantine:
    # otherwise a quarantined fact could never be closed (the reject list did
    # not reach it, deleting from the DB needs an explicit human request, and
    # `quarantined` is actionable) and woke the agent every night forever.
    suppressed = [s for s in scored if s["rejected"]]
    active = [s for s in scored if not s["rejected"]]

    # Quarantine: secrets and facts with embedded instructions never leave.
    # Secrets — no content at all; injection — a short preview in the full JSON only.
    quarantined = [s for s in active if s["unsafe"]]
    safe = [s for s in active if not s["unsafe"]]

    # A dated event whose date has passed cannot be worked on — but dropping it
    # without a trace meant a durable fact that merely CONTAINED a trigger word
    # («страница», «тест», "example") vanished from every section silently
    # (external review, 2026-10-07). It is now listed in its own read-only
    # section and in the diary; it still wakes nobody.
    expired = [s for s in safe if is_expired_event(s["content"], today=inputs.today)]
    expired_ids = {s["fact_id"] for s in expired}
    safe = [s for s in safe if s["fact_id"] not in expired_ids]

    # Every fact created or reinforced during the window is surfaced for a
    # separate profile review — not pre-filtered by score (a stable preference
    # matters before it accumulates retrievals), minus what is already in
    # durable memory, minus what was shown within the cooldown (before the cap,
    # so suppressed candidates do not take slots from unseen ones).
    new_facts_window = [s for s in safe if s["meta"]["days_old"] <= window_days]
    new_facts_pending = [s for s in new_facts_window
                         if not s["in_memory"] and not s["staged"]]
    new_facts = unseen(new_facts_pending, seen_new_facts, now)
    new_facts_suppressed = len(new_facts_pending) - len(new_facts)
    new_facts.sort(key=lambda s: (s["meta"]["days_old"], -s["score"]))

    promotions, promotions_dups, ephemeral_events, promotions_suppressed = _select_promotions(
        safe, seen_promotions, now)
    staged_suppressed = sum(1 for s in safe if s.get("staged"))
    # A fact already offered as a promotion is not repeated in new_facts: the
    # agent reviews it once (live case: 8 extracted facts arrived in both
    # sections, doubling the prompt for no extra work). A duplicate hidden
    # behind a promotion counts as offered too.
    promoted_ids = {s["fact_id"] for s in promotions} | {s["fact_id"] for s in promotions_dups}
    new_facts = [s for s in new_facts if s["fact_id"] not in promoted_ids]
    # Collapsed before the cap, so a hidden twin does not take a slot. Twins of
    # a published representative are marked shown together with it (below);
    # twins of one cut by the cap wait for it.
    new_facts, new_facts_dups = collapse_store_duplicates(new_facts)
    new_facts = new_facts[:SETTINGS.NEW_FACTS_CAP]
    shown_twins = {i for s in new_facts for i in s.get("duplicates", ())}
    new_facts_dups = [s for s in new_facts_dups if s["fact_id"] in shown_twins]

    # Possible updates of existing entries: same subject, different numbers.
    # Deliberately NOT gated by fuzzy `in_memory`: a reworded fact with a new
    # number is exactly what fuzzy dedupe swallows as "already known". Only an
    # identical head or a human-declared alias rule closes the question.
    # A fact already offered as a promotion is not repeated here either: it
    # arrives with its own `nearest_entry`, so the agent sees the possible
    # update once, in one section (the head-match fix surfaced eight sibling
    # facts at once, each doubled as a "conflict" with an unrelated sibling).
    conflicts_pending = _conflict_candidates(safe, promoted_ids, memory_text)
    conflicts = unseen(conflicts_pending, seen_conflicts, now)
    conflicts_suppressed = len(conflicts_pending) - len(conflicts)
    conflicts.sort(key=lambda s: -s["score"])

    # "Nobody mentioned it" is unknown on a night the messages could not be read: no relevance question then.
    decays_pending = [] if inputs.messages_unavailable else _decay_candidates(safe, window_days)
    decays = unseen(decays_pending, seen_fact_decays, now)
    decays_suppressed = len(decays_pending) - len(decays)
    decays.sort(key=lambda s: -s["meta"]["days_old"])

    # Rejected facts the store still trusts: offer the one call that teaches
    # retrieval. Shown at most once per cooldown, and never on a source without
    # the tool.
    # Chat-store facts get neither call: a cron session cannot reach a chat's database.
    feedback_pending = trust_feedback_items([s for s in suppressed if not s.get("store")], rejected, source,
                                            floor=inputs.feedback_floor)
    feedback_fresh = unseen(feedback_pending, seen_feedback, now, key=_feedback_key)
    # Only the shown slice is marked, so what the cap cuts off is not suppressed
    # — it simply comes tomorrow. The counter means "resting", nothing else.
    feedback = feedback_fresh[:SETTINGS.FEEDBACK_CAP]
    feedback_suppressed = len(feedback_pending) - len(feedback_fresh)

    # Facts without keywords (holographic >= 0.6.0 searches them like text):
    # rides along when the agent is awake anyway. Quarantined, rejected and
    # expired facts are not offered — `safe` already left them out. A fact the
    # agent found nothing for rests like any shown item instead of coming back
    # every night; one that got keywords simply leaves the list.
    keywords_pending = keywords_needed_items([s for s in safe if not s.get("store")], source)
    keywords_fresh = unseen(keywords_pending, seen_keywords, now, key=_feedback_key)
    keywords_needed = keywords_fresh[:SETTINGS.KEYWORDS_CAP]

    # Facts people said again on different days (holographic >= 0.15.0): a confirmation is worth giving while the
    # agent is awake, never a reason to wake it.
    confirm_pending = confirm_items(safe, all_messages, by_store, source, rules=inputs.rules, now=now,
                                    veto=vetoes, rested=inputs.recent_confirmations,
                                    counted=counted["confirm"])
    confirm = unseen(confirm_pending, seen_confirm, now, key=_feedback_key)[:CONFIRM_CAP]


    return Decisions(
        scored=scored, suppressed=suppressed, quarantined=quarantined, expired=expired, expired_ids=expired_ids,
        new_facts_window=new_facts_window, new_facts=new_facts, new_facts_dups=new_facts_dups,
        new_facts_suppressed=new_facts_suppressed, all_promotions=promotions, promotions_dups=promotions_dups,
        all_ephemeral=ephemeral_events, promotions_suppressed=promotions_suppressed,
        staged_suppressed=staged_suppressed, all_conflicts=conflicts, conflicts_suppressed=conflicts_suppressed,
        all_decays=decays, decays_suppressed=decays_suppressed, trust_feedback=feedback,
        feedback_suppressed=feedback_suppressed, keywords_needed=keywords_needed, keywords_pending=keywords_pending,
        confirm=confirm, counted={k: frozenset(v) for k, v in counted.items()}, publish_cap=SETTINGS.PUBLISH_CAP)


# ---------------------------------------------------------------------------
# Duplicates across the store (3.4.0): the night's pairs and the "merge?" proposals
# ---------------------------------------------------------------------------
# Part A of 3.4.0. The pass finds pairs of facts of the main store that look like one fact (`find_duplicate_pairs`)
# and asks the human, one pair a night (`duplicates.per_night`), "merge?" — with both texts whole and a proposal
# number (`D-17`) that is never reused. The number names a record of `cache/dream-proposals.json`: the texts the
# human was shown, their `version_id`s, which side stays (`keep`) and which goes (`copy`). On a "yes" to that number
# the agent calls `fact_store merge {fact_id: keep, from: [copy], keywords, expect: {keep: v, copy: v}}` — holographic
# 0.19.0 merges only while both facts are still at those versions, and refuses a scheduled job a merge of facts it
# did not store itself: at night nothing of this is written to the store, by the provider's code. The pass writes
# nothing to the store either; it reads `fact_merges` to know what was merged (merge_sweep, below).
#
# Without holographic 0.19.0 (`fact_merges`), with a scope other than `profile` (the session that answers would
# write to another database than the one the pass read), or with a non-holographic source, the section is off and
# `stats.duplicate_pairs_skipped` says why. Chat stores take no part.
DUP_OPEN_DAYS = 180         # an open proposal nobody answered goes stale after this long
DUP_CLOSED_KEEP_DAYS = 30   # a closed one is kept this long, for a look at what happened, then removed
DUP_KEYWORDS_MAX = 12
PROPOSAL_RE = re.compile(r"(?<![\w-])D-(\d+)(?!\w)")
_ORIGIN_RANK = {"said": 2, "confirmed": 2, "inferred": 0}  # unknown: 1


def pair_key(text_a, text_b):
    """The key of a pair of texts: a hash of their two fingerprints, sorted. A fingerprint is of the normalized
    text — an edit of case or spaces only keeps the key."""
    fps = sorted((_fact_fingerprint(text_a), _fact_fingerprint(text_b)))
    return hashlib.sha256("|".join(fps).encode("utf-8")).hexdigest()[:20]


def store_merges(db):
    """{copy id: keep id} of holographic's `fact_merges` (0.19.0), or None when the store has no such table."""
    c = _conn(db)
    try:
        if not c.execute("select 1 from sqlite_master where type='table' and name='fact_merges'").fetchone():
            return None
        return {int(f): int(t) for f, t in c.execute("select from_fact_id, into_fact_id from fact_merges")}
    finally:
        c.close()


def store_versions(db):
    """({fact_id: (current version_id, its text)}, {fact_id: number of versions}) of the store's history. The id
    and the text of a version come from one statement — one snapshot of the database: a proposal must never pair
    the text of one version with the id of another (a "yes" with `expect` would then merge a text nobody saw)."""
    c = _conn(db)
    try:
        current = {int(f): (int(v), t) for f, v, t in c.execute(
            "select fact_id, version_id, content from fact_versions where status = 'current'")}
        counts = {int(f): int(n) for f, n in c.execute("select fact_id, count(*) from fact_versions group by fact_id")}
        return current, counts
    finally:
        c.close()


def load_proposals(path):
    """{"next_proposal": n, "proposals": {"D-17": record}} of `cache/dream-proposals.json` (for the holder of the
    state lock: a broken file is moved aside)."""
    data = _load_state_file(path) if path else {}
    props = data.get("proposals") if isinstance(data.get("proposals"), dict) else {}
    props = {k: v for k, v in props.items() if isinstance(v, dict) and PROPOSAL_RE.fullmatch(k)}
    try:
        nxt = max(1, int(data.get("next_proposal") or 1))
    except (TypeError, ValueError):
        nxt = 1
    return {"next_proposal": nxt, "proposals": props}


def save_proposals(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    _write_private(path, json.dumps(data, ensure_ascii=False, indent=1))


def _proposal_number(key):
    return int(key.split("-", 1)[1])


def _close_proposal(rec, status, now, why):
    rec.update(status=status, closed_at=now.isoformat(timespec="seconds"), closed_why=why)


def maintain_proposals(data, now, merges=None, current=None, store=None, released=None):
    """Bring the open proposals up to what the store holds now; drop closed ones past DUP_CLOSED_KEEP_DAYS.
    *store* (the database read now, None when unknown): a proposal made from another database is `stale` — its
    ids and versions are that one's. *merges* ({copy: keep}): a pair merged into each other is `done`, a
    participant merged elsewhere makes it `stale`. *current* ({fact_id: version_id}, None when unknown): a
    participant gone (ended, forgotten) or at another version than the one shown makes it `stale` — the human
    answered other texts; such a proposal's number goes to *released* (a list), whose pair may be asked about
    again at once (`release_pair`). An open proposal older than DUP_OPEN_DAYS goes `stale` too. Returns whether
    anything changed."""
    changed = False
    for key in sorted(data["proposals"], key=_proposal_number):
        rec = data["proposals"][key]
        keep, copy_ = (rec.get("keep") or {}), (rec.get("copy") or {})
        k, c = keep.get("fact_id"), copy_.get("fact_id")
        if rec.get("status") == "open":
            merges = merges or {}
            if store is not None and not same_file(rec.get("store"), store):
                _close_proposal(rec, "stale", now, "made from another fact store than the one read now")
            elif merges.get(c) == k or merges.get(k) == c:
                _close_proposal(rec, "done", now, "merged")
            elif k in merges or c in merges:
                _close_proposal(rec, "stale", now, "a fact of the pair was merged into another")
            elif current is not None and (current.get(k) != keep.get("version_id")
                                          or current.get(c) != copy_.get("version_id")):
                _close_proposal(rec, "stale", now, "a text changed or ended since it was shown")
                if released is not None:
                    released.append(key)
            elif (_age_days(rec.get("created"), now) or 0) > DUP_OPEN_DAYS:
                _close_proposal(rec, "stale", now, f"no answer in {DUP_OPEN_DAYS} days")
            else:
                continue
            changed = True
        elif (_age_days(rec.get("closed_at"), now) or 0) > DUP_CLOSED_KEEP_DAYS:
            del data["proposals"][key]
            changed = True
    return changed


def release_pair(seen, pair_key, number=None):
    """Let the pair of *pair_key* be asked about again at once: its cooldown in the `duplicate_pairs` bucket (only
    the one of proposal *number*, when given) and its entry in `_pending`. For a proposal closed `stale` — the
    merge was refused, or a version changed while the pair kept its key (an edit of case or spaces): the 180 days
    of rest were for an answer, and none came. Returns how many records went."""
    removed = 0
    bucket = seen.get("duplicate_pairs")
    rec = bucket.get(pair_key) if isinstance(bucket, dict) else None
    if rec is not None and (number is None or not isinstance(rec, dict) or rec.get("proposal") in (None, number)):
        bucket.pop(pair_key)
        removed += 1
    pending = ((seen.get("_pending") or {}).get("seen") or {}) if isinstance(seen.get("_pending"), dict) else {}
    keys = pending.get("duplicate_pairs") if isinstance(pending, dict) else None
    if isinstance(keys, list) and pair_key in keys:
        pending["duplicate_pairs"] = [k for k in keys if k != pair_key]
        removed += 1
    return removed


def _keep_and_copy(a, b, fa, fb, versions):
    """Which of two facts stays: the side whose numbers and names hold the other's (and more), then more
    significant words, then the origin (said / confirmed over unknown over inferred), more versions, more
    retrievals, the smaller id."""
    def rank(f, mine, other):
        superset = (mine.anchors >= other.anchors and mine.names >= other.names
                    and (mine.anchors != other.anchors or mine.names != other.names))
        return (superset, len(mine.words), _ORIGIN_RANK.get(f.get("origin"), 1), versions.get(f["fact_id"], 0),
                f.get("retrieval_count") or 0, -f["fact_id"])
    return ((a, fa), (b, fb)) if rank(a, fa, fb) >= rank(b, fb, fa) else ((b, fb), (a, fa))


def _keywords_after(keep, copy_):
    """The keep's keywords, then the copy's, distinct, through the screen, at most DUP_KEYWORDS_MAX."""
    out, seen = [], set()
    for kw in list(keep.get("keywords") or []) + list(copy_.get("keywords") or []):
        folded = _norm(kw)
        if folded and folded not in seen and not classify_unsafe(kw):
            seen.add(folded)
            out.append(kw)
    return out[:DUP_KEYWORDS_MAX]


DupNight = namedtuple("DupNight", "items stats alert pending")


def _dup_skip(reason):
    return DupNight([], {"duplicate_pairs_skipped": reason}, None, [])


def duplicate_pairs_night(inputs, d, excluded_ids, seen, proposals_file, now, write):
    """The night's `duplicate_pairs` section. *excluded_ids*: facts in tonight's promotions, conflicts and
    fact_decays (and their duplicates) — one question about a fact a night. *seen*: the pass's state (the
    `duplicate_pairs` bucket, `dup_not`, the proposal counter), marked here when *write*; the proposals file is
    written only then too. Returns DupNight(items, stats, alert, pending pair keys)."""
    if not SETTINGS.DUP_ENABLED:
        return _dup_skip("disabled")
    if inputs.fact_source != "holographic":
        return _dup_skip("not_holographic")
    # The "yes" is carried out by the memory tools of a session, in the database they open; the caller passes that
    # very path as `store`, and every proposal carries it (the agent compares it with `memory_stats` before merging).
    db = store = fact_store_path()
    if not db or not os.path.exists(db):
        return _dup_skip("no_store")
    try:
        merges = store_merges(db)
        if merges is None:
            return _dup_skip("merge_unavailable")
        heads, versions = store_versions(db)
    except sqlite3.Error as exc:
        print(f"[dream] warn: duplicates: the fact store's history is unreadable: {exc}", file=sys.stderr)
        return _dup_skip("unreadable")
    if not proposals_file:
        return _dup_skip("no_state")
    current = {fid: vid for fid, (vid, _text) in heads.items()}
    proposals = load_proposals(proposals_file)
    released = []
    changed = maintain_proposals(proposals, now, merges=merges, current=current, store=store, released=released)
    for number in released:  # nobody answered those texts: the pair may be asked about again tonight
        release_pair(seen, proposals["proposals"][number]["pair_key"], number)

    scored = {s["fact_id"]: s for s in d.scored}
    eligible = []
    for f in inputs.facts:
        fid, s = f["fact_id"], scored.get(f["fact_id"])
        if f.get("store") or s is None or s["unsafe"] or s["rejected"] or fid in excluded_ids:
            continue
        if fid in merges or len(f.get("content") or "") > SETTINGS.DUP_MAX_CHARS:
            continue
        # The text loaded for the night must be the text of the version read now: the provider may have changed
        # the fact in between (the pass's lock does not hold it back). Then the fact waits for the next night.
        if fid not in heads or heads[fid][1] != f["content"]:
            continue
        eligible.append(f)
    by_id = {f["fact_id"]: f for f in eligible}
    alert = None
    try:
        found, _checks = find_duplicate_pairs(eligible, SETTINGS.DUP_MAX_PAIR_CHECKS)
    except DupBudget:
        last = _parse_iso((seen.get("_dup_budget_alert") or {}).get("at"))
        if not last or (now - last) >= timedelta(days=SETTINGS.SEEN_COOLDOWN_DAYS):
            alert = {"kind": "duplicates_budget", "facts": len(eligible),
                     "message": (f"The search for duplicate facts was skipped tonight: {len(eligible)} facts need "
                                 f"more than {SETTINGS.DUP_MAX_PAIR_CHECKS} pair checks "
                                 "(`duplicates.max_pair_checks`). Nothing else is affected.")}
            if write:
                seen["_dup_budget_alert"] = {"at": now.isoformat(timespec="seconds")}
        if write and changed:
            save_proposals(proposals_file, proposals)
        return DupNight([], {"duplicate_pairs_skipped": "budget"}, alert, [])

    pairs = []
    for p in found:
        a, b = by_id[p["a"]], by_id[p["b"]]
        (keep, fk), (copy_, fc) = _keep_and_copy(a, b, _dup_features(a["content"]), _dup_features(b["content"]),
                                                 versions)
        extra = sorted(fc.word_of[w] for w in fc.words - fk.words)
        pairs.append({"pair_key": pair_key(a["content"], b["content"]), "keep": keep, "copy": copy_,
                      "covered": not extra, "extra_words": extra, "why": p["why"],
                      "containment": p["containment"]})
    dup_not = seen.get("dup_not") if isinstance(seen.get("dup_not"), dict) else {}
    bucket = seen.get("duplicate_pairs") if isinstance(seen.get("duplicate_pairs"), dict) else {}
    asked = [p for p in pairs if p["pair_key"] not in dup_not]
    fresh = unseen(asked, bucket, now, cooldown_days=SETTINGS.DUP_COOLDOWN_DAYS, key=lambda p: p["pair_key"])
    fresh.sort(key=lambda p: (-p["containment"], p["keep"]["fact_id"], p["copy"]["fact_id"]))
    chosen, used = [], set()
    for p in fresh:
        ids = {p["keep"]["fact_id"], p["copy"]["fact_id"]}
        if len(chosen) < SETTINGS.DUP_PER_NIGHT and not ids & used:
            chosen.append(p)
            used |= ids

    # The number is never reused: the counter lives in the proposals file and in the seen state, and a number
    # below one already given is never taken — a file lost or set aside does not bring an old number back.
    counter = seen.get("_proposals") if isinstance(seen.get("_proposals"), dict) else {}
    try:
        kept_next = int(counter.get("next") or 1)
    except (TypeError, ValueError):
        kept_next = 1
    nxt = max([proposals["next_proposal"], kept_next] + [_proposal_number(k) + 1 for k in proposals["proposals"]])
    first_next = nxt
    items = []
    stamp = now.isoformat(timespec="seconds")
    for p in chosen:
        keep, copy_ = p["keep"], p["copy"]
        side = {
            "keep": {"fact_id": keep["fact_id"], "version_id": current[keep["fact_id"]], "content": keep["content"],
                     "origin": keep.get("origin")},
            "copy": {"fact_id": copy_["fact_id"], "version_id": current[copy_["fact_id"]],
                     "content": copy_["content"], "origin": copy_.get("origin"), "covered": p["covered"],
                     "extra_words": p["extra_words"]},
        }
        # The same open question again (not shown, or shown before its rest ran out): its number and its sides,
        # even if tonight's ranking would pick the other side to keep — one pair, one open proposal.
        versions_now = {keep["fact_id"]: side["keep"]["version_id"], copy_["fact_id"]: side["copy"]["version_id"]}
        number = next((k for k, r in proposals["proposals"].items()
                       if r.get("status") == "open" and r.get("pair_key") == p["pair_key"]
                       and {(r.get(x) or {}).get("fact_id"): (r.get(x) or {}).get("version_id")
                            for x in ("keep", "copy")} == versions_now), None)
        if number is None:
            number = f"D-{nxt}"
            nxt += 1
            # `store`: the database the proposal is about, as the provider's `fact_store stats` names it
            # (`database`) — the agent compares the two before a merge (SKILL step 7e).
            proposals["proposals"][number] = {"pair_key": p["pair_key"], "created": stamp, "status": "open",
                                              "store": store, **side,
                                              "keywords_after": _keywords_after(keep, copy_), "why": p["why"]}
        rec = proposals["proposals"][number]
        rec["last_published"] = stamp
        changed = True
        p["proposal"] = number
        items.append({"proposal": number, "store": rec["store"], "keep": rec["keep"], "copy": rec["copy"],
                      "keywords_after": rec["keywords_after"], "why": rec["why"]})
    proposals["next_proposal"] = nxt
    if write:
        if changed or nxt != first_next:
            save_proposals(proposals_file, proposals)
        seen["_proposals"] = {"next": nxt}
        bucket = seen.setdefault("duplicate_pairs", {})
        mark_seen(chosen, bucket, now, key=lambda p: p["pair_key"])
        for p in chosen:
            rec = bucket[p["pair_key"]]
            rec.pop("fact_id", None)
            rec.update(fact_ids=[p["keep"]["fact_id"], p["copy"]["fact_id"]],
                       fingerprints=[_fact_fingerprint(p["keep"]["content"]),
                                     _fact_fingerprint(p["copy"]["content"])],
                       proposal=p["proposal"])
        prune_seen(bucket, now, cooldown_days=SETTINGS.DUP_COOLDOWN_DAYS)
    stats = {"duplicate_pairs_found": len(pairs), "duplicate_pairs_published": len(items),
             "duplicate_pairs_suppressed": len(pairs) - len(fresh),
             "duplicate_pairs_deferred": len(fresh) - len(chosen)}
    return DupNight(items, stats, alert, [p["pair_key"] for p in chosen])


def _reopen_unnamed_proposals(pending, seen, answers):
    """After an acknowledged night: a pair whose proposal number the agent's answer did not name was not shown to
    the human, whatever the payload held — its cooldown is dropped and it comes back (`_pending` keeps cooldowns,
    it proves nothing about what a human saw). *answers* None: unknown (no state.db) — left as shown."""
    keys = ((pending.get("seen") or {}).get("duplicate_pairs") or []) if isinstance(pending, dict) else []
    bucket = seen.get("duplicate_pairs")
    if answers is None or not keys or not isinstance(bucket, dict):
        return 0
    named = {f"D-{n}" for text in answers for n in PROPOSAL_RE.findall(text or "")}
    reopened = 0
    for k in keys:
        rec = bucket.get(k)
        if isinstance(rec, dict) and rec.get("proposal") not in named:
            bucket.pop(k)
            reopened += 1
    if reopened:
        print(f"[dream] note: {reopened} merge question(s) of the pass of {pending.get('generated_at')} were not in "
              "the agent's report — asked again", file=sys.stderr)
    return reopened


def _age_days(stamp, now):
    try:
        when = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (now - when).total_seconds() / 86400


StateFiles = namedtuple("StateFiles", "asked rejected seen snapshot proposals forget_sweep diary_index diary")
STATE_NAMES = ("asked.json", "rejected.json", "seen.json", "snapshot.json", "proposals.json", "forget-sweep.json",
               "diary-index.json", "DREAMS.md")
# The state files of the night being run (`night()` sets it; the sweeps and the reject list read it).
STATE = None


def state_files(folder):
    """The engine's own files in *folder*: cooldowns, the reject list, the loss-guard snapshot, the "merge?"
    proposals, the forget-sweep record, the diary and its index."""
    return StateFiles(*(os.path.join(str(folder), name) for name in STATE_NAMES))


def use_state(state):
    global STATE
    STATE = state


@_message_pass
def night(*, memory, messages, state, acks=None, window_days=14, corr_days=30, md_decay_days=30, now=None):
    """One night's review. *memory*: a Memory (the notes asked about, the texts only compared against); *messages*:
    the person's own words (see prepare_messages); *state*: StateFiles; *acks*: {generated_at: [answers]} of the
    nights a person answered. Reads the fact store read-only, writes only *state*; returns the night's dict."""
    use_state(state)
    now = now or _now()
    generated_at = now.isoformat(timespec="seconds")
    seen = load_seen(state.seen)
    reask_md = _revert_unacked(seen, acks)
    for name in BUCKETS:
        seen.setdefault(name, {})
    messages = prepare_messages(messages, corr_days, now=now)
    inputs = load_inputs(window_days, memory, messages, rejected_state_path=state.rejected, seen=seen, now=now)
    facts, memory_text = inputs.facts, inputs.memory_text

    d = _decide(inputs)
    suppressed, quarantined, expired, expired_ids = d.suppressed, d.quarantined, d.expired, d.expired_ids
    new_facts_window, new_facts, new_facts_dups = d.new_facts_window, d.new_facts, d.new_facts_dups
    new_facts_suppressed = d.new_facts_suppressed
    promotions, promotions_dups, ephemeral_events = d.all_promotions, d.promotions_dups, d.all_ephemeral
    promotions_suppressed = d.promotions_suppressed
    conflicts, conflicts_suppressed = d.all_conflicts, d.conflicts_suppressed
    decays, decays_suppressed = d.all_decays, d.decays_suppressed
    feedback, feedback_suppressed = d.trust_feedback, d.feedback_suppressed
    keywords_needed, keywords_pending, confirm = d.keywords_needed, d.keywords_pending, d.confirm
    themes = _new_themes(messages, window_days, facts, memory_text) if SETTINGS.THEMES else []
    md_dec = md_decays(memory, messages, md_decay_days, state.asked, cap=SETTINGS.PUBLISH_CAP, reask=reask_md,
                       rules=inputs.rules)

    # Only published slices count as shown.
    published_promotions = promotions[:SETTINGS.PUBLISH_CAP]
    published_decays = decays[:SETTINGS.PUBLISH_CAP]
    published_conflicts = conflicts[:SETTINGS.PUBLISH_CAP]
    # One question about a fact a night: a fact in tonight's promotions, conflicts or relevance questions (or hidden
    # behind one of them) is not asked about as a duplicate too.
    busy = {i for s in published_promotions + published_conflicts + published_decays
            for i in (s["fact_id"], *s.get("duplicates", ()))}
    dups = duplicate_pairs_night(inputs, d, busy, seen, state.proposals, now, write=True)
    _mark_shown(seen, now, new_facts=new_facts + new_facts_dups, promotions=published_promotions,
                trust_feedback=feedback, keywords_needed=keywords_needed, confirm=confirm,
                fact_decays=published_decays, conflicts=published_conflicts)
    # What this night is about to show, keyed for `_revert_unacked` tomorrow.
    fp = _fact_fingerprint
    seen["_pending"] = {
        "generated_at": generated_at,
        "seen": {"new_facts": [fp(s["content"]) for s in new_facts + new_facts_dups],
                 "fact_decays": [fp(s["content"]) for s in published_decays],
                 "conflicts": [fp(s["content"]) for s in published_conflicts],
                 "promotions": [fp(s["content"]) for s in published_promotions],
                 "trust_feedback": [_feedback_key(i) for i in feedback],
                 "keywords_needed": [_feedback_key(i) for i in keywords_needed],
                 "confirm": [_feedback_key(i) for i in confirm],
                 # The pairs put to the person tonight; the answer must name their numbers.
                 **({"duplicate_pairs": dups.pending} if dups.pending else {})},
        "asked": [m["key"] for m in md_dec if m.get("key")],
    }
    _write_private(state.seen, json.dumps(seen, ensure_ascii=False, indent=1))

    alerts = check_memory_loss(memory, state.snapshot, memory_text)
    if dups.alert:
        alerts.append(dups.alert)
    # Themes are words of the person's messages: counted, never written into the diary.
    diary = build_diary(window_days, len(facts), len(messages), promotions, decays, (), md_dec,
                        ephemeral_events, quarantined, len(suppressed), conflicts=published_conflicts, alerts=alerts)
    memory_sources = load_durable_memory_sources(memory)

    def _pub_near(s):
        item = _pub(s)
        near = nearest_entry(s["content"], sources=memory_sources)
        if near:
            item["nearest_entry"] = near
        return item

    for m in md_dec:
        m.pop("key", None)  # internal handle, not for the caller

    result = {
        "generated_at": generated_at,
        "window_days": window_days, "corr_window_days": corr_days, "md_decay_days": md_decay_days,
        "weights": SETTINGS.WEIGHTS,
        "gates": {"min_score": SETTINGS.MIN_SCORE, "min_mentions": SETTINGS.MIN_MENTIONS,
                  "promotion_corroboration": "2 ref_days OR min_mentions OR 3 retrievals + 1 helpful",
                  "feedback_trust_floor": feedback_floor()},
        "stats": {"facts": len(facts), "new_facts_reviewed": len(new_facts),
                  "fact_history": fact_history_supported(),
                  "new_facts_window": len(new_facts_window),
                  "messages_window": len(messages),
                  "promotions": len(promotions), "ephemeral_events": len(ephemeral_events),
                  "fact_decays": len(decays), "md_decays": len(md_dec), "themes": len(themes),
                  "conflicts": len(conflicts),
                  "quarantined": len(quarantined),
                  "rejected_suppressed": len(suppressed),
                  "new_facts_suppressed": new_facts_suppressed,
                  "promotions_suppressed": promotions_suppressed,
                  "trust_feedback": len(feedback),
                  "trust_feedback_suppressed": feedback_suppressed,
                  "keywords_needed": len(keywords_needed),
                  "keywords_missing": len(keywords_pending),
                  "confirm": len(confirm),
                  "fact_decays_suppressed": decays_suppressed,
                  "conflicts_suppressed": conflicts_suppressed,
                  "expired_events": len(expired_ids),
                  "store_duplicates": len(promotions_dups) + len(new_facts_dups),
                  **dups.stats,
                  "alerts": len(alerts)},
        "alerts": alerts,
        "promotions": [_pub_near(s) for s in published_promotions],
        "new_facts": [_pub_near(s) for s in new_facts],
        "ephemeral_events": [_pub(s) for s in ephemeral_events[:SETTINGS.PUBLISH_CAP]],
        "expired_events": [_pub(s) for s in expired[:SETTINGS.PUBLISH_CAP]],
        "trust_feedback": feedback,
        "keywords_needed": keywords_needed,
        "confirm": confirm,
        "fact_decays": [_pub(s) for s in published_decays],
        "conflicts": [dict(_pub_near(s), conflicts=s["conflicts"]) for s in published_conflicts],
        "md_decays": md_dec,
        "emerging_themes": themes[:SETTINGS.PUBLISH_CAP],
        "quarantined": [_pub_quarantined(s) for s in quarantined[:SETTINGS.PUBLISH_CAP]],
        "diary": diary,
    }
    if dups.items:
        result["duplicate_pairs"] = dups.items
    scrub_result(result)  # a fact forgotten while the night ran never reaches its output
    try:
        append_diary(state.diary, result["diary"])
    except OSError as e:
        print(f"[dream] warn: diary not written: {e}", file=sys.stderr)
    return result


def _pub_quarantined(s):
    """Secrets — fact_id + reason only (content is published nowhere);
    injection — a short preview for human review in the full JSON."""
    item = {"fact_id": s["fact_id"], "reason": s["unsafe"]}
    if s["unsafe"] == "injection":
        item["preview"] = (s["content"] or "")[:60]
    return item


def _why(s):
    """Short explanation why the fact surfaced (report and debugging)."""
    m = s["meta"]
    bits = [f"score {s['score']}"]
    if m["ref_days"]:
        bits.append(f"{m['mentions']} mention(s) in {m['ref_days']} day(s)")
    if m["rc"]:
        bits.append(f"{m['rc']} retrieval(s)")
    return "; ".join(bits)


def _pub(s):
    out = _pub_fields(s)
    if s.get("duplicates"):
        out["duplicates"] = list(s["duplicates"])
    return out


def _pub_fields(s):
    out = _pub_base(s)
    if s.get("store"):
        out["store"] = s["store"]  # a chat's own database: memory tool only, never fact_store / fact_feedback
    if s.get("previously_rejected"):
        out["previously_rejected"] = s["previously_rejected"]  # ask the human again; never write it straight away
    return out


def _pub_base(s):
    return {"fact_id": s["fact_id"], "content": s["content"], "category": s["category"],
            "tags": s["tags"], "score": s["score"], "signals": s["signals"],
            "days_old": s["meta"]["days_old"], "retrieval_count": s["meta"]["rc"],
            "mentions": s["meta"]["mentions"], "ref_days": s["meta"]["ref_days"],
            "evidence": s["meta"].get("evidence", []),
            "why": _why(s),
            "in_memory": s.get("in_memory", False),
            "ephemeral": is_ephemeral_fact(s["content"]),
            "user_profile_hint": user_profile_hint(s),
            **({"origin": s["origin"]} if s.get("origin") else {})}


# ---------------------------------------------------------------------------
# Diary
# ---------------------------------------------------------------------------

REDACTED = "[redacted: the entry holds a secret — look it up in the file by index]"
ENTRY_REDACTED = "[redacted: the entry holds a secret or an instruction]"


def _safe_preview(text, n, redacted=REDACTED):
    """Preview of durable text for the prompt and the diary.

    A MEMORY.md entry can itself hold a credential (a human wrote it there).
    The pass screens facts and evidence, but "still relevant?" questions and the
    diary carried the entry verbatim — the one place where a secret from memory
    reached the prompt and then a file on disk (external review, 2026-10-07)."""
    if classify_unsafe(text):
        return redacted
    return (text or "")[:n]


def build_diary(window, nfacts, nmsgs, promotions, decays, themes, md_dec, ephemeral_events=None,
                quarantined=None, suppressed=0, conflicts=None, alerts=None):
    ephemeral_events = ephemeral_events or []
    quarantined = quarantined or []
    conflicts = conflicts or []
    lines = [f"{SETTINGS.DIARY_HEADING} {_now().astimezone(SETTINGS.LOCAL_TZ).strftime('%Y-%m-%d')}",
             f"Theme window: {window} d · facts: {nfacts} · messages (corroboration): {nmsgs}.", ""]
    for a in alerts or []:
        lines.append(f"⚠️ {a.get('message')}")

    def _line(p):
        if "meta" in p:
            return (f"- ({p['score']}, mentions {p['meta']['mentions']}/{p['meta']['ref_days']}d) "
                    f"{_diary_text(p['content'], 85)}{_diary_tag(p)}")
        return f"- {_diary_text(p['content'], 85)}{_diary_tag(p)}"

    if promotions:
        lines.append("**Strong signals (candidates to promote):**")
        lines.extend(_line(p) for p in promotions[:5])
    else:
        lines.append("No strong promotion candidates yet.")
    if ephemeral_events:
        lines.append("\n**Temporary events (do not promote):**")
        lines.extend(_line(p) for p in ephemeral_events[:5])
    if conflicts:
        lines.append(f"\n**Possible updates of existing entries ({len(conflicts)}):** "
                     + "; ".join(_diary_text(c["content"], 45) + _diary_tag(c) for c in conflicts[:3]))
    if themes:
        lines.append("\n**Emerging themes:** " + ", ".join(f"{t['theme']}×{t['mentions']}" for t in themes[:8]))
    if md_dec:
        lines.append(f"\n**Memory notes not surfaced for long ({len(md_dec)}):** "
                     + "; ".join(d["entry"][:45] for d in md_dec[:3]))
    if decays:
        lines.append(f"\n**Facts never used ({len(decays)}):** "
                     + "; ".join(_diary_text(d["content"], 45) + _diary_tag(d) for d in decays[:3]))
    if quarantined:
        reasons = ", ".join(sorted({q["unsafe"] for q in quarantined}))
        lines.append(f"\n**Quarantined (not promoted): {len(quarantined)}** — {reasons}.")
    if suppressed:
        lines.append(f"\n**Rejected earlier — not asking again: {suppressed}.**")
    lines.append("")
    return "\n".join(lines)


def _owner_of(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_uid, st.st_gid)


def _preserve_owner(path, owner):
    """Keep a state file owned by whoever owned it (or owns its directory).

    A Docker install runs the gateway under its own uid, while a hand run comes
    through `docker exec` as root: the file is then root-owned 0600 and the
    gateway's own pass reads it as missing — silently (the same trap the core
    guards against in cron/jobs.py:_preserve_file_ownership). POSIX only, and
    only when we really are root."""
    if os.name != "posix" or owner is None:
        return
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or geteuid() != 0 or owner == (0, 0):
        return
    try:
        os.chown(path, owner[0], owner[1])
    except OSError as exc:
        print(f"[dream] warn: owner of {path} not restored: {exc}", file=sys.stderr)


def _write_private(path, text, append=False):
    """Dream files contain private facts — create with mode 0600.

    `os.open(..., 0o600)` applies the mode only on CREATION, so a diary created
    earlier kept 0644 (live case). chmod is enforced explicitly.

    A whole-file write goes to a temporary file beside the target and is renamed over it: truncating in place left
    an empty or half-written file when the gate's timeout killed the pass or the disk filled up, and the next reader
    took it for "no state" — every rejection and cooldown gone, silently (review of 2.9.8; `dream-reject.save_state`
    has done this since 2.7.2). A symlinked target is written through, not replaced. Appends stay appends."""
    path = os.path.realpath(path)
    owner = _owner_of(path) or _owner_of(os.path.dirname(path) or ".")
    if append:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(text)
    else:
        tmp = f"{path}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        if os.name == "posix":  # elsewhere there is no mode to set; on POSIX a file of facts stays readable
            print(f"[dream] warn: {path} not made private (0600): {exc}", file=sys.stderr)
    _preserve_owner(path, owner)


CORRUPT_MARK = ".corrupt-"


def _read_state_file(path):
    """(dict, why) of one of the dream's own state files, touching nothing: ({}, None) when there is none or it is
    fine and empty, ({}, why) when it is there but broken — `why` says how. For a reader without the state lock
    (`dream-reject.py --list`): moving a file aside there raced a locked writer — the reader saw a broken list, a
    writer put a good one in its place, and the reader moved the good one away (Codex review of the 2.9.9 changes).

    A file that is there but cannot be read at all (permissions) raises OSError: read as {} it was "no state", and
    the next write of the pass or of `dream-reject.py` replaced every rejection and cooldown in it (review of
    3.3.1). Nothing can be done with it but fixing the permissions, and only a human can."""
    if not path:
        return {}, None
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return {}, None
    except UnicodeDecodeError as exc:
        return {}, f"not UTF-8 ({exc.reason})"
    except OSError as exc:
        raise OSError(f"state file {path} cannot be read ({exc}) — nothing was written over it; "
                      f"fix its permissions") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        return {}, f"not valid JSON ({exc})"
    if not isinstance(data, dict):
        return {}, f"not a JSON object (a {type(data).__name__})"
    return data, None


def _load_state_file(path):
    """One of the dream's own state files (reject list, cooldowns, questions, snapshot, sweep state, diary index,
    extraction cursor, last payload) as a dict; {} when there is none — a fresh install. Only for the holder of the
    state lock, or the only writer of the file (the extraction cursor); others read with `_read_state_file`.

    A file that is there but is not a JSON object is not "no state": read as {} it erased every rejection and
    cooldown without a word, and the next write buried the evidence (review of 2.9.8). It is moved aside to
    `<file>.corrupt-<UTC time>` with a warning, and the pass runs without it. Such a copy may hold a fact's text:
    `forget_sweep` cleans them like the files they came from. A file that cannot be read at all (permissions)
    raises OSError and is left where it is (`_read_state_file`)."""
    data, why = _read_state_file(path)
    if why is None:
        return data
    aside = f"{path}{CORRUPT_MARK}{_now().strftime('%Y%m%dT%H%M%SZ')}"
    try:
        os.replace(path, aside)
        print(f"[dream] warn: state file {path} is {why} — moved to {aside}; running without it", file=sys.stderr)
    except OSError as exc:
        print(f"[dream] warn: state file {path} is {why} and could not be moved aside ({exc}) — running without "
              f"it; the next write replaces it", file=sys.stderr)
    return {}


def rotate_diary(path, keep=None):
    """Keep the last `keep` sections in the diary, move the rest to *.archive.md.
    Nothing is deleted; the diary is not loaded into the prompt, so this is
    hygiene, not token economy."""
    keep = SETTINGS.DIARY_KEEP_SECTIONS if keep is None else keep
    if keep <= 0:
        return False
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return False
    starts = [m.start() for m in SETTINGS.DIARY_SECTION_RE.finditer(text)]
    if len(starts) <= keep:
        return False
    cut = starts[len(starts) - keep]
    _write_private(path + ".archive.md", text[:cut], append=True)
    _write_private(path, text[cut:])
    print(f"[dream] diary: {len(starts) - keep} old section(s) moved to the archive",
          file=sys.stderr)
    return True


def _ensure_private(path):
    """Enforce 0600 on an already existing file (see _write_private)."""
    try:
        if os.path.exists(path) and (os.stat(path).st_mode & 0o777) != 0o600:
            os.chmod(path, 0o600)
    except OSError:
        pass


def append_diary(path, diary):
    """Idempotent: the section «<heading> YYYY-MM-DD» is appended once per day."""
    header = (diary.splitlines() or [""])[0].strip()
    _ensure_private(path)
    try:
        with open(path, encoding="utf-8") as f:
            existing = f.read()
    except OSError:
        existing = ""
    if header and header in existing:
        print(f"[dream] diary: section «{header}» already present — skipped", file=sys.stderr)
        return False
    _write_private(path, diary + "\n", append=True)
    rotate_diary(path)
    return True


# ---------------------------------------------------------------------------
# Forgotten facts (holographic >= 0.8.0)
# ---------------------------------------------------------------------------
# `fact_store forget` deletes a fact from the store for good and leaves a
# tombstone in its `forgotten` table: the id, never the text. The copies this
# engine made are the engine's to clean, by that id: the reject list, the
# "already shown" state, the "merge?" proposals, the diary and its archive.
# Diary lines written since 2.4.0 end with "[#<fact_id>]"; older ones carry no
# id and are found through the diary index — hashes of the previews the diary
# writes, kept for every fact while it still exists (the text is gone after the
# forget, so the index is built ahead of time, on every pass and every sweep).

DIARY_TAG_RE = re.compile(r"\s*\[#((?:[A-Za-z0-9_.-]+#)?\d+)\]")
def _diary_tag(item):
    fact_id = item.get("fact_id")
    return f" [#{fact_id}]" if isinstance(fact_id, int) or FACT_ID_RE.fullmatch(str(fact_id or "")) else ""


def _diary_text(text, n):
    """What the diary writes for a fact since 2.4.0: one line, and "; " only ever between list items (a fact's own
    "; " would make the item boundary ambiguous for the sweep)."""
    return " ".join((text or "")[:n].split()).replace("; ", ", ")


def _preview_hash(text):
    return hashlib.sha256(_fold(text or "").strip().encode("utf-8")).hexdigest()[:20]


def _preview_hashes(content):
    """Hashes of every form the diary has written a fact in: an 85-character line, a 45-character list item, as
    is (before 2.4.0, where a newline in the text broke the line: also its first line) and as `_diary_text`."""
    forms = set()
    for n in (85, 45):
        raw = (content or "")[:n]
        forms |= {raw, raw.split("\n", 1)[0], _diary_text(content, n)}
    return sorted({_preview_hash(f) for f in forms if f.strip()})


def load_forgotten(db=None, errors=None):
    """{fact_id: forgotten_at} from the store's tombstones; {} without a store or without a `forgotten` table."""
    path = db or fact_store_path()
    try:
        return _tombstones(path)
    except sqlite3.Error as exc:
        # Unreadable is not "nothing was forgotten": reported, and the next run tries again.
        if errors is not None:
            errors.append(f"{path}: {exc}")
        else:
            print(f"[dream] warn: tombstones of the fact store unreadable: {exc}", file=sys.stderr)
        return {}


def _tombstones(path):
    """{fact_id: forgotten_at} of one holographic database ({} without one or without the table). An unreadable
    database raises sqlite3.Error — the caller decides; a chat store's error is not "nothing was forgotten"."""
    if not path or not os.path.exists(path):
        return {}
    c = _conn(path)
    try:
        if not c.execute("select 1 from sqlite_master where type='table' and name='forgotten'").fetchone():
            return {}
        return {int(r[0]): str(r[1]) for r in c.execute("select fact_id, forgotten_at from forgotten")}
    finally:
        c.close()


def fact_history_supported(db=None):
    """True when the store keeps fact history (holographic >= 0.8.0: change / end / correct / forget)."""
    path = db or (fact_store_path() if fact_source() == "holographic" else None)
    if not path or not os.path.exists(path):
        return False
    c = _conn(path)
    try:
        if not c.execute("select 1 from sqlite_master where type='table' and name='meta'").fetchone():
            return False
        # The marker is written in the same transaction that versions every fact; the tables exist a moment earlier.
        return bool(c.execute("select 1 from meta where key = 'history_schema'").fetchone())
    except sqlite3.Error:
        return False
    finally:
        c.close()


def update_diary_index(path, facts):
    """Add the preview hashes of every current fact (and the fingerprint the reject list and the cooldowns
    key on). Hashes accumulate per id: a fact that changed keeps the hashes of its older texts, whose lines
    are still in the diary. Returns True when the file changed."""
    if not path:
        return False
    index = _load_state_file(path)
    changed = False
    for f in facts:
        fid = str(f.get("fact_id"))
        if not FACT_ID_RE.fullmatch(fid) or not f.get("content"):
            continue
        entry = index.setdefault(fid, {"previews": [], "fingerprints": []})
        previews = sorted(set(entry.get("previews") or []) | set(_preview_hashes(f["content"])))
        fingerprints = sorted(set(entry.get("fingerprints") or []) | {_fact_fingerprint(f["content"])})
        if previews != entry.get("previews") or fingerprints != entry.get("fingerprints"):
            entry["previews"], entry["fingerprints"] = previews, fingerprints
            changed = True
    if changed:
        _write_private(path, json.dumps(index, ensure_ascii=False))
    return changed


def _strip_tags(text):
    return DIARY_TAG_RE.sub("", text).strip()


_DIARY_ITEM_RE = re.compile(r"^- (?:\([^)]*mentions [^)]*\) )?(.*)$")
# A line the diary writes itself; anything else right after a removed item is the rest of a fact text that
# contained a newline (before 2.4.0) and goes with it.
_DIARY_OWN_LINE_RE = re.compile(r"^(?:- |\*\*|#|⚠️|Theme window|No strong|\s*$)")


def scrub_diary_text(text, fact_ids, index):
    """The diary without the lines (or list items) of *fact_ids*: by the "[#id]" tag, or by the preview
    hashes of the index for lines written before the tags. Returns (text, removed)."""
    ids = {str(i) for i in fact_ids}
    hashes = {h for i in ids for h in (index.get(i) or {}).get("previews", [])}

    def gone(item):
        tags = set(DIARY_TAG_RE.findall(item))
        return bool(tags & ids) or (not tags and _preview_hash(_strip_tags(item)) in hashes)

    out, removed, continuation = [], 0, False
    for line in text.split("\n"):
        if continuation and not _DIARY_OWN_LINE_RE.match(line):
            continue
        continuation = False
        item = _DIARY_ITEM_RE.match(line)
        if item and gone(item.group(1)):
            removed += 1
            continuation = not DIARY_TAG_RE.search(line)  # tagged lines (2.4.0+) are always one line
            continue
        if line.startswith("**") and ":** " in line:
            head, rest = line.split(":** ", 1)
            parts = rest.split("; ")
            drop = {i for i, p in enumerate(parts) if gone(p)}
            for i in range(len(parts)):  # untagged legacy items whose own text contained "; "
                for j in range(i + 2, len(parts) + 1):
                    joined = "; ".join(parts[i:j])
                    if not DIARY_TAG_RE.search(joined) and _preview_hash(joined) in hashes:
                        drop.update(range(i, j))
            if drop:
                removed += len(drop)
                kept = [p for i, p in enumerate(parts) if i not in drop]
                if not kept:
                    continue
                line = f"{head}:** " + "; ".join(kept)
        out.append(line)
    return "\n".join(out), removed


def _drop_fact_items(node, ids):
    """Remove every dict carrying one of *ids* as `fact_id` from the lists of a JSON payload."""
    removed = 0
    if isinstance(node, dict):
        for value in node.values():
            removed += _drop_fact_items(value, ids)
    elif isinstance(node, list):
        keep = []
        for item in node:
            if isinstance(item, dict) and str(item.get("fact_id")) in ids:
                removed += 1
                continue
            # A "merge?" pair (3.4.0) goes whole with either of its facts: half a pair is no question.
            if isinstance(item, dict) and any(isinstance(item.get(side), dict)
                                              and str(item[side].get("fact_id")) in ids for side in ("keep", "copy")):
                removed += 1
                continue
            if isinstance(item, dict) and isinstance(item.get("duplicates"), list):
                twins = [d for d in item["duplicates"] if str(d) not in ids]
                removed += len(item["duplicates"]) - len(twins)
                item["duplicates"] = twins
            removed += _drop_fact_items(item, ids)
            keep.append(item)
        node[:] = keep
    return removed


def _scrub_strings(node, ids, index):
    """The diary text a payload carries (`diary`): the same line rules as the diary file. Other strings are not
    diary text (a kept fact's own words may look like a diary line) and are left alone; facts leave a payload
    with their item (`_drop_fact_items`)."""
    if not isinstance(node, dict) or not isinstance(node.get("diary"), str):
        return 0
    cleaned, removed = scrub_diary_text(node["diary"], ids, index)
    if removed:
        node["diary"] = cleaned
    return removed


FORGOTTEN_COPY = "[removed: a forgotten fact]"
_COPY_PIECE_RE = re.compile(r"\n|(?<=[.!?;])\s+")


def _carries_forgotten(text, hashes):
    """True when *text* holds a forgotten fact verbatim — as a whole, as a line or as a sentence. Only the
    preview hashes are left of its text, so a reworded copy is not found; a memory entry written from the fact
    and an `existing_facts` line of the extraction pass are verbatim."""
    if not hashes or not isinstance(text, str) or not text.strip():
        return False
    for piece in {text, *_COPY_PIECE_RE.split(text)}:
        piece = piece.strip().lstrip("§").strip()
        if piece and hashes & {_preview_hash(piece[:85]), _preview_hash(_diary_text(piece, 85))}:
            return True
    return False


def _is_forgotten_item(item, hashes, fingerprints):
    """A payload item (it has `content`) that IS a forgotten fact. Its full fingerprint decides: two facts may
    share their first 85 characters — all the preview hashes keep — and differ in the end ("…to Telegram" /
    "…by email"; Codex review of 2.7.5). A text no longer than a preview is matched by its hashes, and so is
    every item while no fingerprint was recorded (tombstones swept before 2.4)."""
    content = item.get("content")
    if not isinstance(content, str):
        return False
    if (item.get("fingerprint") or _fact_fingerprint(content)) in fingerprints:
        return True
    return (len(content.strip()) <= 85 or not fingerprints) and _carries_forgotten(content, hashes)


def _scrub_copies(node, hashes, fingerprints=frozenset(), _item=False):
    """Copies of forgotten facts that ride WITHOUT their fact_id — a memory entry shown as another candidate's
    `nearest_entry`, a "still relevant?" entry, a conflict's entry, a bare string in a list. `_drop_fact_items`
    only knew items by id, and these stayed in the payload (Codex review of 2.7.2). A `nearest_entry` goes as a
    whole: its anchor without the text is a blind replace. A kept item's own `content` is its fact, already
    judged by `_is_forgotten_item`."""
    removed = 0
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if key == "diary" or (_item and key == "content"):
                continue  # line rules of its own (_scrub_strings); a kept fact's own text
            if isinstance(value, str):
                if value != FORGOTTEN_COPY and _carries_forgotten(value, hashes):
                    node[key] = FORGOTTEN_COPY
                    removed += 1
            elif key == "nearest_entry" and isinstance(value, dict) and any(
                    _carries_forgotten(v, hashes) for v in value.values()):
                node[key] = {"entry": FORGOTTEN_COPY, "replace_unsafe": True}
                removed += 1
            else:
                removed += _scrub_copies(value, hashes, fingerprints)
    elif isinstance(node, list):
        keep = []
        for value in node:
            if isinstance(value, str):
                if value != FORGOTTEN_COPY and _carries_forgotten(value, hashes):
                    value = FORGOTTEN_COPY
                    removed += 1
            elif isinstance(value, dict) and _is_forgotten_item(value, hashes, fingerprints):
                # An item whose own text is the forgotten fact goes with it: a placeholder left in
                # `promotions` kept the gate open and offered "[removed: …]" for memory (review of 2.7.4).
                removed += 1
                continue
            else:
                removed += _scrub_copies(value, hashes, fingerprints,
                                         isinstance(value, dict) and isinstance(value.get("content"), str))
            keep.append(value)
        node[:] = keep
    return removed


def _preview_set(index):
    return {h for rec in index.values() for h in (rec or {}).get("previews", [])}


def _fingerprint_set(index):
    return {fp for rec in index.values() for fp in (rec or {}).get("fingerprints", [])}


def scrub_result(result):
    """A pass's result without facts forgotten while it ran (it loaded them before the forget): before it is
    published, not after."""
    ids = {str(i) for i in load_forgotten()}
    if not ids:
        return 0
    index = forgotten_diary_index(ids)
    return (_drop_fact_items(result, ids) + _scrub_strings(result, ids, index)
            + _scrub_copies(result, _preview_set(index), _fingerprint_set(index)))


_JSON_STRING_RE = re.compile(r'"((?:[^"\\\n]|\\.){20,})"')


def _json_string(literal):
    try:
        return json.loads(f'"{literal}"')
    except ValueError:
        return literal


def forget_sweep(db=None):
    """Clean this skill's copies of facts the store has forgotten (see the section comment). Every run looks for
    every tombstone again, so a copy written back by a pass that loaded the fact before it was forgotten, or a
    file that could not be removed last time, is caught on the next run; with nothing left the run changes
    nothing. Also refreshes the diary index from the current facts, so a fact forgotten later can still be found
    in old lines. `errors` lists what could not be cleaned (the gate then fails and is retried)."""
    index_path = STATE.diary_index
    state_path = STATE.forget_sweep
    state = _load_state_file(state_path)
    state_before = json.dumps(state, sort_keys=True)
    store_errors = []
    tombstones = load_forgotten(db, errors=store_errors)
    _index_current_facts(db, index_path)
    done = state.setdefault("swept", {})
    new = sorted(str(i) for i in tombstones if str(i) not in done)
    summary = {"tombstones": len(tombstones), "swept": len(new), "rejected": 0, "seen": 0, "proposals": 0,
               "diary_lines": 0, "corrupt_copies": 0, "errors": store_errors}
    if not tombstones:
        return summary
    ids = {str(i) for i in tombstones}
    live = _load_state_file(index_path)
    # A copy merged into another left the live index; its hashes wait in `merged` (merge_sweep).
    merged = state.get("merged") if isinstance(state.get("merged"), dict) else {}
    index = {i: {"previews": sorted(set((done.get(i) or {}).get("previews") or [])
                                    | set((live.get(i) or {}).get("previews") or [])
                                    | set((merged.get(i) or {}).get("previews") or []))} for i in ids}
    fingerprints = ({fp for i in ids for fp in (live.get(i) or {}).get("fingerprints", [])}
                    | {fp for i in ids for fp in (done.get(i) or {}).get("fingerprints", [])}
                    | {fp for i in ids for fp in (merged.get(i) or {}).get("fingerprints", [])})

    # Each copy in turn, in this order: the reject list, the cooldowns, the proposals, the diary, and last the broken
    # state files set aside — the loads above may have just set one aside.
    summary["rejected"] = _sweep_rejected(ids, fingerprints)
    summary["seen"] = _sweep_seen(ids, fingerprints)
    summary["proposals"] = _sweep_proposals(ids, fingerprints)
    summary["diary_lines"] = _sweep_diary(ids, index, summary["errors"])
    # A wipe that did not finish (a copy that could not be deleted) is owed until it does: the tombstone is recorded
    # as swept below, and without this a fragment in a copy kept on a failed run would never be looked for again.
    wipe_all = bool(new) or bool(state.get("corrupt_wipe_owed"))
    errors_before = len(summary["errors"])
    summary["corrupt_copies"] = _sweep_corrupt_copies(ids, index, fingerprints, wipe_all, summary["errors"])
    if wipe_all and len(summary["errors"]) > errors_before:
        state["corrupt_wipe_owed"] = True
    else:
        state.pop("corrupt_wipe_owed", None)

    # The hashes move from the index to the sweep state: they keep finding copies written back later, and the
    # owner of another copy of the diary (a git backup) cleans it by them (forgotten_diary_index); not text.
    stamp = _now().isoformat(timespec="seconds")
    for i in ids:
        record = done.setdefault(i, {"at": stamp})
        record["previews"] = index[i]["previews"]
        record["fingerprints"] = sorted(set(record.get("fingerprints") or [])
                                        | set((live.get(i) or {}).get("fingerprints") or [])
                                        | set((merged.get(i) or {}).get("fingerprints") or []))
        merged.pop(i, None)
    if index_path and any(i in live for i in ids):
        for i in ids:
            live.pop(i, None)
        _write_private(index_path, json.dumps(live, ensure_ascii=False))
    if state_path and json.dumps(state, sort_keys=True) != state_before:
        _write_private(state_path, json.dumps(state, ensure_ascii=False))
    return summary


def _index_current_facts(db, index_path):
    """The diary index refreshed from the facts the store holds now (`forget_sweep`). A store that cannot be read
    leaves the index as it was, with a warning: the sweep itself goes on."""
    store = db or fact_store_path()
    if not store or not os.path.exists(store):
        return
    try:
        facts = _load_holographic(store)
    except (sqlite3.Error, OSError) as exc:
        print(f"[dream] warn: diary index not updated: {exc}", file=sys.stderr)
        return
    if facts:
        try:
            update_diary_index(index_path, facts)
        except OSError as exc:
            print(f"[dream] warn: diary index not updated: {exc}", file=sys.stderr)


def _sweep_rejected(ids, fingerprints):
    """Records of forgotten facts out of the reject list; how many."""
    rejected_path = STATE.rejected
    rejected = _load_state_file(rejected_path)
    gone = [k for k, rec in rejected.items()
            if k in fingerprints or (isinstance(rec, dict) and str(rec.get("fact_id")) in ids)]
    for key in gone:
        rejected.pop(key)
    if gone:
        _write_private(rejected_path, json.dumps(rejected, ensure_ascii=False, indent=1) + "\n")
    return len(gone)


def _sweep_seen(ids, fingerprints, skip=()):
    """Forgotten facts out of the cooldowns and out of what tonight's pass showed (`_pending`); how many. *skip*:
    buckets left as they are (the merge sweep keeps `dup_not`: "these are different" outlives a merge)."""
    seen_path = STATE.seen
    seen = _load_state_file(seen_path)
    removed = 0
    gone_keys = set()  # keys of the records removed: a pair's key (3.4.0) is no fingerprint of a fact
    for name, bucket in seen.items():
        if name in skip or name == "_pending" or not isinstance(bucket, dict):
            continue
        for key in [k for k, v in bucket.items()
                    if k in fingerprints or (isinstance(v, dict) and (
                        str(v.get("fact_id")) in ids
                        # a pair's record (3.4.0: `duplicate_pairs`, `dup_not`) names both of its facts
                        or {str(i) for i in v.get("fact_ids") or []} & ids))]:
            bucket.pop(key)
            gone_keys.add(key)
            removed += 1
    pending = seen.get("_pending")
    if "_pending" not in skip and isinstance(pending, dict) and isinstance(pending.get("seen"), dict):
        for key, values in pending["seen"].items():
            if isinstance(values, list):
                kept = [v for v in values if v not in fingerprints and v not in gone_keys]
                removed += len(values) - len(kept)
                pending["seen"][key] = kept
    if removed:
        _write_private(seen_path, json.dumps(seen, ensure_ascii=False, indent=1))
    return removed


def _sweep_proposals(ids, fingerprints):
    """Merge proposals (3.4.0) that hold a forgotten fact, removed whole — they carry its text; how many."""
    path = STATE.proposals
    if not path or not os.path.exists(path):
        return 0
    data = load_proposals(path)
    gone = [k for k, rec in data["proposals"].items()
            if any(str((rec.get(side) or {}).get("fact_id")) in ids
                   or _fact_fingerprint((rec.get(side) or {}).get("content") or "") in fingerprints
                   for side in ("keep", "copy"))]
    for key in gone:
        del data["proposals"][key]
    if gone:
        save_proposals(path, data)
    return len(gone)


def _sweep_diary(ids, index, errors):
    """Lines about forgotten facts out of the diary and its archive; how many. What cannot be read goes to
    `errors`."""
    removed_all = 0
    diary = STATE.diary
    for path in (diary, diary + ".archive.md"):
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(f"{path}: {exc}")
            continue
        cleaned, removed = scrub_diary_text(text, ids, index)
        if removed:
            removed_all += removed
            _write_private(path, cleaned)
    return removed_all


def _sweep_corrupt_copies(ids, index, fingerprints, wipe_all, errors):
    """Broken state files moved aside (`_load_state_file`) that may still hold a forgotten fact, removed; how many.
    What cannot be read or removed goes to `errors`.

    A copy of the reject list or the payload holds facts' text, and cleaning only the live files left a forgotten
    fact there for good while the sweep reported success (Codex review of the 2.9.9 changes). Such a copy cannot be
    parsed, and a write cut short cuts a fact's text anywhere — a fragment matches no id, fingerprint or preview hash,
    so "it does not mention the fact" cannot be proven. Hence `wipe_all` (a fact forgotten for the first time, or a
    wipe still owed): every set-aside copy goes — the human's "forget it" outweighs the evidence. Otherwise a copy is
    deleted when it mentions a forgotten fact by its id, its fingerprint or its verbatim text (a live file is cleaned
    on every run, so a later copy of it is clean)."""
    state_files = {p for p in (STATE.rejected, STATE.seen, STATE.diary_index, STATE.forget_sweep, STATE.asked,
                               STATE.snapshot, STATE.proposals) if p}
    pattern = re.compile(r'"fact_id"\s*:\s*"?((?:[A-Za-z0-9_.-]+#)?\d+)\b')
    hashes = _preview_set(index)
    by_dir = defaultdict(set)
    for p in state_files:
        by_dir[os.path.dirname(os.path.abspath(p))].add(os.path.basename(p) + CORRUPT_MARK)
    removed = 0
    for folder, prefixes in sorted(by_dir.items()):
        try:
            names = os.listdir(folder)
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(f"{folder}: {exc}")
            continue
        for name in sorted(n for n in names if n.startswith(tuple(prefixes))):
            path = os.path.join(folder, name)
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read()
                if (wipe_all or set(pattern.findall(text)) & ids or any(fp in text for fp in fingerprints) or any(
                        _carries_forgotten(_json_string(lit), hashes) for lit in _JSON_STRING_RE.findall(text))):
                    os.remove(path)
                    removed += 1
            except OSError as exc:
                errors.append(f"{path}: {exc}")
    return removed


# ---------------------------------------------------------------------------
# Merged facts (holographic >= 0.19.0): merge_sweep
# ---------------------------------------------------------------------------
# `fact_store merge` makes a copy part of another fact (its keep): the copy's row leaves `facts`, its history stays
# under its own id, and `fact_merges` says where it went. That is no forget: the copy's text is kept by the store,
# so the diary lines and logs that mention it stay (the forget sweep is a private wipe; `fact_merges` is never
# added to the tombstones). What goes is this skill's live state about the copy, which no longer matches anything:
# its items in the night's payload, its cooldown and reject records, its row of the diary index and its records in
# the judge's files; a merge proposal of the pair becomes `done`, one with a participant merged elsewhere `stale`.
# The keep's own records stay. Runs on every pass (before and after it), whether or not anything was forgotten,
# and is idempotent: a second run finds nothing. A later `forget` of the keep leaves a tombstone for every id of
# the group, and the forget sweep then cleans the rest as for any forgotten fact.

def merge_sweep(db=None):
    """Clean this skill's live state of facts merged into others (see the section comment); counts of what went,
    {} when the store keeps no merges."""
    path = db or fact_store_path()
    if not path or not os.path.exists(path):
        return {}
    c = _conn(path)
    try:
        if not c.execute("select 1 from sqlite_master where type='table' and name='fact_merges'").fetchone():
            return {}
        merges = {int(f): int(t) for f, t in c.execute("select from_fact_id, into_fact_id from fact_merges")}
        texts = {int(f): t for f, t in c.execute(
            "select v.fact_id, v.content from fact_versions v join fact_merges m on m.from_fact_id = v.fact_id "
            "where v.status = 'merged'")}
        # Every text a copy ever had (its history stays under its id until a forget): the forget sweep needs their
        # hashes to find the diary lines and logs written before tags, once the copy has left the live index.
        history = defaultdict(set)
        for f, t in c.execute("select v.fact_id, v.content from fact_versions v join fact_merges m "
                              "on m.from_fact_id = v.fact_id"):
            history[int(f)].add(t)
        kept = {_fact_fingerprint(t) for (t,) in c.execute("select content from facts")}
    finally:
        c.close()
    summary = {"merged": len(merges), "proposals": 0, "seen": 0, "rejected": 0, "index": 0}
    if not merges:
        return summary
    ids = {str(i) for i in merges}
    index_path = STATE.diary_index
    index = _load_state_file(index_path) if index_path else {}
    # The copies' fingerprints, but never one a current fact has: a copy that differed from its keep only in case
    # or spaces shares the keep's key, and the keep's records stay.
    fingerprints = ({_fact_fingerprint(t) for t in texts.values()}
                    | {fp for i in ids for fp in (index.get(i) or {}).get("fingerprints", [])}) - kept
    now = _now()

    ppath = STATE.proposals
    if ppath and os.path.exists(ppath):
        data = load_proposals(ppath)
        before = {k: r.get("status") for k, r in data["proposals"].items()}
        if maintain_proposals(data, now, merges=merges, store=path):
            summary["proposals"] = sum(1 for k, r in data["proposals"].items() if before.get(k) != r.get("status"))
            save_proposals(ppath, data)
    summary["seen"] = _sweep_seen(ids, fingerprints, skip=("dup_not",))
    summary["rejected"] = _sweep_rejected(ids, fingerprints)
    # Before a copy leaves the live index, its hashes (and those of each of its texts) move to the forget sweep's
    # archive of merged copies: a later forget of the group must still find its untagged diary lines.
    _archive_merged(ids, index, history)
    if index_path and any(i in index for i in ids):
        summary["index"] = sum(1 for i in ids if index.pop(i, None) is not None)
        _write_private(index_path, json.dumps(index, ensure_ascii=False))
    return summary


def _archive_merged(ids, index, history):
    """Keep the hashes of merged copies for the forget sweep (`merged` in its state): the previews and fingerprints
    the diary index held for each id in *ids*, and those of every text in *history* ({id: {texts}}). Hashes only,
    never text; a copy forgotten later moves on from here to `swept`."""
    state_path = STATE.forget_sweep
    if not state_path:
        return
    state = _load_state_file(state_path)
    archive = state.get("merged") if isinstance(state.get("merged"), dict) else {}
    changed = False
    for i in sorted(ids):
        texts = (history.get(int(i)) or set()) if i.isdigit() else set()
        old = archive.get(i) if isinstance(archive.get(i), dict) else {}
        live = index.get(i) or {}
        previews = sorted(set(old.get("previews") or []) | set(live.get("previews") or [])
                          | {h for t in texts for h in _preview_hashes(t)})
        fingerprints = sorted(set(old.get("fingerprints") or []) | set(live.get("fingerprints") or [])
                              | {_fact_fingerprint(t) for t in texts})
        if not previews and not fingerprints:
            continue
        if previews != old.get("previews") or fingerprints != old.get("fingerprints"):
            archive[i] = {"previews": previews, "fingerprints": fingerprints}
            changed = True
    if changed:
        state["merged"] = archive
        os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
        _write_private(state_path, json.dumps(state, ensure_ascii=False))


def forgotten_diary_index(ids):
    """{fact_id: {"previews": [...], "fingerprints": [...]}} for *ids*: from the sweep state once swept, else from
    the diary index — what scrub_diary_text needs to clean another copy of the diary (a backup) of those facts."""
    index_path = STATE.diary_index
    state_path = STATE.forget_sweep
    # A reader, also for whoever cleans a backup of the diary without the state lock: it never moves a file.
    index = _read_state_file(index_path)[0]
    sweep_state = _read_state_file(state_path)[0]
    swept = sweep_state.get("swept") or {}
    merged = sweep_state.get("merged") if isinstance(sweep_state.get("merged"), dict) else {}
    out = {}
    for i in (str(x) for x in ids):
        done = (swept.get(i) if isinstance(swept.get(i), dict) else None) or {}
        archived = (merged.get(i) if isinstance(merged.get(i), dict) else None) or {}
        previews = (set(done.get("previews") or []) | set((index.get(i) or {}).get("previews") or [])
                    | set(archived.get("previews") or []))
        fingerprints = (set(done.get("fingerprints") or []) | set((index.get(i) or {}).get("fingerprints") or [])
                        | set(archived.get("fingerprints") or []))
        out[i] = {"previews": sorted(previews), "fingerprints": sorted(fingerprints)}
    return out


# The effective settings at import are the defaults: importing reads no file. The caller applies its own with
# configure() before a night.
configure({})


def __getattr__(name):
    """`dream.MIN_SCORE` and the other settings read from SETTINGS (see Settings)."""
    if name in _SETTING_NAMES:
        return getattr(SETTINGS, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class _DreamModule(types.ModuleType):
    """This module once it is registered in sys.modules (an import, `python dream.py`, the tests' loaders): a
    setting written on the module would be read by nobody, so it raises."""

    def __setattr__(self, name, value):
        if name in _SETTING_NAMES:
            raise AttributeError(f"{name} is a setting: set SETTINGS.{name}, or call configure()")
        super().__setattr__(name, value)

    def __delattr__(self, name):
        if name in _SETTING_NAMES:
            raise AttributeError(f"{name} is a setting: set SETTINGS.{name}, or call configure()")
        super().__delattr__(name)


if vars(sys.modules.get(__name__, sys)) is globals():
    sys.modules[__name__].__class__ = _DreamModule


# ---------------------------------------------------------------------------
# A person's answers that the engine keeps: the reject list and the "merge?" proposals
# ---------------------------------------------------------------------------

def reject_fact(state, fact_id, content, reason=""):
    """Put a fact on the reject list of *state*: the night stops offering it (matched by text, so a fact stored again
    under a new id stays rejected). A text holding a secret is not kept — its fingerprint is. Returns the key."""
    rejected = _load_state_file(state.rejected)
    key = _fact_fingerprint(content)
    record = {"fact_id": fact_id, "content": content, "fingerprint": key, "reason": reason,
              "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if classify_unsafe(content) == "secret":
        record.update(content=None, redacted="secret")
    rejected[key] = record
    _write_private(state.rejected, json.dumps(rejected, ensure_ascii=False, indent=1) + "\n")
    return key


def unreject_fact(state, fact_id):
    """Lift the rejections of *fact_id*; returns how many records went."""
    rejected = _load_state_file(state.rejected)
    gone = [k for k, rec in rejected.items() if isinstance(rec, dict) and str(rec.get("fact_id")) == str(fact_id)]
    for key in gone:
        rejected.pop(key)
    if gone:
        _write_private(state.rejected, json.dumps(rejected, ensure_ascii=False, indent=1) + "\n")
    return len(gone)


def close_proposal(state, number, how):
    """The person's answer to a "merge?" proposal, or what became of it: *how* is `not-duplicates` (never asked
    again), `stale` (the merge was refused because a text changed — the pair may be asked anew at once) or `undo`
    (open again). Raises ValueError for a number unknown or in the wrong status."""
    number = str(number).strip().upper()
    if number.isdigit():
        number = f"D-{number}"
    if not PROPOSAL_RE.fullmatch(number):
        raise ValueError(f"not a proposal number: {number!r} (expected D-17)")
    data = load_proposals(state.proposals)
    seen = _load_state_file(state.seen)
    dup_not = seen.get("dup_not") if isinstance(seen.get("dup_not"), dict) else {}
    seen["dup_not"] = dup_not
    rec = data["proposals"].get(number)
    now = datetime.now(timezone.utc)
    if rec is None and how == "undo":
        keys = [k for k, v in dup_not.items() if isinstance(v, dict) and v.get("proposal") == number]
        if not keys:
            raise ValueError(f"no merge proposal {number}")
        for k in keys:
            dup_not.pop(k)
        _write_private(state.seen, json.dumps(seen, ensure_ascii=False, indent=1))
        return
    if rec is None:
        raise ValueError(f"no merge proposal {number} (a closed one is kept {DUP_CLOSED_KEEP_DAYS} days)")
    status = rec.get("status")
    if how == "not-duplicates":
        if status == "done":
            raise ValueError(f"{number} was merged already")
        _close_proposal(rec, "declined", now, "the person: these are different")
        dup_not[rec["pair_key"]] = {"at": now.isoformat(timespec="seconds"), "proposal": number,
                                    "fact_ids": [(rec.get(s) or {}).get("fact_id") for s in ("keep", "copy")]}
    elif how == "stale":
        if status != "open":
            return
        _close_proposal(rec, "stale", now, "the merge was refused: the texts changed")
        release_pair(seen, rec.get("pair_key"), number)
    elif how == "undo":
        if status not in ("declined", "stale"):
            raise ValueError(f"{number} is {status} — nothing to undo")
        for key in ("closed_at", "closed_why"):
            rec.pop(key, None)
        rec["status"] = "open"
        rec["created"] = now.isoformat(timespec="seconds")
        dup_not.pop(rec["pair_key"], None)
    else:
        raise ValueError(f"unknown answer {how!r}")
    save_proposals(state.proposals, data)
    _write_private(state.seen, json.dumps(seen, ensure_ascii=False, indent=1))
