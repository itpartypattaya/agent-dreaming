"""The dreaming engine (``dreamem/engine/dream.py``), as this package uses it: in process, with every input handed in.

- Screening: the engine's classifier of secrets and instructions, its harness-envelope rule — the same rules the night
  applies, applied before anything is kept.
- The night: ``run_night`` configures the engine for a profile and runs ``dream.night()`` on the person's messages,
  the memory notes and the profile's state folder. Nothing is written but that folder.
"""
from __future__ import annotations

from pathlib import Path

from .engine import dream

ENGINE_DIR = Path(dream.__file__).resolve().parent


def classify(text: str) -> str | None:
    """'secret' | 'injection' | None."""
    return dream.classify_unsafe(text or "")


def is_envelope(text: str) -> bool:
    return bool(dream.is_harness_envelope(text or ""))


def entry_key(entry: str) -> str:
    """The key the engine gives a note's "still relevant?" cooldown."""
    return dream._entry_key(entry)


def fingerprint(content: str) -> str:
    """The key the engine gives a fact's cooldown (``seen`` buckets) and its reject record."""
    return dream._fact_fingerprint(content)


def state_files(folder: Path):
    return dream.state_files(folder)


def engine_config(*, store: Path | None, settings: dict, tz: str, min_trust: float | None) -> dict:
    cfg = {"timezone": tz, "fact_source": "holographic" if store is not None else "none",
           "store": str(store) if store is not None else "",
           "exclude_patterns": list(settings.get("exclude_patterns") or []),
           "agent_names": list(settings.get("agent_names") or [])}
    if min_trust is not None:
        cfg["gates"] = {"feedback_trust_floor": min_trust}
    return cfg


def run_night(*, state_dir: Path, config: dict, notes: list[str], dedupe: list[str], messages: list[dict],
              acks: dict | None, windows: dict) -> tuple[dict, list[str]]:
    """(the night's result, the notes as the engine indexed them). Sweeps what was forgotten or merged in the store
    since the last night out of the state first."""
    state_dir.mkdir(parents=True, exist_ok=True)
    dream.configure(config)
    state = dream.state_files(state_dir)
    dream.use_state(state)
    memory = dream.Memory(notes=notes, dedupe=dedupe)
    if config.get("store"):
        dream.forget_sweep()
        dream.merge_sweep()
    result = dream.night(memory=memory, messages=messages, state=state, acks=acks,
                         window_days=int(windows.get("themes_days", 14)),
                         corr_days=int(windows.get("corroboration_days", 30)),
                         md_decay_days=int(windows.get("md_decay_days", 30)))
    return result, dream.parse_md_entries(memory)


def forget_sweep(*, state_dir: Path, config: dict) -> dict:
    dream.configure(config)
    dream.use_state(dream.state_files(state_dir))
    return dream.forget_sweep()


def reject(state_dir: Path, fact_id, content: str, reason: str = "") -> str:
    return dream.reject_fact(dream.state_files(state_dir), fact_id, content, reason)


def unreject(state_dir: Path, fact_id) -> int:
    return dream.unreject_fact(dream.state_files(state_dir), fact_id)


def close_proposal(state_dir: Path, number: str, how: str) -> None:
    dream.close_proposal(dream.state_files(state_dir), number, how)
