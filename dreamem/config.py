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
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

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
}
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


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
    return _merge(DEFAULTS, {k: v for k, v in raw.items() if k in DEFAULTS}), [f"unknown setting: {k}" for k in unknown]


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
