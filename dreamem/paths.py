"""Where things live. One resolver, so every module and test agrees on it.

Every location can be moved by an environment variable, as the tools themselves allow:
``DREAMEM_HOME`` (ours, ``~/.dreaming``), ``CLAUDE_CONFIG_DIR`` (``~/.claude``), ``CODEX_HOME`` (``~/.codex``),
``HOLOMEM_HOME`` (``~/.holographic``). Everything outside ``DREAMEM_HOME`` is read, never written.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


def _dir(env: dict, name: str, default: str) -> Path:
    raw = (env.get(name) or "").strip()
    return Path(os.path.expandvars(raw)).expanduser() if raw else Path.home() / default


@dataclass(frozen=True)
class Homes:
    dreaming: Path
    claude: Path
    codex: Path
    holomem: Path

    @property
    def profiles(self) -> Path:
        return self.dreaming / "profiles"

    def profile(self, name: str) -> Path:
        return self.profiles / name

    @property
    def lock(self) -> Path:
        return self.dreaming / "lock"

    @property
    def config(self) -> Path:
        return self.dreaming / "config.toml"

    @property
    def summary(self) -> Path:
        return self.dreaming / "summary.json"

    # -- Claude Code ---------------------------------------------------------------------------------------------
    @property
    def claude_projects(self) -> Path:
        return self.claude / "projects"

    def claude_auto_memory_dirs(self) -> list[Path]:
        """``projects/<project>/memory`` of every project, plus ``autoMemoryDirectory`` of the user settings when it
        moves the folder elsewhere."""
        out = sorted(p for p in self.claude_projects.glob("*/memory") if p.is_dir())
        try:
            settings = json.loads((self.claude / "settings.json").read_text(encoding="utf-8"))
            moved = settings.get("autoMemoryDirectory") if isinstance(settings, dict) else None
        except (OSError, ValueError):
            moved = None
        if isinstance(moved, str) and moved.strip():
            path = Path(os.path.expandvars(moved)).expanduser()
            if path.is_dir() and path not in out:
                out.append(path)
        return out

    # -- Codex --------------------------------------------------------------------------------------------------
    def codex_session_dirs(self) -> list[Path]:
        return [p for p in (self.codex / "sessions", self.codex / "archived_sessions") if p.is_dir()]

    def codex_memory_dirs(self) -> list[Path]:
        """``memories`` (v1) and ``memories_v2``: the version is not hard-wired, both are read when present."""
        return sorted(p for p in self.codex.glob("memories*") if p.is_dir())

    # -- holographic-memory ----------------------------------------------------------------------------------------
    @property
    def holomem_config(self) -> Path:
        return self.holomem / "config.toml"


def homes(env: dict | None = None) -> Homes:
    env = os.environ if env is None else env
    return Homes(dreaming=_dir(env, "DREAMEM_HOME", ".dreaming"), claude=_dir(env, "CLAUDE_CONFIG_DIR", ".claude"),
                 codex=_dir(env, "CODEX_HOME", ".codex"), holomem=_dir(env, "HOLOMEM_HOME", ".holographic"))
