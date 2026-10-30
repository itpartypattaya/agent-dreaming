"""The dreaming engine (``dreamem/engine``), two ways.

- In process (``module()``): its classifier of secrets and instructions, its harness-envelope rule and its §-entry
  parser — the same rules the pass applies, applied before anything reaches the shadow home. Importing the engine reads
  no file of any home (its own promise, ``dream.py`` end of file).
- As a program (``run_pass``): the night itself, ``dream.py --hermes-home <shadow home>``, in a child process with an
  environment cleaned of Hermes and dream variables, so nothing of a real Hermes install on the same machine leaks in.
"""
from __future__ import annotations

import functools
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ENGINE_DIR = Path(__file__).resolve().parent / "engine"
DREAM = ENGINE_DIR / "dream.py"
REJECT = ENGINE_DIR / "dream-reject.py"
# Variables the engine reads; a Hermes install on this machine must not steer our pass.
_FOREIGN_PREFIXES = ("DREAM_", "HERMES_", "HOLOGRAPHIC_")


@functools.lru_cache(maxsize=1)
def module():
    spec = importlib.util.spec_from_file_location("_dreamem_engine", DREAM)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def classify(text: str) -> str | None:
    """'secret' | 'injection' | None, by the engine's rules and the holographic provider's screen."""
    return module().classify_unsafe(text or "")


def is_envelope(text: str) -> bool:
    return bool(module().is_harness_envelope(text or ""))


def entry_key(entry: str) -> str:
    """The key the engine gives a §-entry's "still relevant?" cooldown."""
    return module()._entry_key(entry)


def fingerprint(content: str) -> str:
    """The key the engine gives a fact's cooldown (``seen`` buckets)."""
    return module()._fact_fingerprint(content)


def parse_entries(path: Path) -> list[str]:
    """§-entries of a memory file exactly as the engine parses them (entries of 10 characters or less are left out)."""
    return module().parse_md_entries(str(path))


def child_env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(_FOREIGN_PREFIXES)}
    env.update(HERMES_HOME=str(home), PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return env


def run_pass(home: Path, out: Path, *, timeout: float = 900) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-I", str(DREAM), "--hermes-home", str(home), "--config", str(home / "dreaming.json"),
           "--memory-md", str(home / "memories" / "MEMORY.md"), "--out", str(out),
           "--diary", str(home / "memories" / "DREAMS.md")]
    return subprocess.run(cmd, env=child_env(home), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)


def forget_sweep(home: Path, *, timeout: float = 120) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-I", str(DREAM), "--hermes-home", str(home), "--config", str(home / "dreaming.json"),
           "--memory-md", str(home / "memories" / "MEMORY.md"), "--forget-sweep"]
    return subprocess.run(cmd, env=child_env(home), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)


def reject(home: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    """``dream-reject.py`` on the shadow home: the reject list and the "merge?" proposals live in its cache."""
    cmd = [sys.executable, "-I", str(REJECT), "--hermes-home", str(home), *args]
    return subprocess.run(cmd, env=child_env(home), capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, stdin=subprocess.DEVNULL)
