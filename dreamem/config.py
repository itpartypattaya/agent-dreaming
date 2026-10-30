"""Our settings (``~/.dreaming/config.toml``) and the fact stores of holographic-memory.

The stores are found the way holographic-memory finds them — its ``config.toml``: ``db_path`` (default
``~/.holographic/memory_store.db``) and ``scope`` (``global``: one database; ``project``: ``projects/<key>.db`` per git
root) — read here with ``tomllib``, without importing holomem: opening its ``Memory`` would open the store for writing
and may call an embeddings endpoint.

A profile is one pass: one fact store and the messages that may corroborate its facts.
- scope ``global``: profile ``global`` — the store, every message, the auto-memory notes;
- scope ``project``: one profile per project database, with that project's messages only, and profile ``notes`` for
  the auto-memory notes (no facts);
- no holographic-memory: profile ``default`` — notes and messages, no facts.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .fsutil import write_private
from .paths import Homes

DEFAULTS = {
    "clients": ["claude-code", "codex"],
    # Claude Code keeps transcripts 30 days (cleanupPeriodDays): a longer window would read as silence.
    "windows": {"themes_days": 14, "corroboration_days": 30, "md_decay_days": 30},
    "timezone": "auto",
    "max_questions": 7,
    # A longer user turn is mostly a pasted document or log, not the person's words.
    "max_message_chars": 2000,
    "exclude_patterns": [],
    "agent_names": ["claude", "codex"],
    "fact_source": "auto",  # auto | holographic | none
    # When the night's pass runs: "HH:MM" local time — the first session after it; "any" — the first session of each
    # day; "" — not chosen yet (as "any", and the agent asks the person once).
    "dream_time": "",
}
ANY_TIME = "any"
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")
_TIME = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_TOP_KEY = re.compile(r"^\s*dream_time\s*=")


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load(homes: Homes) -> tuple[dict, list[str]]:
    try:
        raw = tomllib.loads(homes.config.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(DEFAULTS), []
    except (OSError, ValueError) as exc:  # TOMLDecodeError and UnicodeDecodeError are ValueErrors
        return dict(DEFAULTS), [f"{homes.config}: {type(exc).__name__} — defaults used"]
    unknown = sorted(set(raw) - set(DEFAULTS))
    settings = _merge(DEFAULTS, {k: v for k, v in raw.items() if k in DEFAULTS})
    problems = [f"unknown setting: {k}" for k in unknown]
    chosen = settings.get("dream_time")
    if chosen not in ("", ANY_TIME) and dream_time(chosen) is None:
        problems.append(f"dream_time {chosen!r} is not HH:MM or {ANY_TIME!r} — the first session of each day is used")
        settings["dream_time"] = ANY_TIME
    return settings, problems


def dream_time(value) -> tuple[int, int] | None:
    """(hour, minute) of an ``HH:MM`` setting, None for anything else."""
    m = _TIME.match(value.strip()) if isinstance(value, str) else None
    return (int(m.group(1)), int(m.group(2))) if m else None


def set_time(homes: Homes, value: str) -> str:
    """Write ``dream_time`` into our settings — that one top-level line, the rest of the file as it was. ``HH:MM`` (as
    ``09:05``) or ``any``. A wrong value, or a file that cannot be read back as TOML, raises ValueError and nothing is
    written. Returns the value kept."""
    value = value.strip().lower()
    hm = dream_time(value)
    if hm is None and value != ANY_TIME:
        raise ValueError(f"{value!r}: a time is HH:MM (24-hour, e.g. 23:00), or {ANY_TIME!r}")
    kept = f"{hm[0]:02d}:{hm[1]:02d}" if hm else ANY_TIME
    try:
        text = homes.config.read_bytes().decode("utf-8") if homes.config.exists() else ""
        tomllib.loads(text)
    except (OSError, ValueError) as exc:
        raise ValueError(f"{homes.config} cannot be read ({type(exc).__name__}); fix it first, nothing was written") from exc
    lines = text.splitlines()
    table = next((i for i, line in enumerate(lines) if line.lstrip().startswith("[")), len(lines))
    head = [line for line in lines[:table] if not _TOP_KEY.match(line)]
    while head and not head[-1].strip():
        head.pop()
    new = head + [f"dream_time = {json.dumps(kept)}"] + ([""] + lines[table:] if table < len(lines) else [])
    out = "\n".join(new) + "\n"
    if tomllib.loads(out).get("dream_time") != kept:
        raise ValueError(f"{homes.config}: the setting would not read back; nothing was written")
    write_private(homes.config, out)
    return kept


def timezone_name(setting: str) -> str:
    """An IANA name for the engine. ``auto``: the machine's current offset as ``Etc/GMT±N`` (one zone for the due check
    and for the days of corroboration; whole hours only — another offset falls back to UTC, set ``timezone``)."""
    if setting and setting != "auto":
        return setting
    offset = datetime.now().astimezone().utcoffset()
    seconds = int(offset.total_seconds()) if offset else 0
    if seconds % 3600:
        return "UTC"
    hours = seconds // 3600
    return "UTC" if hours == 0 else f"Etc/GMT{'-' if hours > 0 else '+'}{abs(hours)}"  # POSIX sign: inverted


def project_root(path: Path) -> Path:
    try:
        path = path.expanduser().resolve()
    except OSError:
        path = path.expanduser().absolute()
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate
    return path


def project_key(root: Path) -> str:
    """holographic-memory's name for a project database (``holomem.config.project_key``, the same rule)."""
    text = str(root)
    if sys.platform == "win32":
        text = text.lower()
    name = _SAFE.sub("_", root.name or "root")[:40]
    return f"{name}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:10]}"


@dataclass
class Store:
    path: Path | None
    scope: str
    min_trust: float | None = None
    problems: list[str] = field(default_factory=list)


def holomem_store(homes: Homes) -> Store:
    cfg_path = homes.holomem_config
    raw: dict = {}
    problems: list[str] = []
    if cfg_path.exists():
        try:
            raw = tomllib.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            problems.append(f"{cfg_path}: {type(exc).__name__}")
    db = str(raw.get("db_path") or "").strip()
    path = Path(os.path.expandvars(db)).expanduser() if db else homes.holomem / "memory_store.db"
    scope = str(raw.get("scope") or "global").strip().lower()
    trust = raw.get("min_trust_threshold")
    trust = float(trust) if isinstance(trust, (int, float)) else None
    if scope not in ("global", "project"):
        problems.append(f"scope {scope!r} unknown — treated as global")
        scope = "global"
    return Store(path, scope, trust, problems)


@dataclass
class Profile:
    name: str
    store: Path | None          # None: no facts (fact_source none)
    project_key: str | None     # messages of this project only; None: all
    notes: bool                 # the auto-memory notes are this profile's questions
    min_trust: float | None = None


def profiles(homes: Homes, settings: dict) -> list[Profile]:
    source = settings.get("fact_source", "auto")
    store = holomem_store(homes)
    if source == "none" or (source == "auto" and not (store.path and store.path.exists())
                            and not (homes.holomem / "projects").is_dir()):
        return [Profile("default", None, None, True)]
    if store.scope == "project":
        dbs = sorted((homes.holomem / "projects").glob("*.db"))
        return [Profile("notes", None, None, True)] + [
            Profile(f"project-{p.stem}", p, p.stem, False, store.min_trust) for p in dbs]
    return [Profile("global", store.path, None, True, store.min_trust)]
